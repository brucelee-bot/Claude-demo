"""
评分计算引擎 — 支持高新技术 & 专精特新评分规则
"""
import json
import os
from typing import Any


def load_rules(rule_type: str) -> dict:
    """加载评分规则 JSON"""
    base = os.path.dirname(__file__)
    filename = {"高新技术": "rules_gaoxin.json", "专精特新": "rules_zhuanjing.json", "小巨人": "rules_xiaojuren.json"}.get(rule_type, f"rules_{rule_type}.json")
    path = os.path.join(base, filename)
    if not os.path.exists(path):
        raise FileNotFoundError(f"规则文件不存在: {path}")
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def _score_threshold(value: float, thresholds: list[dict]) -> tuple[int, int, str]:
    """根据阈值匹配分数区间"""
    for t in thresholds:
        if t["min"] <= value <= t["max"]:
            # 兼容 "score" 和 "score_range" 两种 key
            s = t.get("score") or t.get("score_range", [0, 0])
            if isinstance(s, list):
                return s[0], s[1], t.get("label", "")
            return s, s, t.get("label", "")
    return 0, 0, ""


def _score_grade(grade: str, grades: dict) -> tuple[int, int]:
    """根据等级获取分数区间"""
    if grade in grades:
        r = grades[grade]["score_range"]
        return r[0], r[1]
    return 0, 0


def calculate(data: dict, rule_type: str = "高新技术") -> dict:
    """
    计算评分

    data 示例:
    {
        "company_name": "XX科技",
        "ip_tech_level": "A",       # 等级 A-E
        "ip_core_support": "B",
        "ip_quantity": "A",
        "ip_acquisition": "A",
        "ip_standard": "B",
        "ip_tech_level_score": 8,   # 可覆盖默认取高值
        "ip_core_support_score": 6,
        "ip_quantity_score": 8,
        "ip_acquisition_score": 5,
        "ip_standard_score": 0,
        "transform_count": 5,
        "transform_score": 28,
        "rd_system": 5,
        "rd_institution": 4,
        "rd_transform_incentive": 3,
        "rd_talent": 3,
        "growth_net_assets_rate": 0.28,  # 28%
        "growth_sales_rate": 0.32,       # 32%
        "growth_net_assets_score": 8,
        "growth_sales_score": 8
    }
    """
    rules = load_rules(rule_type)
    breakdown = []
    total = 0
    warnings = []

    for indicator in rules["indicators"]:
        cat_score = 0
        cat_max = indicator["max_score"]
        sub_items = []

        for sub in indicator["sub_indicators"]:
            input_type = sub.get("input_type", "grade")
            sid = sub["id"]
            sid_score = f"{sid}_score"

            # 用户手动指定分数（优先） > 自动计算
            if input_type != "calculated" and sid_score in data and data[sid_score] is not None:
                user_score = int(data[sid_score])
                score_low = score_high = user_score
            elif input_type == "grade":
                grade = data.get(sid, "E")
                if grade == "E" and sub.get("is_bonus"):
                    score_low = score_high = 0
                else:
                    score_low, score_high = _score_grade(grade, sub["grades"])
                    score_low = score_high
            elif input_type == "number":
                value = float(data.get(sid, 0))
                # 特殊公式处理
                formula = sub.get("formula", "")
                if formula == "min(floor(value/2), 5)":
                    value = min(int(value) // 2, 5)
                    score_low = score_high = int(value)
                elif sub.get("thresholds"):
                    score_low, score_high, label = _score_threshold(value, sub["thresholds"])
                else:
                    score_low = score_high = 0
            elif input_type == "multi":
                # 多选：每项加分，总和不能超过 max_score
                score_low = score_high = 0
                for opt in sub.get("options", []):
                    if data.get(opt["id"]):
                        score_low += opt["score"]
                        score_high += opt["score"]
            elif input_type == "score":
                score_low = score_high = int(data.get(sid, 0))
            elif input_type == "dual_threshold":
                # 双阈值：金额或占比，取较高分
                amount = float(data.get(sub.get("amount_field", "rd_amount"), 0))
                ratio = float(data.get(sub.get("ratio_field", "rd_ratio"), 0))
                best = 0
                for t in sub.get("thresholds", []):
                    a_min, a_max = t.get("amount_min", -1), t.get("amount_max", 999999)
                    r_min, r_max = t.get("ratio_min", -1), t.get("ratio_max", 999)
                    if (a_min <= amount <= a_max) or (r_min <= ratio <= r_max):
                        s = t.get("score_range", [0, 0])
                        best = max(best, s[0], s[1])
                score_low = score_high = best
            elif input_type == "calculated":
                formula = sub.get("formula", "")
                # 优先使用预计算值
                rate_key = f"{formula.replace('growth_rate_', 'growth_')}_rate"
                if rate_key in data and data[rate_key] is not None:
                    value = float(data[rate_key])
                    score_low, score_high, label = _score_threshold(value, sub["thresholds"])
                else:
                    score_low = score_high = 0
            else:
                score_low = score_high = 0

            # Clamp
            score_low = max(0, min(score_low, sub["max_score"]))
            score_high = max(0, min(score_high, sub["max_score"]))

            cat_score += score_high

            sub_items.append({
                "id": sid,
                "name": sub["name"],
                "score": score_high,
                "max_score": sub["max_score"],
                "is_bonus": sub.get("is_bonus", False),
            })

        # 含加分项指标，总分不能超过上限
        cat_score = min(cat_score, cat_max)
        total += cat_score

        breakdown.append({
            "id": indicator["id"],
            "name": indicator["name"],
            "score": cat_score,
            "max_score": cat_max,
            "sub_items": sub_items,
        })

    total = min(total, rules["full_score"])
    passed = total >= rules["pass_score"]

    if not passed:
        gap = rules["pass_score"] - total
        warnings.append(f"距离达标还差 {gap} 分，达标线为 {rules['pass_score']} 分")

    return {
        "rule_type": rule_type,
        "rule_name": rules["rule_name"],
        "total_score": total,
        "full_score": rules["full_score"],
        "pass_score": rules["pass_score"],
        "passed": passed,
        "breakdown": breakdown,
        "warnings": warnings,
    }


def calculate_growth_rates(financials: dict) -> dict:
    """
    计算净资产增长率和销售收入增长率

    financials: {
        "year1_net_assets": float,  # 第一年末净资产
        "year2_net_assets": float,
        "year3_net_assets": float,
        "year1_sales": float,       # 第一年销售收入
        "year2_sales": float,
        "year3_sales": float,
    }
    """
    def growth_rate(v1, v2, v3):
        """净资产增长率 / 销售收入增长率"""
        if v1 == 0:
            # 第一年为 0，按后两年计算
            if v2 == 0:
                return 0.0
            return (v3 / v2) - 1
        if v2 == 0:
            # 第二年为 0，按 0 分
            return 0.0
        return 0.5 * (v2 / v1 + v3 / v2) - 1

    net_rate = growth_rate(
        financials.get("year1_net_assets", 0),
        financials.get("year2_net_assets", 0),
        financials.get("year3_net_assets", 0),
    )
    sales_rate = growth_rate(
        financials.get("year1_sales", 0),
        financials.get("year2_sales", 0),
        financials.get("year3_sales", 0),
    )

    # 负增长按 0 分
    return {
        "growth_net_assets_rate": round(max(0, net_rate), 4),
        "growth_sales_rate": round(max(0, sales_rate), 4),
    }
