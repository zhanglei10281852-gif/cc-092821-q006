from __future__ import annotations

from datetime import date

import pytest

from app.core.errors import ConflictError, ValidationError
from app.database import transaction
from app.germplasm.results import CALC_RULE_VERSION, pool_counts, risk_level_for
from app.germplasm.service import GermplasmService

from tests.test_germplasm_workflow import create_stored_lot


def _setup(service: GermplasmService, suffix: str = "001", replicate_count: int = 2):
    _, lot, _ = create_stored_lot(service, suffix)
    protocol = service.viability.create_protocol({
        "protocol_code": f"RICE-GER-{suffix}", "crop_name": "水稻", "sample_size": 100,
        "replicate_count": replicate_count, "temperature_c": 25, "duration_days": 14,
        "normal_seedling_rule": "根芽发育完整", "created_by": "技术负责人",
    })
    for risk, months in (("low", 24), ("medium", 12), ("high", 6)):
        service.viability.create_policy({
            "crop_name": "水稻", "risk_level": risk, "interval_months": months, "warning_days": 30,
            "minimum_germination_percent": 75, "effective_from": "2026-01-01", "effective_to": None,
            "created_by": "技术负责人",
        })
    test = service.viability.schedule_test({
        "test_no": f"VT-{suffix}", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
        "sampled_grams": 5, "scheduled_for": "2026-09-25", "requested_by": "检测员",
        "idempotency_key": f"schedule-vt-{suffix}",
    })
    service.viability.start_test(test["id"], {"performed_by": "检测员", "expected_version": 1})
    return lot, test


def _count(replicate_no, seeds_tested, normal, fresh=0, day=14):
    return {
        "replicate_no": replicate_no, "seeds_tested": seeds_tested, "normal_count": normal,
        "abnormal_count": 0, "dead_count": seeds_tested - normal - fresh, "fresh_count": fresh,
        "observation_day": day, "observed_by": "检测员",
    }


def test_pool_counts_uses_total_seeds_as_denominator():
    # 20 粒与 100 粒两个有效重复：旧口径 (50%+90%)/2=70.00%，合并口径 100/120=83.33%
    result = pool_counts([
        {"seeds_tested": 20, "normal_count": 10, "abnormal_count": 5, "dead_count": 5, "fresh_count": 0},
        {"seeds_tested": 100, "normal_count": 90, "abnormal_count": 5, "dead_count": 5, "fresh_count": 0},
    ])
    assert result["germination_percent"] == 83.33
    assert result["numerator_seeds"] == 100
    assert result["denominator_seeds"] == 120
    assert result["rule_version"] == CALC_RULE_VERSION
    assert result["risk_level"] == "medium"


def test_pool_counts_weights_vigor_by_seed_count():
    # (10+0.5*2)/20=55%、(90+0.5*10)/100=95% 的平均为 75%，合并应为 106/120
    result = pool_counts([
        {"seeds_tested": 20, "normal_count": 10, "abnormal_count": 0, "dead_count": 8, "fresh_count": 2},
        {"seeds_tested": 100, "normal_count": 90, "abnormal_count": 0, "dead_count": 0, "fresh_count": 10},
    ])
    assert result["vigor_index"] == round(100.0 * 106 / 120, 2) == 88.33
    assert result["vigor_numerator_seeds"] == 106


def test_pool_counts_rejects_zero_denominator():
    with pytest.raises(ValidationError):
        pool_counts([])


def test_risk_thresholds():
    assert risk_level_for(69.99) == "high"
    assert risk_level_for(70) == "medium"
    assert risk_level_for(84.99) == "medium"
    assert risk_level_for(85) == "low"


def test_completion_persists_traceable_pooled_result(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        lot, test = _setup(service, "101")
        service.viability.add_count(test["id"], _count(1, 20, 10))
        service.viability.add_count(test["id"], _count(2, 100, 90))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})

        assert completed["germination_percent"] == 83.33
        assert completed["vigor_index"] == 83.33
        result = completed["result"]
        assert result is not None
        assert result["rule_version"] == "pooled-weighted-v1"
        assert result["numerator_seeds"] == 100
        assert result["denominator_seeds"] == 120
        assert result["germination_percent"] == 83.33
        assert result["risk_level"] == "medium"
        assert len(result["adopted_counts"]) == 2
        assert result["calc"]["aggregation"] == "sum_normal_over_sum_tested"
        assert result["calc"]["class_totals"] == {"normal": 100, "abnormal": 0, "dead": 20, "fresh": 0}

        # 批次详情、检测明细必须指向同一份持久化结果
        lot_detail = service.repository.lot_detail(lot["id"])
        assert lot_detail["latest_viability"]["result"]["id"] == result["id"]

        # 复检日程使用同一结果与风险等级（83.33% -> medium -> 12 个月）
        due = service.viability.due_schedules(date(2028, 1, 1))
        assert len(due) == 1
        schedule = due[0]
        assert schedule["source_result_id"] == result["id"]
        assert schedule["risk_level"] == "medium"
        assert schedule["due_on"].startswith("2027-")
        assert "83.33" in schedule["reason"] and "100/120" in schedule["reason"]

        # 未触及低活力阈值，不产生告警
        alerts = service.quality.open_alerts()
        assert not any(a["alert_type"] == "low_viability" for a in alerts)


def test_risk_level_changes_when_pooled_correctly(client):
    # 旧口径 71%（medium 高风险侧），合并口径 102/120=85%（low），风险等级必须改变
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "102")
        service.viability.add_count(test["id"], _count(1, 20, 10))
        service.viability.add_count(test["id"], _count(2, 100, 92))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == 85.0
        assert completed["result"]["risk_level"] == "low"
        due = service.viability.due_schedules(date(2029, 1, 1))
        assert due[0]["risk_level"] == "low"
        assert due[0]["due_on"].startswith("2028-")  # low 风险 24 个月


def test_latest_observation_day_per_replicate_is_adopted(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "103")
        # 重复 1 有两个观察日，第 7 天 14/20、第 14 天 18/20
        service.viability.add_count(test["id"], _count(1, 20, 14, day=7))
        service.viability.add_count(test["id"], _count(1, 20, 18, day=14))
        service.viability.add_count(test["id"], _count(2, 100, 90))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == round(100.0 * 108 / 120, 2) == 90.0
        per_replicate = completed["result"]["calc"]["per_replicate"]
        adopted_day = {row["replicate_no"]: row["observation_day"] for row in per_replicate}
        assert adopted_day == {1: 14, 2: 14}
        # 第 7 天记录留痕但未采用
        excluded = completed["result"]["excluded_counts"]
        assert any(item["observation_day"] == 7 for item in excluded)


def test_voided_count_is_excluded_and_can_be_re_entered(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "104")
        first = service.viability.add_count(test["id"], _count(1, 20, 4))
        service.viability.add_count(test["id"], _count(2, 100, 90))
        voided = service.viability.void_count(first["id"], {"actor": "复核员", "reason": "培养皿霉变计数无效"})
        assert voided["status"] == "voided"
        assert voided["void_reason"] == "培养皿霉变计数无效"

        # 该重复缺少有效最终计数，不能完成
        with pytest.raises(ValidationError) as exc:
            service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert exc.value.context["missing_replicates"] == [1]

        # 作废后允许在同一重复同一观察日重新录入
        service.viability.add_count(test["id"], _count(1, 20, 16))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        assert completed["germination_percent"] == round(100.0 * 106 / 120, 2)
        excluded = completed["result"]["excluded_counts"]
        assert any(item["count_id"] == first["id"] and "霉变" in item["reason"] for item in excluded)


def test_zero_valid_counts_blocks_completion(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "105")
        first = service.viability.add_count(test["id"], _count(1, 20, 10))
        second = service.viability.add_count(test["id"], _count(2, 100, 90))
        service.viability.void_count(first["id"], {"actor": "复核员", "reason": "操作失误全部作废"})
        service.viability.void_count(second["id"], {"actor": "复核员", "reason": "操作失误全部作废"})
        with pytest.raises(ValidationError):
            service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        # 没有产生任何结果行
        assert service.repository.result_for_test(test["id"]) is None


def test_completion_is_idempotent_under_concurrent_finish(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "106")
        service.viability.add_count(test["id"], _count(1, 20, 10))
        service.viability.add_count(test["id"], _count(2, 100, 90))
        service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        # 重复完成（旧版本号）必须被拒绝
        with pytest.raises(ConflictError):
            service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        # 数据库层面对同一检测只允许一条结果
        import sqlite3
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO viability_results(test_id,rule_version,numerator_seeds,denominator_seeds,"
                "vigor_numerator_seeds,germination_percent,vigor_index,risk_level,adopted_counts_json,"
                "computed_by,computed_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (test["id"], "fake", 1, 1, 1, 1, 1, "low", "[]", "x", "2026-10-01T00:00:00+00:00"),
            )
        rows = connection.execute("SELECT COUNT(*) FROM viability_results WHERE test_id=?", (test["id"],)).fetchone()[0]
        assert rows == 1


def test_history_is_not_silently_rewritten(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        lot, test = _setup(service, "107")
        saved = service.viability.add_count(test["id"], _count(1, 20, 10))
        service.viability.add_count(test["id"], _count(2, 100, 90))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        result_id = completed["result"]["id"]

        # 完成后计数不可改、不可作废，必须新建检测纠错
        with pytest.raises(ConflictError):
            service.viability.replace_count(saved["id"], _count(1, 20, 20))
        with pytest.raises(ConflictError):
            service.viability.void_count(saved["id"], {"actor": "复核员", "reason": "检测结束后尝试改写"})

        # 作废检测：日程失效，但原始结果行原样保留作为历史凭证
        service.viability.invalidate_test(test["id"], {
            "expected_version": 3, "reason": "取样污染，整批评定无效", "actor": "审核员",
        })
        result = service.repository.require_result(result_id)
        assert result["germination_percent"] == 83.33
        assert result["numerator_seeds"] == 100 and result["denominator_seeds"] == 120
        schedules = connection.execute(
            "SELECT status FROM retest_schedules WHERE source_result_id=?", (result_id,)
        ).fetchall()
        assert schedules and all(row[0] == "superseded" for row in schedules)

        # 作废后可以对同一批次安排新检测
        new_test = service.viability.schedule_test({
            "test_no": "VT-107-R", "lot_id": lot["id"], "protocol_id": test["protocol_id"],
            "test_type": "异常复核", "sampled_grams": 5, "scheduled_for": "2026-10-05",
            "requested_by": "检测员", "idempotency_key": "schedule-vt-107-r",
        })
        assert new_test["status"] == "scheduled"


def test_low_viability_alert_uses_persisted_result(client):
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test = _setup(service, "108")
        # 合并发芽率 48/120 = 40% -> high 风险且触发低活力告警
        service.viability.add_count(test["id"], _count(1, 20, 6))
        service.viability.add_count(test["id"], _count(2, 100, 42))
        completed = service.viability.complete_test(test["id"], {"performed_by": "检测员", "expected_version": 2})
        result = completed["result"]
        assert result["risk_level"] == "high"

        alerts = [a for a in service.quality.open_alerts() if a["alert_type"] == "low_viability"]
        assert len(alerts) == 1
        detail = alerts[0]["detail"]
        assert detail["result_id"] == result["id"]
        assert detail["numerator_seeds"] == 48 and detail["denominator_seeds"] == 120
        assert detail["value"] == 40.0 and detail["rule_version"] == CALC_RULE_VERSION

        # 日程与告警引用同一结果
        schedule = service.viability.due_schedules(date(2028, 1, 1))[0]
        assert schedule["source_result_id"] == result["id"] and schedule["risk_level"] == "high"
        assert schedule["due_on"].startswith("2027-04")  # high 风险 6 个月


def test_api_detail_summary_risk_and_schedule_agree(client, admin):
    headers = admin["headers"]
    # 走完整 HTTP 流程：建资源、批次、规程、策略、检测、计数、完成
    accession_no, lot_no, proto_code = "API-V-1", "API-VL-1", "API-RICE-V1"
    source_resp = client.post("/api/germplasm/sources", headers=headers, json={
        "source_code": "API-VS-1", "provider_name": "合作站", "country_code": "CN", "restrictions": {},
    })
    assert source_resp.status_code == 201, source_resp.text
    source_id = source_resp.json()["id"]
    acc = client.post("/api/germplasm/accessions", headers=headers, json={
        "accession_no": accession_no, "scientific_name": "Oryza sativa", "crop_name": "水稻",
        "source_id": source_id, "acquisition_type": "采集", "received_on": "2026-09-01", "created_by": "登记员",
    })
    assert acc.status_code == 201, acc.text
    accession_id = acc.json()["id"]
    accepted = client.post(f"/api/germplasm/accessions/{accession_id}/transition", headers=headers, json={
        "target_status": "accepted", "reason": "手续齐全", "expected_version": 1, "actor": "审核员",
    })
    assert accepted.status_code == 200, accepted.text
    loc = client.post("/api/germplasm/locations", headers=headers, json={
        "location_code": "API-VLOC1", "facility": "长期库", "room": "一室", "rack": "A", "shelf": "1",
        "capacity_grams": 3000, "temperature_c": -18, "humidity_percent": 35,
    }).json()
    lot_resp = client.post("/api/germplasm/lots", headers=headers, json={
        "lot_no": lot_no, "accession_id": accession_id, "harvest_year": 2025,
        "initial_weight_grams": 800, "created_by": "登记员",
    })
    assert lot_resp.status_code == 201, lot_resp.text
    lot = lot_resp.json()
    placed = client.post("/api/germplasm/placements", headers=headers, json={
        "lot_id": lot["id"], "location_id": loc["id"], "weight_grams": 800,
        "container_code": "API-VBOX1", "idempotency_key": "api-v-place-1", "actor": "保管员",
    })
    assert placed.status_code == 201, placed.text
    protocol = client.post("/api/germplasm/protocols", headers=headers, json={
        "protocol_code": proto_code, "crop_name": "水稻", "sample_size": 100, "replicate_count": 2,
        "temperature_c": 25, "duration_days": 14, "normal_seedling_rule": "根芽发育完整", "created_by": "负责人",
    }).json()
    for risk in ("low", "medium", "high"):
        resp = client.post("/api/germplasm/policies", headers=headers, json={
            "crop_name": "水稻", "risk_level": risk, "interval_months": 12, "warning_days": 30,
            "minimum_germination_percent": 75, "effective_from": "2026-01-01", "created_by": "负责人",
        })
        assert resp.status_code == 201, resp.text
    test = client.post("/api/germplasm/tests", headers=headers, json={
        "test_no": "API-VT-1", "lot_id": lot["id"], "protocol_id": protocol["id"], "test_type": "周期复检",
        "sampled_grams": 5, "scheduled_for": "2026-09-25", "requested_by": "检测员",
        "idempotency_key": "api-v-schedule-1",
    }).json()
    client.post(f"/api/germplasm/tests/{test['id']}/start", headers=headers, json={
        "performed_by": "检测员", "expected_version": 1,
    })
    for payload in (_count(1, 20, 10), _count(2, 100, 90)):
        resp = client.post(f"/api/germplasm/tests/{test['id']}/counts", headers=headers, json=payload)
        assert resp.status_code == 201, resp.text
    done = client.post(f"/api/germplasm/tests/{test['id']}/complete", headers=headers, json={
        "performed_by": "检测员", "expected_version": 2,
    })
    assert done.status_code == 200, done.text
    body = done.json()
    assert body["germination_percent"] == 83.33
    assert body["result"]["numerator_seeds"] == 100
    assert body["result"]["denominator_seeds"] == 120

    # GET 明细与完成响应一致
    fetched = client.get(f"/api/germplasm/tests/{test['id']}", headers=headers).json()
    assert fetched["result"]["id"] == body["result"]["id"]
    assert fetched["germination_percent"] == fetched["result"]["germination_percent"] == 83.33

    lot_view = client.get(f"/api/germplasm/lots/{lot['id']}", headers=headers).json()
    assert lot_view["latest_viability"]["result"]["id"] == body["result"]["id"]

    # 作废计数接口可用
    count_id = body["counts"][0]["id"]
    # 已完成检测不允许作废计数
    refused = client.post(f"/api/germplasm/counts/{count_id}/void", headers=headers, json={
        "actor": "复核员", "reason": "试图事后修改",
    })
    assert refused.status_code == 409
