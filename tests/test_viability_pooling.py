from __future__ import annotations

import json
import sqlite3
from datetime import date

import pytest

from app.core.errors import ConflictError, ValidationError
from app.database import get_connection, transaction
from app.germplasm.service import GermplasmService
from app.germplasm.viability import RULE_VERSION
from tests.test_germplasm_workflow import create_stored_lot


def _setup(service: GermplasmService, suffix: str, sample_size: int = 100, replicate_count: int = 2,
           policies=("high", "medium", "low")) -> tuple[dict, dict, str]:
    _, lot, _ = create_stored_lot(service, suffix)
    protocol = service.viability.create_protocol({
        "protocol_code": f"RICE-GER-{suffix}", "crop_name": "水稻", "sample_size": sample_size,
        "replicate_count": replicate_count, "temperature_c": 25, "duration_days": 14,
        "normal_seedling_rule": "根芽发育完整", "created_by": "技术负责人",
    })
    for risk, months in [("high", 3), ("medium", 12), ("low", 24)]:
        if risk in policies:
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
    return lot, service.repository.test_detail(test["id"]), suffix


def _count(service, test_id, replicate_no, seeds, normal, *, day=14, fresh=0, abnormal=0, dead=None, by="检测员"):
    dead = seeds - normal - abnormal - fresh if dead is None else dead
    return service.viability.add_count(test_id, {
        "replicate_no": replicate_no, "seeds_tested": seeds, "normal_count": normal,
        "abnormal_count": abnormal, "dead_count": dead, "fresh_count": fresh,
        "observation_day": day, "observed_by": by,
    })


def _complete(service, test_id, version=2):
    return service.viability.complete_test(test_id, {"performed_by": "检测员", "expected_version": version})


def test_unequal_replicate_sizes_use_pooled_total(client):
    """二十粒与一百粒两个重复：不得平均各自百分比，必须以总粒数为分母（83.33% 而非 70.00%）。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, suffix = _setup(service, "UNEQ")
        _count(service, test["id"], 1, seeds=20, normal=14)   # 70%
        _count(service, test["id"], 2, seeds=100, normal=86)  # 86%
        completed = _complete(service, test["id"])
        assert completed["germination_percent"] == pytest.approx(83.33)
        assert completed["vigor_index"] == pytest.approx(83.33)
        result = completed["result"]
        assert result["rule_version"] == RULE_VERSION
        assert result["normal_total"] == 100
        assert result["seeds_total"] == 120
        assert result["germination_percent"] == pytest.approx(round(100 * 100 / 120, 2))
        assert result["risk_level"] == "medium"
        assert {c["replicate_no"] for c in result["adopted_counts"]} == {1, 2}


def test_multi_observation_day_adopts_latest_day_only(client):
    """同一重复多个观察日：只有该重复最大观察日的有效计数进入分子分母。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, _ = _setup(service, "DAYS")
        _count(service, test["id"], 1, seeds=100, normal=40, day=7)
        _count(service, test["id"], 1, seeds=100, normal=90, day=14)  # 最终计数
        _count(service, test["id"], 2, seeds=50, normal=20, day=7)
        _count(service, test["id"], 2, seeds=50, normal=45, day=14)
        completed = _complete(service, test["id"])
        # (90+45)/(100+50) = 90.0
        assert completed["germination_percent"] == 90.0
        result = completed["result"]
        assert result["normal_total"] == 135
        assert result["seeds_total"] == 150
        assert {c["observation_day"] for c in result["adopted_counts"]} == {14}
        # 早期观察日计数仍可追溯
        assert len(completed["counts"]) == 4


def test_voided_count_excluded_and_correction_keeps_history(client):
    """作废计数不参与口径；订正后旧记录保留为 voided，历史不被静默改写。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, _ = _setup(service, "VOID")
        wrong = _count(service, test["id"], 1, seeds=100, normal=10, day=14)
        good_1 = _count(service, test["id"], 2, seeds=100, normal=90, day=14)
        # 订正：旧行作废、新行追加
        corrected = service.viability.replace_count(wrong["id"], {
            "replicate_no": 1, "seeds_tested": 100, "normal_count": 80, "abnormal_count": 10,
            "dead_count": 10, "fresh_count": 0, "observation_day": 14, "observed_by": "复核员",
        })
        old = service.repository.require_count(wrong["id"])
        assert old["status"] == "voided" and old["voided_by"] == "复核员" and old["void_reason"]
        assert corrected["status"] == "active" and corrected["normal_count"] == 80
        # 直接作废另一条有效的当日计数 → 缺少该重复最终计数时不能完成
        service.viability.void_count(good_1["id"], {"reason": "培养皿污染作废", "actor": "复核员"})
        with pytest.raises(ValidationError) as exc:
            _complete(service, test["id"])
        assert exc.value.context["missing_replicates"] == [2]
        # 补录后按 (80+85)/(100+100)
        _count(service, test["id"], 2, seeds=100, normal=85, day=14, by="复核员")
        completed = _complete(service, test["id"])
        assert completed["germination_percent"] == 82.5
        assert all(c["count_id"] != wrong["id"] for c in completed["result"]["adopted_counts"])


def test_zero_valid_counts_rejects_completion(client):
    """有效计数全部缺失/作废（零有效样本）时不得产生百分比或风险结论。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, _ = _setup(service, "ZERO")
        first = _count(service, test["id"], 1, seeds=100, normal=50, day=14)
        second = _count(service, test["id"], 2, seeds=100, normal=50, day=14)
        # 作废一个重复：缺重复，不能完成
        service.viability.void_count(first["id"], {"reason": "样本失活作废", "actor": "复核员"})
        with pytest.raises(ValidationError) as exc:
            _complete(service, test["id"])
        assert exc.value.context["missing_replicates"] == [1]
        # 全部作废：零有效样本，明确拒绝，且无结果、无风险结论
        service.viability.void_count(second["id"], {"reason": "培养箱故障作废", "actor": "复核员"})
        with pytest.raises(ValidationError, match="零有效样本"):
            _complete(service, test["id"])
        assert service.repository.test_result(test["id"]) is None
        assert service.repository.require_test(test["id"])["status"] == "running"


def test_concurrent_completion_persists_single_result(client):
    """并发完成：版本条件更新 + 结果唯一约束，保证只有一个结果生效。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, _ = _setup(service, "CONC")
        _count(service, test["id"], 1, seeds=100, normal=80, day=14)
        _count(service, test["id"], 2, seeds=100, normal=90, day=14)
        _complete(service, test["id"], version=2)
        # 同版本再次完成（并发的赢家已推进 version）
        with pytest.raises(ConflictError):
            _complete(service, test["id"], version=2)
        rows = connection.execute("SELECT COUNT(*) FROM viability_results WHERE test_id=?", (test["id"],)).fetchone()[0]
        assert rows == 1
        detail = service.repository.test_detail(test["id"])
        assert detail["status"] == "completed" and detail["version"] == 3


def test_alert_schedule_and_api_share_one_persisted_result(client):
    """低活力告警与复检策略都来自同一份持久化结果；明细、汇总、风险、日程彼此一致。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        lot, test, _ = _setup(service, "LOWV")
        _count(service, test["id"], 1, seeds=20, normal=8, day=14)    # 40%
        _count(service, test["id"], 2, seeds=100, normal=40, day=14)  # 40%
        completed = _complete(service, test["id"])
        # 汇总 48/120 = 40.0 → 高风险 + 低活力告警
        assert completed["germination_percent"] == 40.0
        assert completed["result"]["risk_level"] == "high"
        alerts = service.quality.open_alerts("critical")
        low = [a for a in alerts if a["alert_type"] == "low_viability"]
        assert len(low) == 1
        alert_detail = json.loads(low[0]["detail"]) if isinstance(low[0]["detail"], str) else low[0]["detail"]
        assert alert_detail["test_id"] == test["id"]
        assert alert_detail["value"] == 40.0
        assert alert_detail["rule_version"] == RULE_VERSION
        result = service.repository.test_result(test["id"])
        assert result["low_viability_alert_id"] == low[0]["id"]
        # 日程携带同一结果的风险与规则版本，且高风险 3 个月后到期
        due = service.viability.due_schedules(date(2027, 1, 1))
        mine = [s for s in due if s["lot_id"] == lot["id"]]
        assert len(mine) == 1
        assert mine[0]["risk_level"] == "high"
        assert mine[0]["rule_version"] == RULE_VERSION
        assert mine[0]["due_on"] == "2027-01-01"
        assert "40.00%" in mine[0]["reason"]
        # 批次明细读到的最新活力与检测结果一致
        lot_detail = service.repository.lot_detail(lot["id"])
        latest = lot_detail["latest_viability"]
        assert latest["germination_percent"] == 40.0
        assert latest["risk_level"] == "high"
        assert latest["result"]["id"] == result["id"]


def test_invalidation_keeps_result_history_and_cancels_schedule(client):
    """作废检测不删除历史结果，派生日程失效，最新有效活力不再引用该检测。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        lot, test, _ = _setup(service, "INV")
        _count(service, test["id"], 1, seeds=100, normal=80, day=14)
        _count(service, test["id"], 2, seeds=100, normal=82, day=14)
        _complete(service, test["id"])
        result_id = service.repository.test_result(test["id"])["id"]
        service.viability.invalidate_test(test["id"], {"expected_version": 3, "reason": "培养箱温度异常", "actor": "审核员"})
        # 结果行仍在
        assert service.repository.test_result(test["id"])["id"] == result_id
        # 没有活动日程
        due = service.viability.due_schedules(date(2030, 1, 1))
        assert all(s["lot_id"] != lot["id"] for s in due)
        # 批次最新有效活力为空（历史检测不静默充当有效结论）
        assert service.repository.lot_detail(lot["id"])["latest_viability"] is None


def test_fresh_seed_half_weighting_uses_pooled_denominator(client):
    """活力指数对新鲜未发芽粒按半粒计分子，分母仍是总有效粒数。"""
    with transaction(immediate=True) as connection:
        service = GermplasmService(connection)
        _, test, _ = _setup(service, "FRSH")
        # 重复1: 10 正常 + 10 新鲜 /20；重复2: 50 正常 + 10 新鲜 /100
        _count(service, test["id"], 1, seeds=20, normal=10, fresh=10, day=14)
        _count(service, test["id"], 2, seeds=100, normal=50, fresh=10, day=14,
               dead=40)
        completed = _complete(service, test["id"])
        # 发芽率 60/120 = 50；活力 (60 + 0.5*20)/120 = 70/120
        assert completed["germination_percent"] == 50.0
        assert completed["vigor_index"] == pytest.approx(round(100 * 70 / 120, 2))
        result = completed["result"]
        assert result["fresh_total"] == 20
        assert result["vigor_numerator"] == 70.0
        assert result["seeds_total"] == 120


def test_http_detail_and_risk_consistency(client, admin):
    """端到端：API 明细中的分子分母、风险等级与日程一致。"""
    headers = admin["headers"]
    # 直接走服务层造数后用 HTTP 读取
    with transaction(immediate=True):
        service = GermplasmService(get_connection())
        _, test, _ = _setup(service, "HTTPV")
        _count(service, test["id"], 1, seeds=20, normal=14, day=14)
        _count(service, test["id"], 2, seeds=100, normal=86, day=14)
        _complete(service, test["id"])
        test_id = test["id"]
    detail = client.get(f"/api/germplasm/tests/{test_id}", headers=headers)
    assert detail.status_code == 200
    body = detail.json()
    assert body["germination_percent"] == pytest.approx(83.33)
    assert body["risk_level"] == "medium"
    assert body["result"]["seeds_total"] == 120 and body["result"]["normal_total"] == 100
    due = client.get("/api/germplasm/retest-schedules/due", params={"before": "2028-01-01"}, headers=headers)
    assert due.status_code == 200
    row = [s for s in due.json() if s["source_test_id"] == test_id][0]
    assert row["risk_level"] == "medium" and row["rule_version"] == RULE_VERSION


def test_legacy_completed_tests_backfilled_without_rewriting_value(client, tmp_path):
    """历史库迁移：旧表重建 + 已完成检测补建结果行，保留当时报告值并标记遗留规则版本。"""
    import os

    db_path = tmp_path / "legacy.db"
    os.environ["GERMPLASM_DATABASE_PATH"] = str(db_path)
    from app.database import close_connection
    close_connection()
    # 用旧结构建库
    old_schema = """
    CREATE TABLE viability_protocols(
        id INTEGER PRIMARY KEY AUTOINCREMENT, protocol_code TEXT, version INTEGER, crop_name TEXT,
        sample_size INTEGER, replicate_count INTEGER, temperature_c REAL, duration_days INTEGER,
        normal_seedling_rule TEXT, active INTEGER, created_by TEXT, created_at TEXT,
        UNIQUE(protocol_code,version));
    CREATE TABLE viability_tests(
        id INTEGER PRIMARY KEY AUTOINCREMENT, test_no TEXT UNIQUE, lot_id INTEGER, protocol_id INTEGER,
        test_type TEXT, sampled_grams REAL, scheduled_for TEXT, started_at TEXT, completed_at TEXT,
        status TEXT, germination_percent REAL, vigor_index REAL, invalid_reason TEXT, requested_by TEXT,
        performed_by TEXT, version INTEGER, created_at TEXT, updated_at TEXT);
    CREATE TABLE viability_counts(
        id INTEGER PRIMARY KEY AUTOINCREMENT, test_id INTEGER, replicate_no INTEGER, seeds_tested INTEGER,
        normal_count INTEGER, abnormal_count INTEGER, dead_count INTEGER, fresh_count INTEGER DEFAULT 0,
        observation_day INTEGER, observed_by TEXT, created_at TEXT,
        UNIQUE(test_id,replicate_no,observation_day));
    """
    conn = sqlite3.connect(db_path)
    conn.executescript(old_schema)
    conn.execute(
        "INSERT INTO viability_protocols VALUES(1,'RICE',1,'水稻',100,2,25,14,'rule',1,'t','2026-01-01')"
    )
    # lot_id 用 999（外键关闭场景下迁移不校验；回填只读计数）
    conn.execute(
        "INSERT INTO viability_tests(id,test_no,lot_id,protocol_id,test_type,sampled_grams,scheduled_for,"
        "started_at,completed_at,status,germination_percent,vigor_index,requested_by,performed_by,version,"
        "created_at,updated_at) VALUES(1,'OLD',999,1,'周期复检',5,'2026-09-01','2026-09-02','2026-09-16',"
        "'completed',70.0,70.0,'检测员','检测员',3,'2026-09-01','2026-09-16')"
    )
    # 20 粒中 14 正常、100 粒中 86 正常 —— 旧算法平均 70/86 → 70.0 已写入报告
    conn.execute(
        "INSERT INTO viability_counts(test_id,replicate_no,seeds_tested,normal_count,abnormal_count,dead_count,"
        "fresh_count,observation_day,observed_by,created_at) VALUES(1,1,20,14,3,3,0,14,'检测员','2026-09-16')"
    )
    conn.execute(
        "INSERT INTO viability_counts(test_id,replicate_no,seeds_tested,normal_count,abnormal_count,dead_count,"
        "fresh_count,observation_day,observed_by,created_at) VALUES(1,2,100,86,7,7,0,14,'检测员','2026-09-16')"
    )
    conn.commit()
    conn.close()

    from app.database import migrate_db
    migrate_db()
    from app.germplasm.repository import GermplasmRepository
    repo = GermplasmRepository(get_connection())
    result = repo.test_result(1)
    assert result is not None
    # 历史报告值保留，不被新算法静默改写
    assert result["germination_percent"] == 70.0
    assert "历史遗留" in result["rule_version"]
    assert result["seeds_total"] == 120 and result["normal_total"] == 100
    # 旧 UNIQUE 已替换为部分唯一索引：可对同一观察日“作废+追加”
    close_connection()
