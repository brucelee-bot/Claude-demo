"""高新技术企业申报体检引擎。

体检结果刻意和专家评分分开：

* qualification：硬性资格条件；
* evidence：材料和事实依据完整度；
* consistency：跨表、跨来源的数据冲突；
* score：评分结果区间，而不是未经校准的通过概率。

这个模块不依赖 Flask、数据库或模板，便于在导出、页面和测试中复用。
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Any


RULE_VERSION = "国科发火〔2016〕32号 / 认定工作指引（规则快照）"
DEFAULT_APPLICATION_YEAR = datetime.now().year


def _text(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _number(value: Any) -> float | None:
    if value in (None, ""):
        return None
    if isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).replace(",", "").replace("，", ""))
    except (TypeError, ValueError):
        return None


def _first_number(data: dict, *keys: str) -> float | None:
    for key in keys:
        value = _number(data.get(key))
        if value is not None:
            return value
    return None


def _parse_date(value: Any) -> date | None:
    raw = _text(value)
    if not raw:
        return None
    raw = raw.replace("年", "-").replace("月", "-").replace("日", "")
    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%Y-%m", "%Y/%m", "%Y"):
        try:
            parsed = datetime.strptime(raw, fmt)
            return parsed.date()
        except ValueError:
            continue
    return None


def _status_item(
    item_id: str,
    title: str,
    status: str,
    detail: str,
    *,
    value: Any = None,
    threshold: Any = None,
    target: str = "",
) -> dict:
    return {
        "id": item_id,
        "title": title,
        "status": status,
        "detail": detail,
        "value": value,
        "threshold": threshold,
        "target": target,
    }


def _status_lists(items: list[dict]) -> dict:
    return {
        "items": items,
        "passed": [item for item in items if item["status"] == "pass"],
        "failed": [item for item in items if item["status"] == "fail"],
        "pending": [item for item in items if item["status"] == "pending"],
    }


def _relation_rows(data: dict) -> list[dict]:
    relation = data.get("gaoxin_relation_table")
    if not isinstance(relation, dict):
        return []
    rows = relation.get("rows")
    return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []


def _unique_relation_values(rows: list[dict], *fields: str) -> list[str]:
    values = []
    for row in rows:
        for field in fields:
            value = _text(row.get(field))
            if value and value not in values:
                values.append(value)
    return values


def _relation_entity_count(rows: list[dict], *identity_fields: str) -> int:
    """Count each row by its first available identifier, using fields as fallbacks."""
    identities = set()
    for row in rows:
        for field in identity_fields:
            value = _text(row.get(field))
            if value:
                identities.add((field, value))
                break
    return len(identities)


def _finance_totals(data: dict, application_year: int) -> dict:
    year_keys = [str(application_year - 3), str(application_year - 2), str(application_year - 1)]

    def yearly_sum(suffixes: tuple[str, ...]) -> float | None:
        values = []
        for year in year_keys:
            value = None
            for suffix in suffixes:
                value = _number(data.get(f"fin_{year}_{suffix}"))
                if value is None:
                    value = _number(data.get(f"{year}_{suffix}"))
                if value is not None:
                    break
            if value is not None:
                values.append(value)
        if not values:
            return None
        return sum(values)

    rd_total = _first_number(data, "rd_total_3y", "fin_rd_total")
    rd_total = rd_total if rd_total is not None else yearly_sum(("rd_expense", "rd"))

    rd_domestic = _first_number(data, "rd_domestic", "fin_rd_domestic")
    rd_domestic = rd_domestic if rd_domestic is not None else yearly_sum(("rd_domestic",))

    sales_total = yearly_sum(("sales", "revenue", "total_revenue"))
    latest_revenue = _first_number(data, "revenue_1y", "revenue_1y_total", "fin_revenue")
    if latest_revenue is None:
        latest_revenue = _first_number(
            data,
            f"fin_{year_keys[-1]}_sales",
            f"fin_{year_keys[-1]}_revenue",
            f"{year_keys[-1]}_sales",
        )

    hitech_revenue = _first_number(
        data,
        "hitech_revenue",
        "hitech_revenue_1y",
        "fin_hitech_revenue",
    )

    return {
        "years": year_keys,
        "rd_total": rd_total,
        "rd_domestic": rd_domestic,
        "sales_total": sales_total,
        "latest_revenue": latest_revenue,
        "hitech_revenue": hitech_revenue,
    }


def _ip_count(data: dict, attachments: dict) -> int | None:
    rows = _relation_rows(data)
    relation_count = _relation_entity_count(
        rows, "ip_code", "ip_auth_no", "ip_name"
    )
    ip_list = data.get("ip_list")
    list_count = len([item for item in ip_list if isinstance(item, dict) and (
        _text(item.get("name")) or _text(item.get("patent_no")) or _text(item.get("auth_no"))
    )]) if isinstance(ip_list, list) else 0
    files = attachments.get("ip", {}).get("files", []) if isinstance(attachments, dict) else []
    file_count = len([item for item in files if isinstance(item, dict)])
    class1 = _number(data.get("ip_class1_count"))
    class2 = _number(data.get("ip_class2_count"))
    counts = [count for count in (relation_count, list_count, file_count) if count > 0]
    if class1 is not None or class2 is not None:
        class_count = int((class1 or 0) + (class2 or 0))
        if class_count > 0:
            counts.append(class_count)
    return max(counts) if counts else (0 if any(
        key in data for key in ("ip_class1_count", "ip_class2_count", "ip_list")
    ) else None)


def _staff_counts(data: dict) -> tuple[float | None, float | None]:
    rows = data.get("hr_staff_rows")
    if isinstance(rows, list) and rows:
        named = [row for row in rows if isinstance(row, dict) and _text(row.get("姓名"))]
        tech = [
            row for row in named
            if _text(row.get("是否科技人员")).lower() in {"是", "yes", "true", "1"}
        ]
        return float(len(named)), float(len(tech))
    return (
        _first_number(data, "staff_total", "hr_total"),
        _first_number(data, "tech_staff", "hr_tech"),
    )


def _bool_flag(data: dict, *keys: str) -> bool | None:
    positive = {"yes", "true", "1", "是", "有", "发生", "存在"}
    negative = {"no", "false", "0", "否", "无", "未发生", "不存在"}
    for key in keys:
        raw = _text(data.get(key)).lower()
        if not raw:
            continue
        if raw in positive:
            return True
        if raw in negative:
            return False
    return None


def _build_qualification(data: dict, attachments: dict, application_year: int) -> dict:
    relation = _relation_rows(data)
    finance = _finance_totals(data, application_year)
    staff_total, tech_staff = _staff_counts(data)
    items = []

    registration = None
    for key in (
        "registration_date",
        "register_date",
        "establish_date",
        "company_established_at",
        "company_register_date",
        "成立日期",
    ):
        registration = _parse_date(data.get(key))
        if registration:
            break
    if registration:
        elapsed_days = (date(application_year, 12, 31) - registration).days
        if elapsed_days >= 365:
            items.append(_status_item(
                "registered_one_year", "注册成立一年以上", "pass",
                f"注册日期 {registration.isoformat()}，已超过一年。",
                value=registration.isoformat(), threshold="≥365天",
            ))
        else:
            items.append(_status_item(
                "registered_one_year", "注册成立一年以上", "fail",
                f"注册日期 {registration.isoformat()}，截至申报年度末不足一年。",
                value=registration.isoformat(), threshold="≥365天",
            ))
    else:
        items.append(_status_item(
            "registered_one_year", "注册成立一年以上", "pending",
            "缺少注册成立日期，无法核验成立年限。",
            target="application",
        ))

    tech_field = _text(
        data.get("tech_field")
        or ((data.get("gaoxin_relation_table") or {}).get("tech_field_path")
            if isinstance(data.get("gaoxin_relation_table"), dict) else "")
    )
    if tech_field:
        items.append(_status_item(
            "key_technology_field", "主要技术属于重点支持领域", "pass",
            f"已填写技术领域：{tech_field}。",
            value=tech_field, target="relation_table",
        ))
    else:
        items.append(_status_item(
            "key_technology_field", "主要技术属于重点支持领域", "pending",
            "尚未选择主要技术领域，无法判断技术归属。",
            target="relation_table",
        ))

    ip_count = _ip_count(data, attachments)
    if ip_count is None:
        items.append(_status_item(
            "core_ip", "拥有对主要产品发挥核心支持作用的知识产权", "pending",
            "尚未识别知识产权数量或证明材料。",
            target="relation_table",
        ))
    elif ip_count > 0:
        items.append(_status_item(
            "core_ip", "拥有对主要产品发挥核心支持作用的知识产权", "pass",
            f"已识别 {ip_count} 项知识产权；核心支持作用仍需结合产品逐项核对。",
            value=ip_count, threshold="≥1项", target="relation_table",
        ))
    else:
        items.append(_status_item(
            "core_ip", "拥有对主要产品发挥核心支持作用的知识产权", "fail",
            "当前数据明确为 0 项知识产权。",
            value=0, threshold="≥1项", target="relation_table",
        ))

    if staff_total is None or tech_staff is None:
        items.append(_status_item(
            "tech_staff_ratio", "科技人员占职工总数不低于10%", "pending",
            "缺少职工总数或科技人员数。",
            target="application",
        ))
    elif staff_total <= 0:
        items.append(_status_item(
            "tech_staff_ratio", "科技人员占职工总数不低于10%", "fail",
            "职工总数为 0，比例无效。",
            value=0, threshold="≥10%", target="application",
        ))
    elif tech_staff < 0 or tech_staff > staff_total:
        items.append(_status_item(
            "tech_staff_ratio", "科技人员占职工总数不低于10%", "fail",
            f"科技人员数 {tech_staff:g} 超出职工总数 {staff_total:g}，数据冲突。",
            value=f"{tech_staff:g}/{staff_total:g}", threshold="科技人员≤职工总数",
            target="application",
        ))
    else:
        ratio = tech_staff / staff_total
        items.append(_status_item(
            "tech_staff_ratio", "科技人员占职工总数不低于10%",
            "pass" if ratio >= 0.10 else "fail",
            f"科技人员占比 {ratio * 100:.2f}%。",
            value=round(ratio * 100, 2), threshold="≥10%", target="application",
        ))

    if finance["rd_total"] is None or finance["sales_total"] is None:
        items.append(_status_item(
            "rd_expense_ratio", "近三年研发费用占销售收入比例达标", "pending",
            "缺少近三年研发费用或销售收入，无法按营收档位核验。",
            target="application",
        ))
    elif finance["sales_total"] <= 0 or finance["rd_total"] < 0:
        items.append(_status_item(
            "rd_expense_ratio", "近三年研发费用占销售收入比例达标", "fail",
            "研发费用或销售收入数据无效。",
            value=f"{finance['rd_total']}/{finance['sales_total']}", target="application",
        ))
    else:
        rd_ratio = finance["rd_total"] / finance["sales_total"]
        # 申报书和评分表的金额单位统一为“万元”，因此分档阈值为
        # 5000/20000，而不是按人民币元填写的 5000 万/2 亿元。
        if finance["sales_total"] < 5_000:
            threshold = 0.05
        elif finance["sales_total"] <= 20_000:
            threshold = 0.04
        else:
            threshold = 0.03
        items.append(_status_item(
            "rd_expense_ratio", "近三年研发费用占销售收入比例达标",
            "pass" if rd_ratio >= threshold else "fail",
            f"研发费用占比 {rd_ratio * 100:.2f}%，按销售收入档位要求不低于 {threshold * 100:.0f}%。",
            value=round(rd_ratio * 100, 2), threshold=f"≥{threshold * 100:.0f}%",
            target="application",
        ))

    if finance["rd_total"] is None or finance["rd_domestic"] is None:
        items.append(_status_item(
            "domestic_rd_ratio", "境内研发费用占研发费用比例不低于60%", "pending",
            "缺少研发费用总额或境内研发费用，无法核验。",
            target="application",
        ))
    elif finance["rd_total"] <= 0 or finance["rd_domestic"] < 0:
        items.append(_status_item(
            "domestic_rd_ratio", "境内研发费用占研发费用比例不低于60%", "fail",
            "研发费用数据无效。",
            value=f"{finance['rd_domestic']}/{finance['rd_total']}", target="application",
        ))
    elif finance["rd_domestic"] > finance["rd_total"]:
        items.append(_status_item(
            "domestic_rd_ratio", "境内研发费用占研发费用比例不低于60%", "fail",
            "境内研发费用大于研发费用总额，数据冲突。",
            value=f"{finance['rd_domestic']}/{finance['rd_total']}", threshold="境内≤总额",
            target="application",
        ))
    else:
        domestic_ratio = finance["rd_domestic"] / finance["rd_total"]
        items.append(_status_item(
            "domestic_rd_ratio", "境内研发费用占研发费用比例不低于60%",
            "pass" if domestic_ratio >= 0.60 else "fail",
            f"境内研发费用占比 {domestic_ratio * 100:.2f}%。",
            value=round(domestic_ratio * 100, 2), threshold="≥60%", target="application",
        ))

    if finance["latest_revenue"] is None or finance["hitech_revenue"] is None:
        items.append(_status_item(
            "hitech_revenue_ratio", "高新技术产品（服务）收入占比不低于60%", "pending",
            "缺少近一年企业总收入或高新技术产品收入。",
            target="application",
        ))
    elif finance["latest_revenue"] <= 0 or finance["hitech_revenue"] < 0:
        items.append(_status_item(
            "hitech_revenue_ratio", "高新技术产品（服务）收入占比不低于60%", "fail",
            "收入数据无效。",
            value=f"{finance['hitech_revenue']}/{finance['latest_revenue']}", target="application",
        ))
    elif finance["hitech_revenue"] > finance["latest_revenue"]:
        items.append(_status_item(
            "hitech_revenue_ratio", "高新技术产品（服务）收入占比不低于60%", "fail",
            "高新技术产品收入大于企业总收入，数据冲突。",
            value=f"{finance['hitech_revenue']}/{finance['latest_revenue']}",
            threshold="高新收入≤总收入", target="application",
        ))
    else:
        hitech_ratio = finance["hitech_revenue"] / finance["latest_revenue"]
        items.append(_status_item(
            "hitech_revenue_ratio", "高新技术产品（服务）收入占比不低于60%",
            "pass" if hitech_ratio >= 0.60 else "fail",
            f"高新技术产品收入占比 {hitech_ratio * 100:.2f}%。",
            value=round(hitech_ratio * 100, 2), threshold="≥60%", target="application",
        ))

    violation = _bool_flag(
        data,
        "major_accident",
        "accident",
        "serious_environmental_violation",
        "environment_violation",
    )
    if violation is None:
        no_violation = _bool_flag(data, "no_violation")
        violation = None if no_violation is None else not no_violation
    if violation is None:
        items.append(_status_item(
            "no_major_violation", "申请前一年无重大安全、质量事故或严重环境违法", "pending",
            "尚未完成安全、质量和环境合规声明核验。",
            target="application",
        ))
    else:
        items.append(_status_item(
            "no_major_violation", "申请前一年无重大安全、质量事故或严重环境违法",
            "fail" if violation else "pass",
            "数据记录显示存在相关事故或违法行为。" if violation else "数据记录显示未发生相关事故或违法行为。",
            value="是" if violation else "否", threshold="不得发生", target="application",
        ))

    return _status_lists(items)


def _file_count(attachments: dict, key: str) -> int:
    section = attachments.get(key) if isinstance(attachments, dict) else {}
    files = section.get("files") if isinstance(section, dict) else []
    return len([item for item in files if isinstance(item, dict)])


def _year_file_count(attachments: dict, key: str, year: str) -> int:
    section = attachments.get(key) if isinstance(attachments, dict) else {}
    files = section.get("files") if isinstance(section, dict) else []
    return len([
        item for item in files
        if isinstance(item, dict) and _text(item.get("attachment_year")) == str(year)
    ])


def _build_evidence(data: dict, attachments: dict, application_year: int) -> dict:
    relation = _relation_rows(data)
    finance_years = [str(application_year - 3), str(application_year - 2), str(application_year - 1)]
    items = []

    def add(item_id, title, status, detail, target):
        items.append({
            "id": item_id,
            "title": title,
            "status": status,
            "detail": detail,
            "target": target,
        })

    for key, title, target in (
        ("application_pdf", "申请书签字盖章 PDF", "attachments"),
        ("business_license", "营业执照", "attachments"),
        ("hitech_income_audit", "近一年高新产品收入专项审计报告", "attachments"),
    ):
        count = _file_count(attachments, key)
        add(key, title, "complete" if count else "missing",
            f"已上传 {count} 份。" if count else "尚未上传。",
            target)

    ip_files = _file_count(attachments, "ip")
    ip_rows = _relation_entity_count(relation, "ip_code", "ip_auth_no", "ip_name")
    if ip_files and ip_rows:
        add("ip_mapping", "知识产权证明与关系表对应", "complete",
            f"已上传 {ip_files} 份证明，关系表识别 {ip_rows} 项。", "relation_table")
    elif ip_files or ip_rows:
        add("ip_mapping", "知识产权证明与关系表对应", "weak",
            "知识产权证明或关系表仅完成一侧，需逐项对应。", "relation_table")
    else:
        add("ip_mapping", "知识产权证明与关系表对应", "missing",
            "缺少知识产权证明和关系表记录。", "relation_table")

    for key, title in (
        ("rd_expense_audit", "研发费用专项审计报告"),
        ("annual_audit", "年度审计报告"),
        ("tax_settlement", "企业所得税汇算清缴文件"),
    ):
        missing_years = [year for year in finance_years if not _year_file_count(attachments, key, year)]
        if not missing_years:
            status = "complete"
            detail = f"{', '.join(finance_years)} 年份材料齐全。"
        else:
            status = "missing"
            detail = f"缺少年份：{', '.join(missing_years)}。"
        add(key, title, status, detail, "attachments")

    projects = _relation_entity_count(relation, "rd_code", "rd_activity")
    products = _relation_entity_count(relation, "ps_code", "ps_name")
    achievements = _relation_entity_count(relation, "result_no", "result_name")
    add("rd_relation", "RD-IP-PS 关系表", "complete" if projects and products else "missing",
        f"研发项目 {projects} 个、产品 {products} 个、成果 {achievements} 个。",
        "relation_table")
    add("staff_roster", "研发人员/科技人员名单", "complete" if data.get("hr_staff_rows") or data.get("staff_total") else "missing",
        "已存在人员明细或统计数字。" if (data.get("hr_staff_rows") or data.get("staff_total")) else "尚未录入人员名单。",
        "application")
    add("system_docs", "研发组织管理制度", "complete" if data.get("gaoxin_system_docs") else "weak",
        "已维护制度数据。" if data.get("gaoxin_system_docs") else "制度框架尚未完整维护。",
        "system_docs")

    return {
        "items": items,
        "complete": [item for item in items if item["status"] == "complete"],
        "weak": [item for item in items if item["status"] == "weak"],
        "missing": [item for item in items if item["status"] == "missing"],
    }


def _build_consistency(data: dict, attachments: dict, application_year: int) -> dict:
    conflicts = []
    warnings = []
    relation = _relation_rows(data)
    staff_total, tech_staff = _staff_counts(data)

    if staff_total is not None and tech_staff is not None and tech_staff > staff_total:
        conflicts.append({
            "id": "staff_ratio",
            "title": "科技人员数大于职工总数",
            "detail": f"科技人员 {tech_staff:g} 人，职工总数 {staff_total:g} 人。",
            "target": "application",
        })

    finance = _finance_totals(data, application_year)
    if finance["hitech_revenue"] is not None and finance["latest_revenue"] is not None:
        if finance["hitech_revenue"] > finance["latest_revenue"]:
            conflicts.append({
                "id": "hitech_revenue",
                "title": "高新收入大于企业总收入",
                "detail": f"{finance['hitech_revenue']:g} > {finance['latest_revenue']:g}。",
                "target": "application",
            })
    if finance["rd_domestic"] is not None and finance["rd_total"] is not None:
        if finance["rd_domestic"] > finance["rd_total"]:
            conflicts.append({
                "id": "domestic_rd",
                "title": "境内研发费用大于研发费用总额",
                "detail": f"{finance['rd_domestic']:g} > {finance['rd_total']:g}。",
                "target": "application",
            })

    rd_codes = _unique_relation_values(relation, "rd_code")
    for code in rd_codes:
        code_rows = [row for row in relation if _text(row.get("rd_code")) == code]
        names = _unique_relation_values(code_rows, "rd_activity")
        periods = _unique_relation_values(code_rows, "rd_period")
        if len(names) > 1:
            conflicts.append({
                "id": f"rd-name-{code}",
                "title": f"{code} 项目名称不一致",
                "detail": "；".join(names),
                "target": "relation_table",
            })
        if len(periods) > 1:
            conflicts.append({
                "id": f"rd-period-{code}",
                "title": f"{code} 项目周期不一致",
                "detail": "；".join(periods),
                "target": "relation_table",
            })

    ip_files = _file_count(attachments, "ip")
    ip_rows = _relation_entity_count(relation, "ip_code", "ip_auth_no", "ip_name")
    if ip_files and ip_rows and ip_files != ip_rows:
        warnings.append({
            "id": "ip-file-row-count",
            "title": "知识产权证明数量与关系表数量不一致",
            "detail": f"证明 {ip_files} 份，关系表 {ip_rows} 项，需人工确认是否一证多项或存在遗漏。",
            "target": "relation_table",
        })

    return {"conflicts": conflicts, "warnings": warnings}


def _build_score(score_result: dict | None) -> dict:
    if not isinstance(score_result, dict):
        return {
            "available": False,
            "conservative": None,
            "expected": None,
            "optimistic": None,
            "pass_score": 71,
            "passed": False,
            "label": "尚未评分",
        }
    total = _number(score_result.get("total_score")) or 0
    full = _number(score_result.get("full_score")) or 100
    pass_score = _number(score_result.get("pass_score")) or 71
    breakdown = score_result.get("breakdown") if isinstance(score_result.get("breakdown"), list) else []
    weak_categories = sum(
        1 for item in breakdown
        if isinstance(item, dict)
        and _number(item.get("score")) is not None
        and _number(item.get("max_score")) not in (None, 0)
        and _number(item.get("score")) < _number(item.get("max_score")) * 0.5
    )
    conservative = max(0, total - weak_categories * 2)
    optimistic = min(full, total + weak_categories * 2)
    return {
        "available": True,
        "conservative": round(conservative, 1),
        "expected": round(total, 1),
        "optimistic": round(optimistic, 1),
        "pass_score": round(pass_score, 1),
        "passed": total >= pass_score,
        "label": "评分达标" if total >= pass_score else "评分未达标",
    }


def score_result_from_record(record: Any) -> dict | None:
    """把旧的宽表评分记录转换为体检引擎可用的最小结果结构。"""
    if record is None:
        return None
    try:
        breakdown = record.breakdown_json
        if isinstance(breakdown, str):
            import json

            breakdown = json.loads(breakdown)
        if not isinstance(breakdown, list):
            breakdown = []
        score_type = _text(getattr(record, "score_type", ""))
        pass_score = 71 if score_type == "高新技术" else 50
        return {
            "rule_type": score_type,
            "total_score": _number(getattr(record, "total_score", 0)) or 0,
            "full_score": 100,
            "pass_score": pass_score,
            "breakdown": breakdown,
        }
    except (TypeError, ValueError, ImportError):
        return None


def run_health_check(
    company_data: dict | None,
    attachments: dict | None = None,
    score_result: dict | None = None,
    *,
    application_year: int | None = None,
) -> dict:
    data = company_data if isinstance(company_data, dict) else {}
    attachments = attachments if isinstance(attachments, dict) else {}
    try:
        year = int(application_year or data.get("application_year") or DEFAULT_APPLICATION_YEAR)
    except (TypeError, ValueError):
        year = DEFAULT_APPLICATION_YEAR

    qualification = _build_qualification(data, attachments, year)
    evidence = _build_evidence(data, attachments, year)
    consistency = _build_consistency(data, attachments, year)
    score = _build_score(score_result)

    export_blockers = []
    for item in qualification["failed"]:
        export_blockers.append({
            "id": item["id"],
            "title": item["title"],
            "reason": item["detail"],
            "target": item["target"],
        })
    for item in qualification["pending"]:
        if item["id"] in {
            "registered_one_year",
            "key_technology_field",
            "tech_staff_ratio",
            "rd_expense_ratio",
            "domestic_rd_ratio",
            "hitech_revenue_ratio",
            "no_major_violation",
        }:
            export_blockers.append({
                "id": f"pending-{item['id']}",
                "title": f"待核实：{item['title']}",
                "reason": item["detail"],
                "target": item["target"],
            })
    for item in evidence["missing"]:
        if item["id"] in {
            "application_pdf",
            "business_license",
            "hitech_income_audit",
            "rd_expense_audit",
            "annual_audit",
            "tax_settlement",
        }:
            export_blockers.append({
                "id": f"missing-{item['id']}",
                "title": f"缺少：{item['title']}",
                "reason": item["detail"],
                "target": item["target"],
            })
    for item in consistency["conflicts"]:
        export_blockers.append({
            "id": f"conflict-{item['id']}",
            "title": item["title"],
            "reason": item["detail"],
            "target": item["target"],
        })

    actions = []
    for blocker in export_blockers[:8]:
        actions.append({
            "priority": "P0",
            "title": blocker["title"],
            "reason": blocker["reason"],
            "target": blocker["target"],
        })
    for item in evidence["weak"][:4]:
        actions.append({
            "priority": "P1",
            "title": f"补强：{item['title']}",
            "reason": item["detail"],
            "target": item["target"],
        })
    for item in consistency["warnings"][:4]:
        actions.append({
            "priority": "P1",
            "title": item["title"],
            "reason": item["detail"],
            "target": item["target"],
        })

    qualification_count = max(1, len(qualification["items"]))
    evidence_count = max(1, len(evidence["items"]))
    qualification_score = (
        len(qualification["passed"]) / qualification_count * 100
        - len(qualification["failed"]) / qualification_count * 35
        - len(qualification["pending"]) / qualification_count * 15
    )
    evidence_score = (
        len(evidence["complete"]) / evidence_count * 100
        + len(evidence["weak"]) / evidence_count * 35
    )
    consistency_score = max(
        0,
        100 - len(consistency["conflicts"]) * 25 - len(consistency["warnings"]) * 8,
    )
    score_score = (
        min(100, (score["expected"] / score["pass_score"]) * 100)
        if score["available"] and score["pass_score"] > 0 else 0
    )
    readiness_score = round(max(0, min(
        100,
        qualification_score * 0.45
        + evidence_score * 0.30
        + consistency_score * 0.15
        + score_score * 0.10,
    )))

    if export_blockers:
        status = "blocked"
        status_label = "暂不可申报"
    elif qualification["pending"] or evidence["missing"] or evidence["weak"] or consistency["warnings"]:
        status = "warning"
        status_label = "存在待补强项"
    else:
        status = "ready"
        status_label = "资料基本齐备"

    return {
        "status": status,
        "status_label": status_label,
        "readiness_score": readiness_score,
        "application_year": year,
        "rule_version": RULE_VERSION,
        "qualification": qualification,
        "evidence": evidence,
        "consistency": consistency,
        "score": score,
        "actions": actions,
        "export_blockers": export_blockers,
    }
