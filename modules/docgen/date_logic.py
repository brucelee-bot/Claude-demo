import calendar
import os
import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo


DEFAULT_TIMEZONE = "Asia/Shanghai"

_DATE_TOKEN_RE = re.compile(
    r"(?<!\d)"
    r"(?P<year>20\d{2})"
    r"(?:\s*(?:年|[./\-])\s*(?P<month>0?[1-9]|1[0-2])"
    r"(?:\s*(?:月|[./\-])\s*(?P<day>0?[1-9]|[12]\d|3[01])\s*日?|\s*月)?)?"
    r"(?!\d)"
)


def system_today():
    """Return the business date in the configured application timezone."""
    timezone_name = os.getenv("APP_TIMEZONE", DEFAULT_TIMEZONE)
    try:
        timezone = ZoneInfo(timezone_name)
    except Exception:
        timezone = ZoneInfo(DEFAULT_TIMEZONE)
    return datetime.now(timezone).date()


def _coerce_date(value):
    if value is None:
        return system_today()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return datetime.strptime(str(value), "%Y-%m-%d").date()


def _parse_token(match, boundary):
    year = int(match.group("year"))
    month_text = match.group("month")
    day_text = match.group("day")
    precision = "year"
    month = 1 if boundary == "start" else 12
    day = 1 if boundary == "start" else 31

    if month_text:
        precision = "month"
        month = int(month_text)
        day = 1 if boundary == "start" else calendar.monthrange(year, month)[1]
    if day_text:
        precision = "day"
        day = int(day_text)

    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    return {"date": parsed, "precision": precision}


def _display_date(value, precision="day"):
    if not value:
        return ""
    if precision == "year":
        return f"{value.year}年"
    if precision == "month":
        return f"{value.year}年{value.month}月"
    return f"{value.year}年{value.month}月{value.day}日"


def parse_date_range(value):
    """Parse common Chinese project-period formats without inventing missing values."""
    raw = str(value or "").strip()
    open_ended = bool(re.search(r"(?:至今|迄今|目前|present|now)", raw, re.IGNORECASE))
    matches = list(_DATE_TOKEN_RE.finditer(raw))
    if not matches:
        return {
            "raw": raw,
            "valid": False,
            "open_ended": open_ended,
            "start": None,
            "end": None,
            "start_precision": "",
            "end_precision": "",
            "start_display": "",
            "end_display": "",
        }

    start_token = _parse_token(matches[0], "start")
    end_match = matches[-1] if len(matches) > 1 else matches[0]
    end_token = _parse_token(end_match, "end")
    if not start_token or not end_token or start_token["date"] > end_token["date"]:
        return {
            "raw": raw,
            "valid": False,
            "open_ended": open_ended,
            "start": None,
            "end": None,
            "start_precision": "",
            "end_precision": "",
            "start_display": "",
            "end_display": "",
        }

    return {
        "raw": raw,
        "valid": True,
        "open_ended": open_ended,
        "start": start_token["date"],
        "end": end_token["date"],
        "start_precision": start_token["precision"],
        "end_precision": end_token["precision"],
        "start_display": _display_date(start_token["date"], start_token["precision"]),
        "end_display": _display_date(end_token["date"], end_token["precision"]),
    }


def split_project_stages(start, end, as_of=None):
    """Split a known project period into chronological, non-overlapping stages."""
    if not start or not end or start > end:
        return []

    as_of_date = _coerce_date(as_of)
    definitions = [
        ("项目准备阶段", "需求调研、资料收集、可行性分析、任务分解和立项评审。"),
        ("设计开发阶段", "技术方案设计、关键模块开发、样品试制或系统联调。"),
        ("测试优化阶段", "功能测试、问题整改、参数优化、稳定性和适用性验证。"),
        ("总结验收阶段", "整理过程记录和成果资料，完成指标对照并按项目状态组织验收。"),
    ]
    total_days = (end - start).days + 1
    stage_count = min(len(definitions), total_days)
    definitions = definitions[:stage_count]
    weights = [0.18, 0.42, 0.25, 0.15][:stage_count]
    weight_total = sum(weights)
    normalized = [weight / weight_total for weight in weights]

    stages = []
    cursor = start
    allocated = 0
    for index, (name, content) in enumerate(definitions):
        if index == stage_count - 1:
            stage_end = end
        else:
            allocated += max(1, round(total_days * normalized[index]))
            latest_end = end - timedelta(days=stage_count - index - 1)
            stage_end = min(start + timedelta(days=allocated - 1), latest_end)

        if as_of_date < cursor:
            stage_status = "未到计划节点"
        elif as_of_date >= stage_end:
            stage_status = "已到计划节点"
        else:
            stage_status = "处于计划周期"

        stages.append({
            "index": index + 1,
            "name": name,
            "start_iso": cursor.isoformat(),
            "end_iso": stage_end.isoformat(),
            "time_range": f"{_display_date(cursor)}至{_display_date(stage_end)}",
            "content": content,
            "status": stage_status,
        })
        cursor = stage_end + timedelta(days=1)
    return stages


def project_temporal_context(
    period,
    as_of=None,
    *,
    completion_record_supported=False,
    acceptance_record_supported=False,
):
    """Return the single temporal truth used by AI prompts and generated documents."""
    as_of_date = _coerce_date(as_of)
    parsed = parse_date_range(period)
    start = parsed.get("start")
    end = parsed.get("end")
    open_ended = bool(parsed.get("open_ended"))

    if not parsed["valid"]:
        status = "待补充"
    elif open_ended and as_of_date >= start:
        status = "研发中"
    elif as_of_date < start:
        status = "计划中"
    elif as_of_date < end:
        status = "研发中"
    else:
        status = "已完成"

    date_eligible_for_acceptance = bool(
        parsed["valid"] and not open_ended and end and end <= as_of_date
    )
    completion_record_supported = bool(
        completion_record_supported and date_eligible_for_acceptance
    )
    acceptance_record_supported = bool(
        acceptance_record_supported and date_eligible_for_acceptance
    )
    status_display = "已到计划结束时间" if status == "已完成" else status
    issue_date = _display_date(start) if parsed["valid"] and start else "待补充"
    approval_date = issue_date
    period_end_date = (
        _display_date(end)
        if parsed["valid"] and not open_ended and end
        else ""
    )

    if date_eligible_for_acceptance:
        acceptance_title = "研发项目验收报告"
        acceptance_date = period_end_date or "待补充"
        acceptance_date_label = "验收日期"
        acceptance_signature_label = "日期"
        if acceptance_record_supported:
            acceptance_status = "已提供实际验收记录"
            acceptance_opinion = "验收意见应严格依据已提供的实际验收记录填写。"
            acceptance_result = "验收结论应严格依据已提供的实际验收记录填写。"
        else:
            acceptance_status = "已到计划结束时间，验收状态待核实"
            acceptance_opinion = (
                "项目已到计划结束时间，应对照立项目标、研发内容、过程记录、测试资料和成果材料组织验收。"
                "是否按计划完成以及资料是否归档完整，以实际验收记录为准。"
            )
            acceptance_result = "项目已具备组织验收的时间条件，实际验收结论及日期待根据验收记录填写。"
        tense_instruction = (
            "该项目已到计划结束时间，但仅到达结束日期不能证明项目已经完成、形成成果或验收合格；"
            "只有已提供的实际记录明确支持时，才可使用“已完成、已形成、已实现”等完成时表述，"
            "验收结论必须以正式验收记录和已提供材料为依据。"
        )
    else:
        acceptance_title = "研发项目阶段检查及待验收说明"
        acceptance_date = period_end_date or "待验收"
        acceptance_date_label = "计划验收日期" if period_end_date else "验收日期"
        acceptance_signature_label = "计划日期" if period_end_date else "日期"
        acceptance_status = "待验收"
        if status == "研发中":
            acceptance_opinion = "项目正在研发周期内推进，现阶段仅记录进展和阶段成果，项目结束后再组织正式验收。"
            tense_instruction = (
                "该项目仍在研发中，只能使用“正在开展、阶段形成、计划完成、拟形成”等表述；"
                "不得写“已完成、达到预期目标、同意验收、验收合格”。"
            )
        elif status == "计划中":
            acceptance_opinion = "项目尚未到计划开始时间，当前仅保留立项计划，待启动实施并完成后再组织验收。"
            tense_instruction = (
                "该项目尚未开始，只能使用“计划、拟开展、拟形成、预计”等表述；"
                "不得描述已经实施、完成、形成成果或通过验收。"
            )
        else:
            acceptance_opinion = "项目周期信息不完整，暂不能判断实施阶段或安排验收，相关日期和状态待补充。"
            tense_instruction = (
                "项目日期不完整，日期字段必须留空或写“待补充”；"
                "不得推断项目已开始、已完成、已形成成果或通过验收。"
            )
        acceptance_result = "项目尚未具备正式验收时间条件，验收结论待项目完成后填写。"

    period_display = parsed["raw"] or "待补充"
    end_display = "至今" if open_ended else (parsed["end_display"] or "待补充")
    return {
        "raw_period": parsed["raw"],
        "period_display": period_display,
        "date_valid": parsed["valid"],
        "start_iso": start.isoformat() if start else "",
        "end_iso": "" if open_ended else (end.isoformat() if end else ""),
        "start_display": parsed["start_display"] or "待补充",
        "end_display": end_display,
        "as_of_iso": as_of_date.isoformat(),
        "as_of_display": _display_date(as_of_date),
        "status": status,
        "status_display": status_display,
        "date_eligible_for_acceptance": date_eligible_for_acceptance,
        "can_accept": date_eligible_for_acceptance,
        "completion_record_supported": completion_record_supported,
        "acceptance_record_supported": acceptance_record_supported,
        "issue_date": issue_date,
        "approval_date": approval_date,
        "acceptance_date": acceptance_date,
        "acceptance_date_label": acceptance_date_label,
        "acceptance_signature_label": acceptance_signature_label,
        "acceptance_title": acceptance_title,
        "acceptance_status": acceptance_status,
        "acceptance_opinion": acceptance_opinion,
        "acceptance_result": acceptance_result,
        "tense_instruction": tense_instruction,
        "stages": (
            split_project_stages(start, end, as_of_date)
            if parsed["valid"] and not open_ended
            else []
        ),
    }


def event_date_context(value, as_of=None):
    """Classify a supplied event date without fabricating a missing date."""
    as_of_date = _coerce_date(as_of)
    parsed = parse_date_range(value)
    start = parsed.get("start")
    end = parsed.get("end")
    open_ended = bool(parsed.get("open_ended"))

    if not parsed["valid"]:
        status = "待补充"
        can_claim_occurred = False
    elif open_ended and start and start <= as_of_date:
        status = "日期范围进行中"
        can_claim_occurred = False
    elif start and start > as_of_date:
        status = "未来计划"
        can_claim_occurred = False
    elif end and end <= as_of_date:
        status = "已到日期"
        can_claim_occurred = True
    else:
        status = "日期范围进行中"
        can_claim_occurred = False

    return {
        "raw": parsed["raw"],
        "display": parsed["raw"] or "待补充",
        "date_valid": parsed["valid"],
        "open_ended": open_ended,
        "start_iso": start.isoformat() if start else "",
        "end_iso": end.isoformat() if end else "",
        "as_of_iso": as_of_date.isoformat(),
        "as_of_display": _display_date(as_of_date),
        "status": status,
        "is_future": bool(start and start > as_of_date),
        "can_claim_occurred": can_claim_occurred,
    }


_PRE_PROJECT_EVIDENCE_WORDS = (
    "研发项目立项申请",
    "产学研合作立项评审",
    "项目入驻申请",
)

_PROJECT_BOUND_EVIDENCE_WORDS = (
    "投入",
    "费用",
    "辅助账",
    "凭证",
    "分摊",
    "结转",
    "月度汇总",
    "年度复核",
    "例会",
    "会议",
    "活动记录",
    "过程记录",
    "资源开放使用",
    "成果",
    "转化",
    "试制",
    "试用",
    "应用",
    "测试",
    "验收",
    "输出",
    "效果",
    "奖励",
    "绩效",
    "晋升",
    "培训签到",
    "培训效果",
    "培养跟踪",
    "移交",
    "归档",
)


def evidence_event_type(file_title):
    """Classify a generated evidence form by its relationship to the project period."""
    title = str(file_title or "").strip()
    if any(word in title for word in _PRE_PROJECT_EVIDENCE_WORDS):
        return "pre_project"
    if any(word in title for word in _PROJECT_BOUND_EVIDENCE_WORDS):
        return "project_bound"
    return "general"


def evidence_record_date_context(file_title, record_date, project_period, as_of=None):
    """Validate whether a shared record date can be shown for one evidence form."""
    as_of_date = _coerce_date(as_of)
    event_type = evidence_event_type(file_title)
    record = event_date_context(record_date, as_of_date)
    project_range = parse_date_range(project_period)
    record_start = (
        date.fromisoformat(record["start_iso"])
        if record.get("start_iso")
        else None
    )
    project_start = project_range.get("start")
    before_project_start = bool(
        event_type == "project_bound"
        and record_start
        and project_start
        and record_start < project_start
    )
    usable = bool(
        record["date_valid"]
        and record["can_claim_occurred"]
        and not before_project_start
    )

    if not record["date_valid"]:
        display = "待根据实际记录填写"
        reason = "未提供实际记录日期"
    elif record["is_future"]:
        display = f"拟定日期：{record['raw']}（实际日期待填写）"
        reason = "记录日期尚未到达"
    elif before_project_start:
        display = "待根据实际记录填写"
        reason = "所填日期早于项目开始时间，不适用于该类记录"
    elif not record["can_claim_occurred"]:
        display = "待根据实际记录填写"
        reason = "日期范围尚未结束，不能作为已发生记录日期"
    else:
        display = record["raw"]
        reason = ""

    if usable and record_start:
        record_project = project_temporal_context(
            project_period,
            as_of=record_start,
        )
    else:
        record_project = project_temporal_context("", as_of=as_of_date)

    return {
        "event_type": event_type,
        "requires_project_start": event_type == "project_bound",
        "before_project_start": before_project_start,
        "usable": usable,
        "display": display,
        "reason": reason,
        "record": record,
        "record_project": record_project,
        "status_display": (
            record_project["status_display"]
            if usable
            else "待根据实际记录填写"
        ),
    }


def enforce_temporal_wording(
    text,
    temporal,
    *,
    completion_record_supported=None,
    acceptance_record_supported=None,
):
    """Downgrade claims that are not supported by dated completion or acceptance records."""
    content = str(text or "")
    if not content:
        return content

    status = temporal.get("status")
    if completion_record_supported is None:
        completion_record_supported = bool(temporal.get("completion_record_supported"))
    if acceptance_record_supported is None:
        acceptance_record_supported = bool(temporal.get("acceptance_record_supported"))

    if status == "计划中":
        replacements = [
            ("已经完成", "计划完成"),
            ("已完成", "计划完成"),
            ("完成了", "计划完成"),
            ("已经形成", "拟形成"),
            ("已形成", "拟形成"),
            ("形成了", "拟形成"),
            ("已经实现", "拟实现"),
            ("已实现", "拟实现"),
            ("实现了", "拟实现"),
            ("实现转化", "拟开展转化"),
            ("已经应用", "拟应用"),
            ("已应用", "拟应用"),
            ("已经使用", "拟使用"),
            ("已使用", "拟使用"),
            ("已经归档", "拟归档"),
            ("已归档", "拟归档"),
            ("提升了", "预计提升"),
        ]
    elif status == "研发中":
        replacements = [
            ("已经完成", "正在推进"),
            ("已完成", "正在推进"),
            ("完成了", "正在推进"),
            ("已经形成", "阶段形成"),
            ("已形成", "阶段形成"),
            ("形成了", "阶段形成"),
            ("已经实现", "正在推进实现"),
            ("已实现", "正在推进实现"),
            ("实现了", "正在推进实现"),
            ("实现转化", "正在开展转化准备"),
            ("已经应用", "正在开展应用验证"),
            ("已应用", "正在开展应用验证"),
            ("已经使用", "正在试用"),
            ("已使用", "正在试用"),
            ("已经归档", "正在整理归档资料"),
            ("已归档", "正在整理归档资料"),
            ("提升了", "正在验证提升效果"),
        ]
    elif not completion_record_supported:
        replacements = [
            ("已经完成", "实际完成情况待核实"),
            ("已完成", "实际完成情况待核实"),
            ("完成了", "实际完成情况待核实"),
            ("已经形成", "实际形成情况待核实"),
            ("已形成", "实际形成情况待核实"),
            ("形成了", "实际形成情况待核实"),
            ("已经实现", "实际实现情况待核实"),
            ("已实现", "实际实现情况待核实"),
            ("实现了", "实际实现情况待核实"),
            ("实现转化", "实际转化情况待核实"),
            ("已经应用", "实际应用情况待核实"),
            ("已应用", "实际应用情况待核实"),
            ("已经使用", "实际使用情况待核实"),
            ("已使用", "实际使用情况待核实"),
            ("已经归档", "实际归档情况待核实"),
            ("已归档", "实际归档情况待核实"),
            ("提升了", "相关提升效果待核实"),
        ]
    else:
        replacements = []

    for source, target in replacements:
        if source == "已完成":
            content = re.sub(r"(?<!未)(?<!尚未)已完成", target, content)
        else:
            content = content.replace(source, target)

    if status in {"计划中", "研发中"}:
        content = re.sub(r"(?<!拟)(?<!计划)应用于", "拟应用于", content)

    if not acceptance_record_supported:
        pending_result = temporal.get("acceptance_result") or "实际验收结论待根据正式记录填写。"
        content = re.sub(
            r"(?:该项目)?达到预期(?:的)?目标[，,。；;！!]*同意验收[！!。]?",
            pending_result,
            content,
        )
        content = re.sub(
            r"验收小组认为[^。；\n]*(?:开发|研发|项目)[^。；\n]*成功[^。；\n]*[。；]?",
            "项目实际完成及验收情况待根据正式验收记录核实。",
            content,
        )
        content = re.sub(
            r"该项目经[^。；\n]*(?:检测|检查|评审)[^。；\n]*符合[^。；\n]*(?:技术要求|验收要求|项目要求)[。；]?",
            "项目是否符合技术及验收要求，待根据实际检测和验收记录核实。",
            content,
        )
        content = re.sub(
            r"(?:同意验收|验收合格|验收通过|通过验收)[！!。；;]?",
            pending_result,
            content,
        )
        content = re.sub(
            r"(?<!是否)(?<!能否)达到预期(?:的)?目标",
            "是否达到预期目标待根据实际记录核实",
            content,
        )
    content = re.sub(
        r"(?m)^(验收时间\s*[：:])\s*.*$",
        rf"\1{temporal.get('acceptance_date') or '待验收'}",
        content,
    )
    return content


def enforce_transformation_wording(text, temporal, *, record_supported=False):
    """Remove unsupported claims that a research result was converted or applied."""
    content = enforce_temporal_wording(
        text,
        temporal,
        completion_record_supported=record_supported,
        acceptance_record_supported=False,
    )
    if not content or record_supported:
        return content

    status = temporal.get("status")
    content = content.replace("成果转化成功证明材料", "成果转化核验材料")
    content = re.sub(
        r"成果转化方式\s*[：:]\s*自行投资[，,、 ]*实施转化",
        "成果转化方式：实际实施情况待根据证明材料核实",
        content,
    )

    if status == "计划中":
        content = content.replace("实施转化", "拟开展转化")
        content = content.replace("转化成功", "计划转化情况")
        content = re.sub(r"并应用于", "并拟应用于", content)
    elif status == "研发中":
        content = content.replace("实施转化", "开展转化准备和验证")
        content = content.replace("转化成功", "阶段转化情况")
        content = re.sub(r"并应用于", "并拟应用于", content)
    else:
        content = content.replace("实施转化", "实际实施情况待核实")
        content = content.replace("转化成功", "转化情况待核实")
        content = re.sub(
            r"并应用于([^，。；\n]+)",
            r"并与\1的实际应用关系待核实",
            content,
        )
        content = re.sub(
            r"(?<!拟)应用于([^，。；\n]+)",
            r"与\1的实际应用关系待核实",
            content,
        )
        content = re.sub(
            r"(?:提升了|促进了)[^。；\n]+",
            "相关效果待根据实际证明材料核实",
            content,
        )

    return content
