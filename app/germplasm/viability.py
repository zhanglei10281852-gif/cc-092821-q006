from __future__ import annotations

import calendar
import sqlite3
from datetime import date, datetime
from typing import Any

from app.core.clock import Clock, SystemClock, to_storage
from app.core.errors import ConflictError, ValidationError
from app.germplasm.inventory import InventoryService
from app.germplasm.repository import GermplasmRepository, record, records
from app.germplasm.results import LOW_VIABILITY_THRESHOLD, pool_counts


class ViabilityService:
    def __init__(self, connection: sqlite3.Connection, clock: Clock | None = None) -> None:
        self.connection = connection
        self.clock = clock or SystemClock()
        self.repository = GermplasmRepository(connection)
        self.inventory = InventoryService(connection, self.clock)

    def create_protocol(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.repository.protocol_latest(data["protocol_code"])
        version = int(latest["version"]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        if latest:
            self.connection.execute("UPDATE viability_protocols SET active=0 WHERE id=?", (latest["id"],))
        cursor = self.connection.execute(
            "INSERT INTO viability_protocols(protocol_code,version,crop_name,sample_size,replicate_count,temperature_c,"
            "duration_days,normal_seedling_rule,active,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,1,?,?)",
            (
                data["protocol_code"], version, data["crop_name"], data["sample_size"], data["replicate_count"],
                data["temperature_c"], data["duration_days"], data["normal_seedling_rule"], data["created_by"], timestamp,
            ),
        )
        return self.repository.require_protocol(int(cursor.lastrowid))

    def schedule_test(self, data: dict[str, Any]) -> dict[str, Any]:
        replay = self.connection.execute(
            "SELECT response_json FROM idempotency_records WHERE scope='viability.schedule' AND idempotency_key=?",
            (data["idempotency_key"],),
        ).fetchone()
        if replay:
            import json
            return self.repository.test_detail(int(json.loads(replay[0])["test_id"]))
        lot = self.repository.require_lot(int(data["lot_id"]))
        protocol = self.repository.require_protocol(int(data["protocol_id"]))
        accession = self.repository.require_accession(int(lot["accession_id"]))
        if protocol["crop_name"] != accession["crop_name"]:
            raise ValidationError("检测规程与资源作物不匹配")
        if lot["status"] in {"depleted", "disposed"}:
            raise ConflictError("耗尽或报废批次不能安排检测")
        if float(data["sampled_grams"]) > float(lot["available_weight_grams"]):
            raise ConflictError("检测取样重量超过批次可用重量")
        active = self.connection.execute(
            "SELECT id FROM viability_tests WHERE lot_id=? AND status IN ('scheduled','running')",
            (lot["id"],),
        ).fetchone()
        if active:
            raise ConflictError("该批次已有未完成的活力检测", context={"test_id": active[0]})
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO viability_tests(test_no,lot_id,protocol_id,test_type,sampled_grams,scheduled_for,status,"
                "requested_by,created_at,updated_at) VALUES(?,?,?,?,?,?,'scheduled',?,?,?)",
                (
                    data["test_no"], lot["id"], protocol["id"], data["test_type"], data["sampled_grams"],
                    data["scheduled_for"], data["requested_by"], timestamp, timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("检测编号已经存在") from exc
        test_id = int(cursor.lastrowid)
        import json
        self.connection.execute(
            "INSERT INTO idempotency_records(scope,idempotency_key,request_hash,response_json,status_code,created_at) "
            "VALUES('viability.schedule',?,? ,?,201,?)",
            (data["idempotency_key"], data["test_no"], json.dumps({"test_id": test_id}), timestamp),
        )
        schedule = self.connection.execute(
            "SELECT id FROM retest_schedules WHERE lot_id=? AND status IN ('pending','notified') ORDER BY due_on LIMIT 1",
            (lot["id"],),
        ).fetchone()
        if schedule:
            self.connection.execute(
                "UPDATE retest_schedules SET status='scheduled',updated_at=? WHERE id=?", (timestamp, schedule[0])
            )
        return self.repository.test_detail(test_id)

    def start_test(self, test_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_test(test_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检测任务版本冲突", context={"current_version": test["version"]})
        if test["status"] != "scheduled":
            raise ConflictError("只有待执行检测可以开始")
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE viability_tests SET status='running',started_at=?,performed_by=?,version=version+1,updated_at=? "
            "WHERE id=? AND version=? AND status='scheduled'",
            (timestamp, data["performed_by"], timestamp, test_id, data["expected_version"]),
        )
        if cursor.rowcount != 1:
            raise ConflictError("检测任务版本冲突")
        sample_key = f"viability-sample-{test_id}"
        self.inventory.withdraw({
            "lot_id": test["lot_id"],
            "quantity_grams": test["sampled_grams"],
            "movement_type": "取样",
            "idempotency_key": sample_key,
            "actor": data["performed_by"],
            "reason": f"活力检测 {test['test_no']} 取样",
        })
        return self.repository.test_detail(test_id)

    def add_count(self, test_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_test(test_id)
        if test["status"] != "running":
            raise ConflictError("只有执行中的检测可以录入计数")
        protocol = self.repository.require_protocol(int(test["protocol_id"]))
        if int(data["replicate_no"]) > int(protocol["replicate_count"]):
            raise ValidationError("重复编号超过规程规定的重复数")
        if int(data["seeds_tested"]) > int(protocol["sample_size"]):
            raise ValidationError("单个重复的检测粒数超过规程样本数")
        if int(data["observation_day"]) > int(protocol["duration_days"]):
            raise ValidationError("观察日超过规程持续天数")
        total = int(data["normal_count"]) + int(data["abnormal_count"]) + int(data["dead_count"]) + int(data.get("fresh_count", 0))
        if total != int(data["seeds_tested"]):
            raise ValidationError("正常、异常、死亡和新鲜未发芽计数之和必须等于检测粒数")
        timestamp = to_storage(self.clock.now())
        try:
            cursor = self.connection.execute(
                "INSERT INTO viability_counts(test_id,replicate_no,seeds_tested,normal_count,abnormal_count,dead_count,"
                "fresh_count,observation_day,observed_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    test_id, data["replicate_no"], data["seeds_tested"], data["normal_count"],
                    data["abnormal_count"], data["dead_count"], data.get("fresh_count", 0),
                    data["observation_day"], data["observed_by"], timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            raise ConflictError("该重复在该观察日已经录入") from exc
        return record(self.connection.execute("SELECT * FROM viability_counts WHERE id=?", (cursor.lastrowid,)).fetchone()) or {}

    def replace_count(self, count_id: int, data: dict[str, Any]) -> dict[str, Any]:
        existing = record(self.connection.execute("SELECT * FROM viability_counts WHERE id=?", (count_id,)).fetchone())
        if not existing:
            raise ValidationError("计数记录不存在")
        test = self.repository.require_test(int(existing["test_id"]))
        if test["status"] != "running":
            raise ConflictError("已经结束的检测不能修改计数")
        total = int(data["normal_count"]) + int(data["abnormal_count"]) + int(data["dead_count"]) + int(data.get("fresh_count", 0))
        if total != int(data["seeds_tested"]):
            raise ValidationError("分类计数之和必须等于检测粒数")
        self.connection.execute(
            "UPDATE viability_counts SET seeds_tested=?,normal_count=?,abnormal_count=?,dead_count=?,fresh_count=?,"
            "observation_day=?,observed_by=? WHERE id=?",
            (
                data["seeds_tested"], data["normal_count"], data["abnormal_count"], data["dead_count"],
                data.get("fresh_count", 0), data["observation_day"], data["observed_by"], count_id,
            ),
        )
        return record(self.connection.execute("SELECT * FROM viability_counts WHERE id=?", (count_id,)).fetchone()) or {}

    def void_count(self, count_id: int, data: dict[str, Any]) -> dict[str, Any]:
        """作废单条计数（如霉变、操作失误）；作废后不可恢复，只能重新录入。"""
        existing = record(self.connection.execute("SELECT * FROM viability_counts WHERE id=?", (count_id,)).fetchone())
        if not existing:
            raise ValidationError("计数记录不存在")
        if existing["status"] == "voided":
            raise ConflictError("该计数已经作废")
        test = self.repository.require_test(int(existing["test_id"]))
        if test["status"] != "running":
            raise ConflictError("已经结束的检测不能作废计数")
        reason = str(data.get("reason", "")).strip()
        if len(reason) < 3:
            raise ValidationError("作废计数时必须填写不少于三个字的原因")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE viability_counts SET status='voided',voided_at=?,voided_by=?,void_reason=? WHERE id=? AND status='valid'",
            (timestamp, data["actor"], reason, count_id),
        )
        return record(self.connection.execute("SELECT * FROM viability_counts WHERE id=?", (count_id,)).fetchone()) or {}

    def complete_test(self, test_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_test(test_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检测任务版本冲突", context={"current_version": test["version"]})
        if test["status"] != "running":
            raise ConflictError("只有执行中的检测可以完成")
        protocol = self.repository.require_protocol(int(test["protocol_id"]))
        adopted, excluded = self._adopted_counts(test_id)
        replicate_count = int(protocol["replicate_count"])
        present = {int(item["replicate_no"]) for item in adopted}
        missing = [no for no in range(1, replicate_count + 1) if no not in present]
        if missing:
            raise ValidationError("每个规程重复都必须有有效的最终计数", context={
                "expected": replicate_count, "adopted": sorted(present), "missing_replicates": missing,
            })
        result = pool_counts(adopted)
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "UPDATE viability_tests SET status='completed',germination_percent=?,vigor_index=?,completed_at=?,"
            "performed_by=?,version=version+1,updated_at=? WHERE id=? AND version=? AND status='running'",
            (
                result["germination_percent"], result["vigor_index"], timestamp, data["performed_by"], timestamp,
                test_id, data["expected_version"],
            ),
        )
        if cursor.rowcount != 1:
            raise ConflictError("检测任务版本冲突")
        adopted_ids = [int(item["id"]) for item in adopted]
        excluded_payload = [
            {"count_id": int(item["id"]), "replicate_no": int(item["replicate_no"]),
             "observation_day": int(item["observation_day"]), "reason": item.get("void_reason", "")}
            for item in excluded
        ]
        calc = {
            "aggregation": "sum_normal_over_sum_tested",
            "selection": "latest_valid_observation_day_per_replicate",
            "low_viability_threshold": LOW_VIABILITY_THRESHOLD,
            "low_viability_alert": result["low_viability_alert"],
            "class_totals": {
                "normal": result["normal_total"], "abnormal": result["abnormal_total"],
                "dead": result["dead_total"], "fresh": result["fresh_total"],
            },
            "per_replicate": [
                {"replicate_no": int(item["replicate_no"]), "observation_day": int(item["observation_day"]),
                 "seeds_tested": int(item["seeds_tested"]), "normal_count": int(item["normal_count"]),
                 "fresh_count": int(item.get("fresh_count", 0))}
                for item in adopted
            ],
        }
        import json
        try:
            result_cursor = self.connection.execute(
                "INSERT INTO viability_results(test_id,rule_version,numerator_seeds,denominator_seeds,"
                "vigor_numerator_seeds,germination_percent,vigor_index,risk_level,adopted_counts_json,"
                "excluded_counts_json,calc_json,computed_by,computed_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    test_id, result["rule_version"], result["numerator_seeds"], result["denominator_seeds"],
                    result["vigor_numerator_seeds"], result["germination_percent"], result["vigor_index"],
                    result["risk_level"], json.dumps(adopted_ids), json.dumps(excluded_payload, ensure_ascii=False),
                    json.dumps(calc, ensure_ascii=False), data["performed_by"], timestamp,
                ),
            )
        except sqlite3.IntegrityError as exc:
            # UNIQUE(test_id)：并发/重复完成时只允许一份结果生效
            raise ConflictError("该检测已经存在生效结果，不能重复计算") from exc
        persisted = self.repository.require_result(int(result_cursor.lastrowid))
        # 告警与复检日程必须使用同一份已持久化结果，不接受各自重算
        self._schedule_next(test_id, persisted, timestamp)
        if result["low_viability_alert"]:
            self._create_low_viability_alert(test_id, persisted, timestamp)
        return self.repository.test_detail(test_id)

    def invalidate_test(self, test_id: int, data: dict[str, Any]) -> dict[str, Any]:
        test = self.repository.require_test(test_id)
        if int(test["version"]) != int(data["expected_version"]):
            raise ConflictError("检测任务版本冲突", context={"current_version": test["version"]})
        if test["status"] not in {"running", "completed"}:
            raise ConflictError("当前检测状态不能作废")
        timestamp = to_storage(self.clock.now())
        self.connection.execute(
            "UPDATE viability_tests SET status='invalidated',invalid_reason=?,version=version+1,updated_at=? WHERE id=? AND version=?",
            (data["reason"], timestamp, test_id, data["expected_version"]),
        )
        self.connection.execute(
            "UPDATE retest_schedules SET status='superseded',updated_at=? WHERE source_test_id=? AND status IN ('pending','notified','scheduled')",
            (timestamp, test_id),
        )
        return self.repository.test_detail(test_id)

    def create_policy(self, data: dict[str, Any]) -> dict[str, Any]:
        latest = self.connection.execute(
            "SELECT version FROM retest_policies WHERE crop_name=? AND risk_level=? ORDER BY version DESC LIMIT 1",
            (data["crop_name"], data["risk_level"]),
        ).fetchone()
        version = int(latest[0]) + 1 if latest else 1
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            "INSERT INTO retest_policies(crop_name,risk_level,interval_months,warning_days,minimum_germination_percent,"
            "effective_from,effective_to,version,created_by,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                data["crop_name"], data["risk_level"], data["interval_months"], data["warning_days"],
                data["minimum_germination_percent"], data["effective_from"], data.get("effective_to"), version,
                data["created_by"], timestamp,
            ),
        )
        return self.repository.require_policy(int(cursor.lastrowid))

    def due_schedules(self, before: date, limit: int = 100) -> list[dict[str, Any]]:
        return records(self.connection.execute(
            "SELECT s.*,l.lot_no,a.accession_no,a.crop_name FROM retest_schedules s "
            "JOIN seed_lots l ON l.id=s.lot_id JOIN accessions a ON a.id=l.accession_id "
            "WHERE s.status IN ('pending','notified') AND s.due_on<=? ORDER BY s.due_on,l.lot_no LIMIT ?",
            (before.isoformat(), limit),
        ).fetchall())

    def mark_notifications(self, schedule_ids: list[int]) -> int:
        if not schedule_ids:
            return 0
        placeholders = ",".join("?" for _ in schedule_ids)
        timestamp = to_storage(self.clock.now())
        cursor = self.connection.execute(
            f"UPDATE retest_schedules SET status='notified',updated_at=? WHERE id IN ({placeholders}) AND status='pending'",
            (timestamp, *schedule_ids),
        )
        return int(cursor.rowcount)

    def _adopted_counts(self, test_id: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        """选取每个重复最终观察日上的有效计数，并返回被排除的作废/过期计数。

        - 同一重复多个观察日：只采用观察日序号最大的一天；
        - 作废计数（status='voided'）不参与分子分母；
        - 若最终观察日的计数被作废，该重复视为缺少有效最终计数。
        """
        rows = records(self.connection.execute(
            "SELECT * FROM viability_counts WHERE test_id=? ORDER BY replicate_no,observation_day,id",
            (test_id,),
        ).fetchall())
        valid = [item for item in rows if item["status"] == "valid"]
        latest_day: dict[int, int] = {}
        for item in valid:
            replicate_no = int(item["replicate_no"])
            day = int(item["observation_day"])
            if replicate_no not in latest_day or day > latest_day[replicate_no]:
                latest_day[replicate_no] = day
        adopted = [
            item for item in valid
            if int(item["observation_day"]) == latest_day[int(item["replicate_no"])]
        ]
        adopted_ids = {int(item["id"]) for item in adopted}
        excluded = [item for item in rows if int(item["id"]) not in adopted_ids]
        return adopted, excluded

    def _schedule_next(self, test_id: int, result: dict[str, Any], timestamp: str) -> None:
        test = self.repository.require_test(test_id)
        lot = self.repository.require_lot(int(test["lot_id"]))
        accession = self.repository.require_accession(int(lot["accession_id"]))
        risk = result["risk_level"]
        germination = float(result["germination_percent"])
        completed_date = datetime.fromisoformat(timestamp).date()
        policy = self.repository.applicable_policy(accession["crop_name"], risk, completed_date.isoformat())
        if policy is None:
            return
        self.connection.execute(
            "UPDATE retest_schedules SET status='superseded',updated_at=? WHERE lot_id=? "
            "AND status IN ('pending','notified','scheduled')",
            (timestamp, lot["id"]),
        )
        due = add_months(completed_date, int(policy["interval_months"]))
        self.connection.execute(
            "INSERT INTO retest_schedules(lot_id,source_test_id,source_result_id,policy_id,due_on,status,risk_level,"
            "reason,created_at,updated_at) VALUES(?,?,?,?,?, 'pending',?,?,?,?)",
            (
                lot["id"], test_id, result["id"], policy["id"], due.isoformat(), risk,
                f"检测结果 {germination:.2f}%（{result['numerator_seeds']}/{result['denominator_seeds']}，"
                f"规则 {result['rule_version']}）对应 {risk} 风险",
                timestamp, timestamp,
            ),
        )

    def _create_low_viability_alert(self, test_id: int, result: dict[str, Any], timestamp: str) -> None:
        test = self.repository.require_test(test_id)
        key = f"low-viability-{test_id}"
        import json
        detail = {
            "test_id": test_id,
            "result_id": result["id"],
            "value": float(result["germination_percent"]),
            "numerator_seeds": result["numerator_seeds"],
            "denominator_seeds": result["denominator_seeds"],
            "rule_version": result["rule_version"],
            "threshold": LOW_VIABILITY_THRESHOLD,
        }
        self.connection.execute(
            "INSERT OR IGNORE INTO quality_alerts(alert_key,alert_type,severity,lot_id,message,detail_json,created_at,updated_at) "
            "VALUES(?,'low_viability','critical',?,?,?,?,?)",
            (
                key, test["lot_id"],
                f"批次活力降至 {float(result['germination_percent']):.2f}%"
                f"（{result['numerator_seeds']}/{result['denominator_seeds']} 粒）",
                json.dumps(detail, ensure_ascii=False), timestamp, timestamp,
            ),
        )


def add_months(value: date, months: int) -> date:
    target_month = value.month - 1 + months
    year = value.year + target_month // 12
    month = target_month % 12 + 1
    day = min(value.day, calendar.monthrange(year, month)[1])
    return date(year, month, day)
