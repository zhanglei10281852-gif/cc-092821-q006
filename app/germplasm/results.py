"""活力检测结果的合并计算规则。

历史缺陷（规则 v0，已废弃）：对每个重复分别计算百分比后做算术平均，
重复样本量不同时小样本重复被赋予同等权重，例如 20 粒与 100 粒两个重复
会把 100/120 的真实发芽率 83.33% 错报为 70.00%。

规则 pooled-weighted-v1：以全部有效重复最终计数的总粒数为分母合并计算，
重复样本量不同时按粒数自然加权。
"""

from __future__ import annotations

from typing import Any

from app.core.errors import ValidationError

CALC_RULE_VERSION = "pooled-weighted-v1"

# 发芽率低于该阈值触发低活力告警
LOW_VIABILITY_THRESHOLD = 50.0
# 风险等级阈值：[0,HIGH) high，[HIGH,MEDIUM) medium，[MEDIUM,100] low
HIGH_RISK_BELOW = 70.0
MEDIUM_RISK_BELOW = 85.0


def risk_level_for(germination_percent: float) -> str:
    if germination_percent < HIGH_RISK_BELOW:
        return "high"
    if germination_percent < MEDIUM_RISK_BELOW:
        return "medium"
    return "low"


def pool_counts(adopted_counts: list[dict[str, Any]]) -> dict[str, Any]:
    """按最终有效计数汇总分子分母，不在调用处自行拼公式。

    adopted_counts 为每个重复在其最终观察日的有效计数记录；
    重复样本量可以不同，权重由各自粒数决定。
    """
    denominator = sum(int(item["seeds_tested"]) for item in adopted_counts)
    if denominator <= 0:
        raise ValidationError("有效计数的总粒数为零，无法计算发芽率与活力指标")
    normal_total = sum(int(item["normal_count"]) for item in adopted_counts)
    abnormal_total = sum(int(item["abnormal_count"]) for item in adopted_counts)
    dead_total = sum(int(item["dead_count"]) for item in adopted_counts)
    fresh_total = sum(int(item.get("fresh_count", 0)) for item in adopted_counts)
    # 新鲜未发芽种子按半权计入活力分子（沿用既有判定口径，分母改为合并总粒数）
    vigor_numerator = normal_total + 0.5 * fresh_total
    germination = round(100.0 * normal_total / denominator, 2)
    vigor = round(100.0 * vigor_numerator / denominator, 2)
    return {
        "rule_version": CALC_RULE_VERSION,
        "numerator_seeds": normal_total,
        "denominator_seeds": denominator,
        "vigor_numerator_seeds": vigor_numerator,
        "normal_total": normal_total,
        "abnormal_total": abnormal_total,
        "dead_total": dead_total,
        "fresh_total": fresh_total,
        "replicate_count": len(adopted_counts),
        "germination_percent": germination,
        "vigor_index": vigor,
        "risk_level": risk_level_for(germination),
        "low_viability_alert": germination < LOW_VIABILITY_THRESHOLD,
    }
