import json
import os
import re
from pathlib import Path

from flask import jsonify, render_template, request, redirect, url_for, flash, send_file, session
from flask_login import login_required, current_user

from models import db, Company, ScoreRecord, ApplicationDraft
from modules.docgen import docgen_bp
from modules.docgen.generator import generate, TEMPLATE_PATH
from modules.docgen.generator_zhuanjing import generate_zhuanjing
from modules.docgen.relation_table_exporter import export_relation_table, import_relation_table
from modules.ai.analyzer import analyze
from modules.ai.llm_client import call_llm


def _upsert_application_draft(company, app_data, output_path):
    """同一公司只保留一条申报书记录；再次生成时更新原记录。"""
    draft = ApplicationDraft.query.filter_by(company_id=company.id).first()
    if not draft:
        draft = ApplicationDraft(company_id=company.id, app_type=company.app_type)
        db.session.add(draft)
    draft.app_type = company.app_type
    draft.sections_json = json.dumps(app_data, ensure_ascii=False)
    draft.docx_path = output_path
    return draft


def _load_company_data(company):
    if not company.data_json:
        return {}
    try:
        data = json.loads(company.data_json)
    except (json.JSONDecodeError, TypeError):
        return {}
    return data if isinstance(data, dict) else {}


def _load_high_tech_field_options():
    path = Path(__file__).resolve().parent / "high_tech_fields.json"
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return data if isinstance(data, list) else []


def _normalize_relation_rows(rows):
    normalized = []
    for raw in rows or []:
        row = {
            "year": str(raw.get("year", "")).strip(),
            "rd_code": str(raw.get("rd_code", "")).strip(),
            "rd_activity": str(raw.get("rd_activity", "")).strip(),
            "rd_period": str(raw.get("rd_period", "")).strip(),
            "ip_code": str(raw.get("ip_code", "")).strip(),
            "ip_name": str(raw.get("ip_name", "")).strip(),
            "ip_auth_no": str(raw.get("ip_auth_no", "")).strip(),
            "ps_code": str(raw.get("ps_code", "")).strip(),
            "ps_name": str(raw.get("ps_name", "")).strip(),
            "result_no": str(raw.get("result_no", "")).strip(),
            "result_name": str(raw.get("result_name", "")).strip(),
            "technology": str(raw.get("technology", "")).strip(),
        }
        if not any(row.values()):
            continue
        row["ps_display"] = "-".join(part for part in [row["ps_code"], row["ps_name"]] if part)
        normalized.append(row)
    return normalized


def _split_relation_values(value):
    return [item.strip() for item in re.split(r"[；;、,，\n]+", str(value or "")) if item.strip()]


def _validate_relation_rows(rows):
    errors = []
    ip_results = {}
    result_names = {}

    for index, row in enumerate(rows, start=1):
        label = f"第{index}行"
        if not row.get("year"):
            errors.append(f"{label}：请填写年份")
        if not row.get("rd_code") and not row.get("rd_activity"):
            errors.append(f"{label}：请填写 RD 序号或研发活动")
        if not row.get("ip_code") and not row.get("ip_auth_no"):
            errors.append(f"{label}：请填写相关知识产权或授权号")
        if not row.get("ps_code") and not row.get("ps_name"):
            errors.append(f"{label}：请填写对应 PS")
        if not row.get("result_no"):
            errors.append(f"{label}：请填写成果序号")
        if not row.get("result_name"):
            errors.append(f"{label}：请填写成果名称")

        ip_keys = _split_relation_values(row.get("ip_code")) or _split_relation_values(row.get("ip_auth_no"))
        result_key = row.get("result_no") or row.get("result_name")
        if ip_keys and result_key:
            for ip_key in ip_keys:
                old = ip_results.get(ip_key)
                if old and old != result_key:
                    errors.append(f"{label}：同一知识产权不能对应多个成果")
                ip_results[ip_key] = result_key

        result_no = row.get("result_no")
        result_name = row.get("result_name")
        if result_no and result_name:
            old_name = result_names.get(result_no)
            if old_name and old_name != result_name:
                errors.append(f"{label}：同一成果序号不能对应多个成果名称")
            result_names[result_no] = result_name

    if not rows:
        errors.append("请至少填写一行 RD-IP-PS-成果关联关系")
    return errors


def _relation_payload():
    if request.is_json:
        return request.get_json(silent=True) or {}
    rows_json = request.form.get("rows", "[]")
    try:
        rows = json.loads(rows_json)
    except (json.JSONDecodeError, TypeError):
        rows = []
    return {"rows": rows}


def _save_relation_table(company, rows, tech_field_path=""):
    data = _load_company_data(company)
    data["gaoxin_relation_table"] = {"rows": rows, "tech_field_path": str(tech_field_path or "").strip()}
    company.data_json = json.dumps(data, ensure_ascii=False)
    db.session.commit()


def _save_gaoxin_book_data(company, form_data):
    data = _load_company_data(company)
    relation_table = data.get("gaoxin_relation_table")
    data = dict(form_data)
    if relation_table:
        data["gaoxin_relation_table"] = relation_table
    company.data_json = json.dumps(data, ensure_ascii=False)
    db.session.commit()


def _relation_label(code, name, max_len=12):
    label = code or name
    if code and name:
        label = f"{code} - {name[:max_len]}{'…' if len(name) > max_len else ''}"
    return label


def _load_ip_details(company):
    ip_details = []
    try:
        from modules.parser.routes import _get_ip_certs
        ip_details = _get_ip_certs()
    except Exception:
        ip_details = []

    if not ip_details and company.ip_certs_json:
        try:
            ip_details = json.loads(company.ip_certs_json)
        except (json.JSONDecodeError, TypeError):
            ip_details = []

    return ip_details if isinstance(ip_details, list) else []


def _build_relation_ip_options(ip_details):
    options = []
    seen = set()
    for index, ip in enumerate(ip_details or [], start=1):
        if not isinstance(ip, dict):
            continue
        details = (ip.get("parsed") or {}).get("details") or {}
        name = str(details.get("name") or "").strip()
        auth_no = str(details.get("patent_no") or details.get("grant_no") or "").strip()
        app_date = str(details.get("app_date") or details.get("application_date") or details.get("apply_date") or "").strip()
        code = f"IP{str(index).zfill(2)}"
        key = auth_no or name or code
        if key in seen:
            continue
        seen.add(key)
        options.append({"code": code, "name": name, "auth_no": auth_no, "app_date": app_date})
    return options


def _strip_json_block(content):
    content = str(content or "").strip()
    if content.startswith("```"):
        lines = content.split("\n")
        content = "\n".join(lines[1:-1]).strip()
    return content


def _extract_json_object(content):
    decoder = json.JSONDecoder()
    text = _strip_json_block(content)
    try:
        return decoder.raw_decode(text)[0]
    except (json.JSONDecodeError, TypeError, ValueError):
        pass

    start = text.find("{")
    while start != -1:
        try:
            return decoder.raw_decode(text[start:])[0]
        except (json.JSONDecodeError, TypeError, ValueError):
            start = text.find("{", start + 1)
    return None


def _extract_labeled_value(content, labels, stop_labels):
    label_pattern = "|".join(re.escape(label) for label in labels)
    stop_pattern = "|".join(re.escape(label) for label in stop_labels)
    pattern = rf"(?:^|[\n\r\-*•\s])(?:{label_pattern})\s*[：:]\s*(.*?)(?=\n\s*(?:[-*•]\s*)?(?:{stop_pattern})\s*[：:]|$)"
    match = re.search(pattern, str(content or ""), re.S)
    return match.group(1).strip().strip('"“”') if match else ""


def _normalize_relation_value(value):
    if isinstance(value, list):
        return "；".join(str(item).strip() for item in value if str(item).strip())
    return str(value or "").strip()


def _merge_relation_result(base, incoming):
    if not base.get("result_name") and incoming.get("result_name"):
        base["result_name"] = incoming.get("result_name")
    if not base.get("technology") and incoming.get("technology"):
        base["technology"] = incoming.get("technology")
    return base


def _extract_relation_result_fields(data):
    result_keys = ["result_name", "resultName", "name", "title", "achievement", "achievement_name", "achievementName", "product_name", "productName", "成果名称", "科技成果名称", "科技成果", "成果", "名称", "专业名词"]
    technology_keys = ["technology", "technologies", "tech", "technical", "core_technology", "coreTechnology", "technical_details", "technicalDetails", "detail", "details", "description", "技术", "科技成果技术详情", "技术详情", "核心技术", "技术内容", "关键技术", "技术名称"]

    parsed = {"result_name": "", "technology": ""}

    if isinstance(data, str):
        parsed_json = _extract_json_object(data)
        if parsed_json is not None:
            _merge_relation_result(parsed, _extract_relation_result_fields(parsed_json))
        _merge_relation_result(parsed, {
            "result_name": _extract_labeled_value(data, result_keys, technology_keys),
            "technology": _extract_labeled_value(data, technology_keys, result_keys),
        })
        return parsed

    if isinstance(data, list):
        text_parts = []
        for item in data:
            if isinstance(item, str):
                text_parts.append(item)
            elif isinstance(item, dict):
                for text_key in ["text", "content", "output_text"]:
                    if isinstance(item.get(text_key), str):
                        text_parts.append(item.get(text_key))
            _merge_relation_result(parsed, _extract_relation_result_fields(item))
            if parsed.get("result_name") and parsed.get("technology"):
                return parsed
        if text_parts:
            _merge_relation_result(parsed, _extract_relation_result_fields("\n".join(text_parts)))
        return parsed

    if not isinstance(data, dict):
        return parsed

    for key in result_keys:
        if data.get(key):
            parsed["result_name"] = _normalize_relation_value(data.get(key))
            break
    for key in technology_keys:
        if data.get(key):
            parsed["technology"] = _normalize_relation_value(data.get(key))
            break
    if parsed.get("result_name") and parsed.get("technology"):
        return parsed

    for key in ["data", "result", "results", "items", "output", "outputs", "output_text", "content", "text", "answer", "message", "choices", "arguments", "function", "function_call", "tool_calls", "delta"]:
        if key in data:
            _merge_relation_result(parsed, _extract_relation_result_fields(data.get(key)))
            if parsed.get("result_name") and parsed.get("technology"):
                return parsed
    return parsed


def _fallback_relation_result(row):
    ip_name = str(row.get("ip_name") or "").strip()
    rd_activity = str(row.get("rd_activity") or "").strip()
    ps_name = str(row.get("ps_name") or "").strip()
    result_name = re.sub(r"^(一种|一种用于|一种基于|一种新型|一种实用型)", "", ip_name).strip(" ，,。") or ip_name
    technology_source = result_name or rd_activity or ps_name
    technology = f"{technology_source}关键技术" if technology_source else ""
    return {"result_name": result_name, "technology": technology}


def _parse_relation_result_content(content):
    parsed = _extract_relation_result_fields(content)
    if parsed.get("result_name") and parsed.get("technology"):
        return parsed

    data = _extract_json_object(content)
    _merge_relation_result(parsed, _extract_relation_result_fields(data))
    return parsed


RD_ACTIVITY_SUFFIX = "的研发"


def _ensure_rd_activity_suffix(name):
    name = str(name or "").strip().strip('"“” ，,。；;')
    if not name:
        return ""
    if name.endswith("的研发"):
        return name
    if name.endswith("研发"):
        name = name[:-2].rstrip("的")
    elif name.endswith("的"):
        return f"{name}研发"
    return f"{name}的研发"


def _normalize_rd_activity_name(name):
    base = str(name or "").strip().strip('"“” ，,。；;')
    if base.endswith(RD_ACTIVITY_SUFFIX):
        base = base[:-len(RD_ACTIVITY_SUFFIX)]
    elif base.endswith("研发"):
        base = base[:-2].rstrip("的")
    base = _clean_rd_activity_base(base) or "科技成果转化项目"
    return _ensure_rd_activity_suffix(base)


def _parse_rd_activity_content(content):
    data = _extract_json_object(content)
    for source in [data, content]:
        if isinstance(source, dict):
            for key in ["rd_activity", "activity_name", "name", "研发活动", "研发活动名称", "项目名称"]:
                if source.get(key):
                    return _normalize_rd_activity_name(source.get(key))
        if isinstance(source, str):
            value = _extract_labeled_value(source, ["rd_activity", "activity_name", "研发活动", "研发活动名称", "项目名称"], ["说明", "理由", "依据"])
            if value:
                return _normalize_rd_activity_name(value)
            cleaned = re.sub(r"^```(?:json)?|```$", "", source.strip(), flags=re.I).strip()
            if cleaned and "{" not in cleaned and len(cleaned) <= 80:
                return _normalize_rd_activity_name(cleaned)
    return ""


def _clean_relation_topic(value):
    text = str(value or "").strip()
    text = re.sub(r"^(一种|一种用于|一种基于|一种新型|一种实用型)", "", text)
    text = re.sub(r"(的研发|研发|关键技术|技术|方法|系统|装置|设备|工艺)$", "", text)
    text = text.strip(" ，,。；;")
    if not text or re.fullmatch(r"[\d\W_]+", text):
        return ""
    if len(text) <= 2 and not re.search(r"[一-鿿]{2,}", text):
        return ""
    if re.fullmatch(r"(RD|IP|PS)?\d+", text, re.I):
        return ""
    return text


def _clean_rd_activity_base(value):
    parts = [part for part in re.split(r"[与及、]+", str(value or "")) if _clean_relation_topic(part)]
    return "与".join(parts).strip("与及、 ，,。；;")

def _fallback_rd_activity(rows):
    texts = []
    for row in rows:
        for key in ["result_name", "ip_name", "technology", "ps_name"]:
            value = _clean_relation_topic(row.get(key))
            if value and value not in texts:
                texts.append(value)
    if not texts:
        return _normalize_rd_activity_name("科技成果转化项目")

    joined = "；".join(texts)
    domain_terms = [term for term in ["物联网", "卫星物联网", "光电传感", "数据安全", "网络安全", "信息安全", "数据分析", "预警", "漏洞扫描", "漏洞修复", "备份恢复", "安全审计", "智能检测", "防护", "监测", "控制", "管理", "识别"] if term in joined]
    if "卫星物联网" in domain_terms and "物联网" in domain_terms:
        domain_terms.remove("物联网")
    if len(domain_terms) >= 2:
        base = "".join(domain_terms[:3]) + "技术"
    else:
        first = texts[0]
        base = re.sub(r"(平台|系统|软件|装置|设备|产品|成果)$", "", first)
        base = f"{base}技术" if not base.endswith("技术") else base
    return _normalize_rd_activity_name(base)


def _generate_rd_activity(rows):
    rows = [row for row in rows or [] if isinstance(row, dict)]
    source_rows = [row for row in rows if str(row.get("ip_name") or row.get("result_name") or "").strip()]
    if not source_rows:
        return {"success": False, "error": "请先填写该项目下的知识产权名称或成果名称"}

    lines = []
    seen_lines = set()
    for row in source_rows:
        parts = []
        for label, key in [("专利名称", "ip_name"), ("成果名称", "result_name"), ("技术", "technology"), ("PS", "ps_name")]:
            value = str(row.get(key) or "").strip()
            if value:
                parts.append(f"{label}：{value}")
        line = "；".join(parts)
        if line and line not in seen_lines:
            seen_lines.add(line)
            lines.append(f"{len(lines) + 1}. {line}")

    prompt = f"""请根据同一个 RD 项目下的全部专利名称、成果名称和技术点，提炼一个上位的研发活动名称。

项目资料：
{chr(10).join(lines)}

要求：
1. 把所有 IP 名称和成果名称当作研发产出证据，反推能产出这些 IP/成果的共同研发方向。
2. 输出应是精简的上位技术项目名称，例如“物联网安全防护技术的研发”，而不是把多个 IP/成果名称直接拼接。
3. 不要直接复制第一个专利名称、单个成果名称，也不要用长串“与”连接多个平台/系统/软件名称。
4. 名称应体现核心技术方向，适合政府项目申报材料。
5. 名称最后必须以“的研发”结尾。
6. 只输出 JSON，不要 markdown，不要解释。

JSON 格式：
{{"rd_activity": "上位技术方向的研发"}}"""
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报顾问，擅长把同一研发项目下的多个知识产权名称和成果名称综合归纳为一个研发活动名称。输出必须是可解析 JSON，且必须以“的研发”结尾。"},
        {"role": "user", "content": prompt},
    ], temperature=0.2, max_tokens=300, timeout=45)
    fallback = _fallback_rd_activity(source_rows)
    if not result.get("success"):
        return {"success": True, "rd_activity": fallback}

    rd_activity = _parse_rd_activity_content(result.get("content")) or fallback
    single_names = {
        _ensure_rd_activity_suffix(_clean_relation_topic(row.get(key)))
        for row in source_rows
        for key in ["ip_name", "result_name"]
        if row.get(key)
    }
    if rd_activity in single_names and len(single_names) > 1:
        rd_activity = fallback
    if rd_activity.count("与") >= 2 or len(rd_activity) > 28:
        rd_activity = fallback
    return {"success": True, "rd_activity": _normalize_rd_activity_name(rd_activity)}


def _parse_ps_name_content(content):
    data = _extract_json_object(content)
    for source in [data, content]:
        if isinstance(source, dict):
            for key in ["ps_name", "product_service_name", "name", "PS名称", "产品服务名称", "产品名称", "服务名称"]:
                if source.get(key):
                    return str(source.get(key)).strip().strip('"“” ，,。；;')
        if isinstance(source, str):
            value = _extract_labeled_value(source, ["ps_name", "product_service_name", "PS名称", "产品服务名称", "产品名称", "服务名称"], ["说明", "理由", "依据"])
            if value:
                return value.strip().strip('"“” ，,。；;')
            cleaned = re.sub(r"^```(?:json)?|```$", "", source.strip(), flags=re.I).strip()
            if cleaned and "{" not in cleaned and len(cleaned) <= 80:
                return cleaned.strip('"“” ，,。；;')
    return ""


def _infer_business_type(company, company_data):
    text_parts = [company.name]
    for key in ["industry_code", "sub_industry_code", "main_product_name", "main_product_category", "company_overview", "business_scope", "industry_chain"]:
        value = company_data.get(key)
        if value:
            text_parts.append(str(value))
    text = " ".join(text_parts)
    if any(word in text for word in ["服务", "平台", "软件", "信息技术", "咨询", "运营", "设计", "广告"]):
        return "service"
    if any(word in text for word in ["制造", "生产", "加工", "设备", "材料", "产品", "纸箱", "部件", "装置"]):
        return "manufacturing"
    return "unknown"


def _fallback_ps_name(rows, business_type):
    names = []
    for row in rows:
        for key in ["result_name", "rd_activity", "technology", "ip_name"]:
            value = str(row.get(key) or "").strip()
            if value and value not in names:
                names.append(value)
    base = names[0] if names else "高新技术"
    base = re.sub(r"(的研发|研发)$", "", base).strip(" ，,。") or base
    return f"{base}服务" if business_type == "service" and not base.endswith("服务") else (base if business_type == "service" else base)


def _generate_ps_name(company, rows):
    rows = [row for row in rows or [] if isinstance(row, dict)]
    source_rows = [row for row in rows if str(row.get("rd_activity") or row.get("result_name") or row.get("technology") or row.get("ip_name") or "").strip()]
    if not source_rows:
        return {"success": False, "error": "请先生成或填写 RD 名称、成果名称或技术内容"}

    company_data = _load_company_data(company)
    business_type = _infer_business_type(company, company_data)
    type_hint = "服务业，PS名称应为“……服务”" if business_type == "service" else "制造业，PS名称应为一个产品名称" if business_type == "manufacturing" else "根据资料判断是服务还是产品，服务业以“服务”结尾，制造业使用产品名称"
    lines = []
    seen = set()
    for row in source_rows:
        line = "；".join(
            f"{label}：{value}" for label, key in [("RD名称", "rd_activity"), ("成果名称", "result_name"), ("技术", "technology"), ("知识产权", "ip_name")]
            for value in [str(row.get(key) or "").strip()] if value
        )
        if line and line not in seen:
            seen.add(line)
            lines.append(f"{len(lines) + 1}. {line}")

    prompt = f"""请根据企业所有 RD 名称、成果名称、技术内容和知识产权名称，总结一个统一的 PS 名称。

企业名称：{company.name}
行业判断：{type_hint}
资料：
{chr(10).join(lines)}

要求：
1. PS名称必须是一个总名称，能概括所有 RD 和成果，不要逐条罗列。
2. 如果是服务业，名称应为“……服务”；如果是制造业，名称应为一个产品名称。
3. 名称要专业、简洁，适合高新技术企业申报材料。
4. 只输出 JSON，不要 markdown，不要解释。

JSON 格式：
{{"ps_name": "PS名称"}}"""
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报顾问，擅长把研发项目和成果归纳为产品或服务名称。输出必须是可解析 JSON。"},
        {"role": "user", "content": prompt},
    ], temperature=0.2, max_tokens=300, timeout=45)
    fallback = _fallback_ps_name(source_rows, business_type)
    if not result.get("success"):
        return {"success": True, "ps_name": fallback}
    ps_name = _parse_ps_name_content(result.get("content")) or fallback
    if business_type == "service" and ps_name and not ps_name.endswith("服务"):
        ps_name = f"{ps_name}服务"
    return {"success": True, "ps_name": ps_name}


def _generate_relation_result(row):
    ip_name = str(row.get("ip_name") or "").strip()
    if not ip_name:
        return {"success": False, "error": "请先填写相关知识产权名称"}

    context = {
        "研发活动": str(row.get("rd_activity") or "").strip(),
        "研发周期": str(row.get("rd_period") or "").strip(),
        "相关知识产权名称": ip_name,
        "相关知识产权授权号": str(row.get("ip_auth_no") or "").strip(),
        "对应PS编号": str(row.get("ps_code") or "").strip(),
        "对应PS名称": str(row.get("ps_name") or "").strip(),
    }
    context_text = "\n".join(f"{key}：{value}" for key, value in context.items() if value)
    prompt = f"""请根据以下 RD-IP-PS 关系信息，生成高新技术企业申报材料中的科技成果信息。

{context_text}

生成要求：
1. 科技成果技术详情 technology：根据知识产权名称提炼其背后的核心技术，使用专业、申报材料风格表达，可用中文分号分隔 2-3 项技术。
2. 成果名称 result_name：把上述技术对应的成果总结成一个专业名词，通常是产品/工艺/系统/材料类名称，不要写成完整句子。
3. 不要虚构具体专利号、性能参数、检测数据、未给出的企业事实。
4. 只输出 JSON，不要 markdown，不要解释。

JSON 格式：
{{
  "result_name": "成果名称",
  "technology": "技术1；技术2"
}}"""
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报顾问，擅长把知识产权名称提炼为科技成果名称和核心技术。输出必须是可解析 JSON。"},
        {"role": "user", "content": prompt},
    ], temperature=0.2, max_tokens=800, timeout=45)
    if not result.get("success"):
        return {"success": False, "error": result.get("error") or "AI 生成失败"}

    data = _parse_relation_result_content(result.get("content"))
    fallback = _fallback_relation_result(row)

    result_name = str(data.get("result_name") or fallback.get("result_name") or "").strip()
    technology = str(data.get("technology") or fallback.get("technology") or "").strip()
    if not result_name or not technology:
        return {"success": False, "error": "AI 返回内容不完整，请重试"}
    return {"success": True, "result_name": result_name, "technology": technology}


def _merge_relation_fields(data):
    relation_table = data.get("gaoxin_relation_table") or {}
    relation = relation_table.get("rows") or []
    tech_field_path = str(relation_table.get("tech_field_path") or "").strip()
    if not relation:
        if tech_field_path:
            merged = dict(data)
            merged.setdefault("tech_field", tech_field_path)
            return merged
        return data

    merged = dict(data)
    if tech_field_path:
        merged.setdefault("tech_field", tech_field_path)
    rds = []
    ips = []
    pss = []
    seen_rd = set()
    seen_ip = set()
    seen_ps = set()

    for row in relation:
        rd_key = row.get("rd_code") or row.get("rd_activity")
        if rd_key and rd_key not in seen_rd:
            seen_rd.add(rd_key)
            rds.append(row)

        ip_key = row.get("ip_code") or row.get("ip_auth_no") or row.get("ip_name")
        if ip_key and ip_key not in seen_ip:
            seen_ip.add(ip_key)
            ips.append(row)

        ps_key = row.get("ps_code") or row.get("ps_name")
        if ps_key and ps_key not in seen_ps:
            seen_ps.add(ps_key)
            pss.append(row)

    relation_fields = {
        "_relation_rd_count": len(rds),
        "_relation_ip_count": len(ips),
        "_relation_ps_count": len(pss),
        "_relation_cv_count": len(relation),
    }

    for i, row in enumerate(rds):
        rd_code = row.get("rd_code") or f"RD{str(i + 1).zfill(2)}"
        relation_fields[f"rd_{i}_no"] = rd_code
        relation_fields[f"rd_{i}_name"] = row.get("rd_activity", "")
        relation_fields[f"rd_{i}_period"] = row.get("rd_period", "")
        ip_labels = []
        tech_values = []
        for rel in relation:
            if (rel.get("rd_code") or rel.get("rd_activity")) == (row.get("rd_code") or row.get("rd_activity")):
                ip_label = _relation_label(rel.get("ip_code", ""), rel.get("ip_name", ""), 15)
                if ip_label and ip_label not in ip_labels:
                    ip_labels.append(ip_label)
                if rel.get("technology") and rel.get("technology") not in tech_values:
                    tech_values.append(rel.get("technology"))
        relation_fields[f"rd_{i}_ip_no"] = ip_labels
        if tech_field_path:
            relation_fields[f"rd_{i}_field"] = tech_field_path
        elif tech_values:
            relation_fields[f"rd_{i}_field"] = "；".join(tech_values)

    for i, row in enumerate(ips):
        relation_fields[f"ip_{i}_seq"] = row.get("ip_code") or f"IP{str(i + 1).zfill(2)}"
        relation_fields[f"ip_{i}_name"] = row.get("ip_name", "")
        relation_fields[f"ip_{i}_patent_no"] = row.get("ip_auth_no", "")
        if row.get("ip_auth_no", "").upper().startswith("ZL"):
            relation_fields[f"ip_{i}_status"] = "授权"

    for i, row in enumerate(pss):
        ps_code = row.get("ps_code") or f"PS{str(i + 1).zfill(2)}"
        relation_fields[f"ps_{i}_no"] = ps_code
        relation_fields[f"ps_{i}_name"] = row.get("ps_name", "")
        if tech_field_path:
            relation_fields[f"ps_{i}_field"] = tech_field_path
        relation_fields[f"ps_{i}_is_main"] = "yes"
        rd_labels = []
        ip_labels = []
        for rel in relation:
            if (rel.get("ps_code") or rel.get("ps_name")) == (row.get("ps_code") or row.get("ps_name")):
                rd_label = _relation_label(rel.get("rd_code", ""), rel.get("rd_activity", ""))
                if rd_label and rd_label not in rd_labels:
                    rd_labels.append(rd_label)
                ip_label = _relation_label(rel.get("ip_code", ""), rel.get("ip_name", ""), 15)
                if ip_label and ip_label not in ip_labels:
                    ip_labels.append(ip_label)
        relation_fields[f"ps_{i}_rds"] = rd_labels
        relation_fields[f"ps_{i}_ip_no"] = ip_labels

    for i, row in enumerate(relation):
        relation_fields[f"cv_{i}_rd"] = _relation_label(row.get("rd_code", ""), row.get("rd_activity", ""))
        relation_fields[f"cv_{i}_ip"] = _relation_label(row.get("ip_code", ""), row.get("ip_name", ""), 15)
        relation_fields[f"cv_{i}_ps"] = _relation_label(row.get("ps_code", ""), row.get("ps_name", ""))
        result_name = row.get("result_name", "")
        if result_name:
            relation_fields[f"cv_{i}_result_name"] = result_name
            relation_fields[f"cv_{i}_desc"] = f"{result_name}由{row.get('rd_code', '相关RD项目')}研发形成，并通过{row.get('ip_code') or row.get('ip_name') or '相关知识产权'}支撑{row.get('ps_code') or row.get('ps_name') or '相关PS产品'}转化。"

    for key, value in relation_fields.items():
        if value in (None, "", []):
            continue
        if re.match(r"^cv_\d+_desc$", key) and merged.get(key):
            continue
        merged[key] = value
    return merged


@docgen_bp.route("/", methods=["GET"])
@login_required
def index():
    all_records = (
        ScoreRecord.query
        .join(Company)
        .filter(Company.user_id == current_user.id)
        .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
        .all()
    )
    companies = []
    seen = set()
    for record in all_records:
        key = record.company.name
        if key in seen:
            continue
        seen.add(key)
        record.company.latest_score = record
        companies.append(record.company)
        if len(companies) >= 20:
            break
    return render_template("application_index.html", companies=companies)


@docgen_bp.route("/fill/<int:company_id>", methods=["GET", "POST"])
@login_required
def fill(company_id):
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    latest_score = (
        ScoreRecord.query
        .filter_by(company_id=company.id)
        .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
        .first()
    )
    is_gaoxin = company.app_type == "高新技术" or (latest_score and latest_score.score_type == "高新技术")
    if request.method == "GET" and is_gaoxin:
        return redirect(url_for("docgen.gaoxin_relation_table", company_id=company.id))

    score_data = {}
    if company.scores:
        score_data = json.loads(company.data_json or "{}")

    if request.method == "POST":
        form = request.form
        if company.app_type == "专精特新":
            app_data = _build_zhuanjing_data(company, form)
        else:
            app_data = _build_app_data(company, form)
        app_data.update(score_data)

        try:
            if company.app_type == "专精特新":
                output_path = generate_zhuanjing(app_data)
            else:
                output_path = generate(app_data)
        except Exception as e:
            flash(f"生成失败: {e}", "error")
            tpl = "application_zhuanjing_form.html" if company.app_type == "专精特新" else "application_form.html"
            return render_template(tpl, company=company, score_data=score_data)

        draft = _upsert_application_draft(company, app_data, output_path)
        db.session.commit()
        flash("申报书生成成功！", "success")
        return redirect(url_for("docgen.download", draft_id=draft.id))

    tpl = "application_zhuanjing_form.html" if company.app_type == "专精特新" else "application_form.html"
    return render_template(tpl, company=company, score_data=score_data)


@docgen_bp.route("/download/<int:draft_id>")
@login_required
def download(draft_id):
    draft = ApplicationDraft.query.get_or_404(draft_id)
    company = Company.query.get(draft.company_id)
    if company.user_id != current_user.id:
        flash("无权访问", "error")
        return redirect(url_for("docgen.index"))
    if not draft.docx_path or not os.path.exists(draft.docx_path):
        flash("文件不存在", "error")
        return redirect(url_for("docgen.index"))
    suffix = "专精特新中小企业申请书" if draft.app_type == "专精特新" else "高新技术企业认定申请书"
    return send_file(draft.docx_path, as_attachment=True, download_name=f"{company.name}_{suffix}.docx")


@docgen_bp.route("/history", methods=["GET"])
@login_required
def history():
    drafts = (
        ApplicationDraft.query
        .join(Company)
        .filter(Company.user_id == current_user.id)
        .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
        .all()
    )
    return render_template("application_history.html", drafts=drafts)


@docgen_bp.route("/assistant", methods=["GET"])
@login_required
def assistant():
    """项目申报助手总览页"""
    app_type = request.args.get("type", "高新技术")

    companies = (
        Company.query
        .filter(Company.user_id == current_user.id)
        .order_by(Company.created_at.desc())
        .all()
    )

    score_cards = []
    recent_scores = []
    for company in companies:
        latest_score = (
            ScoreRecord.query
            .filter_by(company_id=company.id)
            .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
            .first()
        )
        latest_draft = (
            ApplicationDraft.query
            .filter_by(company_id=company.id)
            .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
            .first()
        )
        score_cards.append({
            "company": company,
            "score": latest_score,
            "draft": latest_draft,
        })
        if latest_score:
            recent_scores.append(latest_score)

    total_companies = len(companies)
    total_scores = len(recent_scores)
    total_drafts = ApplicationDraft.query.join(Company).filter(Company.user_id == current_user.id).count()

    if recent_scores:
        avg_score = round(sum(s.total_score or 0 for s in recent_scores) / len(recent_scores), 1)
        pass_count = sum(1 for s in recent_scores if (s.total_score or 0) >= (71 if s.score_type == "高新技术" else 50))
    else:
        avg_score = 0
        pass_count = 0

    selected_company = None
    selected_score = None
    selected_analysis = None
    selected_draft = None
    selected_recommendations = []
    selected_warnings = []
    company_id = request.args.get("company_id", type=int)
    if company_id:
        selected_company = Company.query.filter_by(id=company_id, user_id=current_user.id).first()
    elif companies:
        selected_company = companies[0]

    if selected_company:
        selected_score = (
            ScoreRecord.query
            .filter_by(company_id=selected_company.id)
            .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
            .first()
        )
        selected_draft = (
            ApplicationDraft.query
            .filter_by(company_id=selected_company.id)
            .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
            .first()
        )
        if selected_score:
            try:
                selected_analysis = json.loads(selected_score.ai_analysis) if selected_score.ai_analysis else None
            except (json.JSONDecodeError, TypeError):
                selected_analysis = None
            if not selected_analysis:
                selected_analysis = analyze(
                    {
                        "rule_type": selected_score.score_type,
                        "total_score": selected_score.total_score,
                        "full_score": 100,
                        "pass_score": 71 if selected_score.score_type == "高新技术" else (60 if selected_score.score_type == "小巨人" else 50),
                        "passed": selected_score.total_score >= (71 if selected_score.score_type == "高新技术" else (60 if selected_score.score_type == "小巨人" else 50)),
                        "breakdown": json.loads(selected_score.breakdown_json or "[]"),
                        "warnings": [],
                    },
                    json.loads(selected_company.data_json or "{}") if selected_company.data_json else None,
                )
            selected_recommendations = selected_analysis.get("recommendations", [])[:3]
            selected_warnings = [w for w in (selected_score.breakdown_json and [])]

    if not selected_recommendations:
        selected_recommendations = ["先完成一次评分，再查看针对性的申报建议。"]

    return render_template(
        "assistant.html",
        app_type=app_type,
        companies=companies,
        score_cards=score_cards,
        total_companies=total_companies,
        total_scores=total_scores,
        total_drafts=total_drafts,
        avg_score=avg_score,
        pass_count=pass_count,
        selected_company=selected_company,
        selected_score=selected_score,
        selected_analysis=selected_analysis,
        selected_draft=selected_draft,
        selected_recommendations=selected_recommendations,
        selected_warnings=selected_warnings,
    )


@docgen_bp.route("/assistant/brief", methods=["GET"])
@login_required
def assistant_brief():
    """项目申报助手建议页"""
    company_id = request.args.get("company_id", type=int)
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first() if company_id else None
    if not company:
        company = (
            Company.query
            .filter(Company.user_id == current_user.id)
            .order_by(Company.created_at.desc())
            .first()
        )
    if not company:
        flash("请先添加企业或完成评分", "error")
        return redirect(url_for("docgen.assistant"))

    score = (
        ScoreRecord.query
        .filter_by(company_id=company.id)
        .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
        .first()
    )
    draft = (
        ApplicationDraft.query
        .filter_by(company_id=company.id)
        .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
        .first()
    )

    data = json.loads(company.data_json or "{}") if company.data_json else {}
    analysis = None
    if score:
        try:
            analysis = json.loads(score.ai_analysis) if score.ai_analysis else None
        except (json.JSONDecodeError, TypeError):
            analysis = None
        if not analysis:
            analysis = analyze(
                {
                    "rule_type": score.score_type,
                    "total_score": score.total_score,
                    "full_score": 100,
                    "pass_score": 71 if score.score_type == "高新技术" else (60 if score.score_type == "小巨人" else 50),
                    "passed": score.total_score >= (71 if score.score_type == "高新技术" else (60 if score.score_type == "小巨人" else 50)),
                    "breakdown": json.loads(score.breakdown_json or "[]"),
                    "warnings": [],
                },
                data,
            )
    else:
        analysis = {
            "overall": "尚未评分，建议先完成企业评分，再进入申报书填报。",
            "strengths": [],
            "weaknesses": [],
            "recommendations": ["先补齐企业基础信息、知识产权和财务数据。"],
            "priority": "先评分，再填报",
            "risk_level": "未知",
        }

    suggestions = analysis.get("recommendations", [])[:5]
    if not suggestions:
        suggestions = ["先完成评分，再生成针对性的申报建议。"]

    return render_template(
        "assistant_brief.html",
        company=company,
        score=score,
        draft=draft,
        analysis=analysis,
        suggestions=suggestions,
        data=data,
    )


def _indexed_form_count(form, prefix: str, suffix: str, minimum: int = 0) -> int:
    max_index = -1
    marker = f"_{suffix}"
    for key in form.keys():
        if not key.startswith(prefix) or not key.endswith(marker):
            continue
        raw = key[len(prefix):-len(marker)]
        if raw.isdigit():
            max_index = max(max_index, int(raw))
    return max(minimum, max_index + 1)


def _build_app_data(company, form):
    data = {"company_name": company.name, "province": form.get("province", ""),
            "city": form.get("city", ""), "tech_field": form.get("tech_field", "")}
    ip_list = []
    for i in range(_indexed_form_count(form, "ip_", "name", 10)):
        name = (form.get(f"ip_{i}_name") or form.get(f"ip_name_{i}") or "").strip()
        if name:
            patent_no = (
                form.get(f"ip_{i}_patent_no", "")
                or form.get(f"ip_{i}_no", "")
                or form.get(f"ip_{i}_auth_no", "")
                or form.get(f"ip_auth_no_{i}", "")
            )
            ip_list.append({
                "seq": form.get(f"ip_{i}_seq", "") or str(i + 1),
                "name": name,
                "type": form.get(f"ip_{i}_type", "") or form.get(f"ip_type_{i}", ""),
                "status": form.get(f"ip_{i}_status", ""),
                "patent_no": patent_no,
                "app_date": form.get(f"ip_{i}_app_date", ""),
                "applicant": form.get(f"ip_{i}_applicant", ""),
                "grant_date": form.get(f"ip_{i}_date", "") or form.get(f"ip_date_{i}", ""),
                "auth_no": patent_no,
                "date": form.get(f"ip_{i}_date", "") or form.get(f"ip_date_{i}", ""),
                "method": form.get(f"ip_{i}_method", "") or form.get(f"ip_method_{i}", "") or "自主研发",
            })
    data["ip_list"] = ip_list
    data["ip_class1_count"] = int(form.get("ip_class1_count", 0))
    data["ip_class2_count"] = int(form.get("ip_class2_count", 0))
    data["staff_total"] = int(form.get("staff_total", 0))
    data["tech_staff"] = int(form.get("tech_staff", 0))
    data["hr_detail"] = {
        "onjob": int(form.get("hr_onjob", 0)), "parttime": int(form.get("hr_parttime", 0)),
        "temp": int(form.get("hr_temp", 0)), "foreign": int(form.get("hr_foreign", 0)),
        "returnee": int(form.get("hr_returnee", 0)), "talent_plan": int(form.get("hr_talent_plan", 0)),
    }
    data["hr_edu"] = {"博士": int(form.get("hr_phd", 0)), "硕士": int(form.get("hr_master", 0)),
                      "本科": int(form.get("hr_bachelor", 0)), "大专及以下": int(form.get("hr_college", 0))}
    data["hr_title"] = {"高级职称": int(form.get("hr_title_senior", 0)), "中级职称": int(form.get("hr_title_mid", 0)),
                        "初级职称": int(form.get("hr_title_junior", 0)), "高级技工": int(form.get("hr_title_tech", 0))}
    data["hr_age"] = {"30及以下": int(form.get("hr_age_30", 0)), "31-40": int(form.get("hr_age_40", 0)),
                      "41-50": int(form.get("hr_age_50", 0)), "51及以上": int(form.get("hr_age_51", 0))}
    data["year1"] = form.get("fin_year1", ""); data["year2"] = form.get("fin_year2", ""); data["year3"] = form.get("fin_year3", "")
    for field in ["net_assets", "sales", "profit"]:
        for y in ["year1", "year2", "year3"]:
            data[f"{y}_{field}"] = float(form.get(f"fin_{y}_{field}", 0) or 0)
    data["rd_total_3y"] = float(form.get("fin_rd_total", 0) or 0)
    data["rd_domestic"] = float(form.get("fin_rd_domestic", 0) or 0)
    data["rd_basic"] = float(form.get("fin_rd_basic", 0) or 0)
    data["revenue_1y"] = float(form.get("fin_revenue", 0) or 0)
    data["hitech_revenue_1y"] = float(form.get("fin_hitech_revenue", 0) or 0)
    data["no_violation"] = form.get("no_violation", "□否")
    for f in ["innovation_ip_role", "innovation_transform", "innovation_rd_mgmt", "innovation_talent"]:
        data[f] = form.get(f, "")
    standards = []
    for i in range(3):
        name = (form.get(f"std_{i}_name") or form.get(f"std_name_{i}") or "").strip()
        if name:
            standards.append({
                "name": name,
                "no": form.get(f"std_{i}_no", "") or form.get(f"std_no_{i}", ""),
                "level": form.get(f"std_{i}_level", "") or form.get(f"std_level_{i}", ""),
                "role": form.get(f"std_{i}_role", "") or form.get(f"std_role_{i}", ""),
            })
    data["standards"] = standards
    return data


def _build_zhuanjing_data(company, form):
    data = {"company_name": company.name, "province": form.get("province", ""),
            "city": form.get("city", ""), "address": form.get("address", ""),
            "zipcode": form.get("zipcode", ""), "legal_rep": form.get("legal_rep", ""),
            "shareholder": form.get("shareholder", ""), "actual_controller": form.get("actual_controller", ""),
            "controller_nationality": form.get("controller_nationality", ""),
            "contact": form.get("contact", ""), "phone": form.get("phone", ""),
            "mobile": form.get("mobile", ""), "fax": form.get("fax", ""),
            "email": form.get("email", ""), "register_date": form.get("register_date", ""),
            "register_capital": form.get("register_capital", ""), "credit_code": form.get("credit_code", ""),
            "industry_code": form.get("industry_code", ""), "sub_industry_code": form.get("sub_industry_code", "")}
    fin_fields = ["employees", "rd_staff", "revenue", "main_revenue", "revenue_growth",
                  "cost", "main_cost", "product_cost", "sales_expense", "admin_expense",
                  "profit", "net_profit", "net_profit_growth", "assets", "net_assets",
                  "liabilities", "debt_ratio", "tax", "equity_financing", "valuation",
                  "bank_loan", "domestic_bond", "foreign_bond"]
    for field in fin_fields:
        for yr in ["2023", "2024", "2025"]:
            data[f"fin_{yr}_{field}"] = form.get(f"fin_{yr}_{field}", "")
    for yr in ["2023", "2024", "2025"]:
        data[f"fin_{yr}_rd_expense"] = form.get(f"fin_{yr}_rd_expense", "")
    data["audit_report_code"] = form.get("audit_report_code", "")
    data["market_years"] = form.get("market_years", "")
    data["main_revenue_ratio"] = form.get("main_revenue_ratio", "")
    data["revenue_cagr"] = form.get("revenue_cagr", "")
    for i in range(3):
        name = form.get(f"product_{i}_name", "").strip()
        rev = form.get(f"product_{i}_revenue", "").strip()
        if name or rev:
            data[f"product_{i}"] = {"name": name, "revenue": rev}
    data["std_international"] = form.get("std_international", "")
    data["std_national"] = form.get("std_national", "")
    data["std_industry"] = form.get("std_industry", "")
    data["std_names"] = form.get("std_names", "")
    rd = {}
    for inst in ["tech_academy", "tech_center", "eng_center", "design_center", "key_lab"]:
        rd[inst] = {"national": form.get(f"rd_inst_{inst}_national", ""),
                    "province": form.get(f"rd_inst_{inst}_province", ""),
                    "self_built": form.get(f"rd_inst_{inst}_self", "")}
    data["rd_institutions"] = rd
    data["has_academician_station"] = bool(form.get("has_academician_station"))
    data["has_postdoc_station"] = bool(form.get("has_postdoc_station"))
    data["partner_schools"] = form.get("partner_schools", "")
    for yr in ["2023", "2024", "2025"]:
        data[f"rd_ratio_{yr}"] = form.get(f"rd_ratio_{yr}", "")
        data[f"rd_staff_ratio_{yr}"] = form.get(f"rd_staff_ratio_{yr}", "")
    for f in ["market_position_desc", "export_amount", "brand_count", "brand_revenue",
              "industry_chain", "main_product_name", "main_product_category", "company_overview"]:
        data[f] = form.get(f, "")
    return data


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>", methods=["GET", "POST"])
@login_required
def gaoxin_relation_table(company_id):
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()

    if request.method == "POST":
        payload = _relation_payload()
        rows = _normalize_relation_rows(payload.get("rows", []))
        errors = _validate_relation_rows(rows)
        if errors:
            return jsonify({"ok": False, "errors": errors}), 400
        _save_relation_table(company, rows, payload.get("tech_field_path", ""))
        return jsonify({"ok": True})

    data = _load_company_data(company)
    relation_table = data.get("gaoxin_relation_table") or {}
    rows = relation_table.get("rows") or []
    tech_field_path = relation_table.get("tech_field_path", "") or next((str(row.get("tech_field_path") or "").strip() for row in rows if str(row.get("tech_field_path") or "").strip()), "")

    ip_details = _load_ip_details(company)
    ip_options = _build_relation_ip_options(ip_details)
    tech_field_options = _load_high_tech_field_options()

    return render_template(
        "application_gaoxin_relation_table.html",
        company=company,
        rows=rows,
        ip_details=ip_details,
        ip_options=ip_options,
        tech_field_options=tech_field_options,
        tech_field_path=tech_field_path,
    )


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>/generate_result", methods=["POST"])
@login_required
def gaoxin_relation_table_generate_result(company_id):
    Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    payload = request.get_json(silent=True) or {}
    generated = _generate_relation_result(payload.get("row") or {})
    if not generated.get("success"):
        return jsonify({"ok": False, "errors": [generated.get("error") or "AI 生成失败"]}), 400
    return jsonify({"ok": True, "result_name": generated["result_name"], "technology": generated["technology"]})


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>/generate_rd_activity", methods=["POST"])
@login_required
def gaoxin_relation_table_generate_rd_activity(company_id):
    Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    payload = request.get_json(silent=True) or {}
    generated = _generate_rd_activity(payload.get("rows") or [])
    if not generated.get("success"):
        return jsonify({"ok": False, "errors": [generated.get("error") or "AI 生成失败"]}), 400
    return jsonify({"ok": True, "rd_activity": generated["rd_activity"]})


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>/generate_ps_name", methods=["POST"])
@login_required
def gaoxin_relation_table_generate_ps_name(company_id):
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    payload = request.get_json(silent=True) or {}
    generated = _generate_ps_name(company, payload.get("rows") or [])
    if not generated.get("success"):
        return jsonify({"ok": False, "errors": [generated.get("error") or "AI 生成失败"]}), 400
    return jsonify({"ok": True, "ps_name": generated["ps_name"]})


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>/import", methods=["POST"])
@login_required
def gaoxin_relation_table_import(company_id):
    Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    upload = request.files.get("file")
    if not upload or not upload.filename:
        return jsonify({"ok": False, "errors": ["请先选择要上传的 Excel 文件"]}), 400
    if not upload.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"ok": False, "errors": ["请上传 .xlsx 或 .xlsm 格式的 Excel 文件"]}), 400

    try:
        imported_rows = import_relation_table(upload)
        tech_field_path = next((str(row.get("tech_field_path") or "").strip() for row in imported_rows if str(row.get("tech_field_path") or "").strip()), "")
        rows = _normalize_relation_rows(imported_rows)
    except Exception as exc:
        return jsonify({"ok": False, "errors": [f"Excel 识别失败：{exc}"]}), 400

    if not rows:
        return jsonify({"ok": False, "errors": ["未从 Excel 中识别到有效数据行"]}), 400

    return jsonify({"ok": True, "rows": rows, "tech_field_path": tech_field_path, "validation_errors": _validate_relation_rows(rows)})


@docgen_bp.route("/gaoxin_relation_table/<int:company_id>/export", methods=["POST"])
@login_required
def gaoxin_relation_table_export(company_id):
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    payload = _relation_payload()
    rows = _normalize_relation_rows(payload.get("rows", []))
    errors = _validate_relation_rows(rows)
    if errors:
        return jsonify({"ok": False, "errors": errors}), 400

    _save_relation_table(company, rows, payload.get("tech_field_path", ""))
    stream = export_relation_table(rows, payload.get("tech_field_path", ""))
    return send_file(
        stream,
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name=f"{company.name}_RD-IP-PS-成果关联详情表.xlsx",
    )


@docgen_bp.route("/gaoxin_book/<int:company_id>", methods=["GET", "POST"])
@login_required
def gaoxin_book(company_id):
    """高新技术企业认定申请书 — 官方格式网页版"""
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()

    if request.method == "POST":
        form_data = request.form.to_dict()
        _save_gaoxin_book_data(company, form_data)
        flash("申请书已保存", "success")
        return redirect(url_for("docgen.gaoxin_book", company_id=company.id))

    auto_data = {}
    if company.data_json:
        try:
            auto_data = json.loads(company.data_json)
        except (json.JSONDecodeError, TypeError):
            pass

    auto_data = _merge_relation_fields(auto_data)

    if "last_finance_data" in session:
        auto_data.update(session["last_finance_data"])
    
    # 获取 IP 明细 — 优先从 DB 加载
    ip_details = session.get("ip_certificates", [])
    if not ip_details and company.ip_certs_json:
        try:
            ip_details = json.loads(company.ip_certs_json)
            session["ip_certificates"] = ip_details
        except (json.JSONDecodeError, TypeError):
            pass
    
    return render_template("application_gaoxin_book.html", 
                          company=company, auto_data=auto_data, 
                          ip_details=ip_details)


@docgen_bp.route("/gaoxin_book/<int:company_id>/pdf")
@login_required
def gaoxin_book_pdf(company_id):
    """导出高新技术企业认定申请书 PDF"""
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    
    data = {}
    if company.data_json:
        try:
            data = json.loads(company.data_json)
        except (json.JSONDecodeError, TypeError):
            pass
    if "last_finance_data" in session:
        data.update(session["last_finance_data"])
    
    html = render_template("application_gaoxin_print.html", company=company, data=data)
    
    import tempfile, subprocess, os
    with tempfile.NamedTemporaryFile(suffix=".html", mode="w", encoding="utf-8", delete=False) as f:
        f.write(html)
        html_path = f.name
    
    pdf_path = html_path.replace(".html", ".pdf")
    
    try:
        chrome = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
        subprocess.run([
            chrome, "--headless", "--disable-gpu", "--no-sandbox",
            "--print-to-pdf=" + pdf_path,
            "--no-pdf-header-footer",
            "--virtual-time-budget=15000",
            html_path
        ], timeout=30, check=True, capture_output=True)
        
        from flask import send_file
        return send_file(pdf_path, mimetype="application/pdf",
                        as_attachment=True,
                        download_name=f"高新技术企业认定申请书_{company.name}.pdf")
    except Exception as e:
        return f"PDF生成失败: {str(e)}", 500
    finally:
        for p in [html_path, pdf_path]:
            try:
                if os.path.exists(p):
                    os.remove(p)
            except OSError:
                pass


@docgen_bp.route("/gaoxin_book/<int:company_id>/word", methods=["GET", "POST"])
@login_required
def gaoxin_book_word(company_id):
    """导出高新技术企业认定申请书 Word"""
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    
    # 如果是 POST 请求，先保存表单数据
    if request.method == "POST":
        form_data = request.form.to_dict()
        _save_gaoxin_book_data(company, form_data)
    
    from docx import Document
    from docx.shared import Pt, Cm, RGBColor
    from docx.enum.text import WD_ALIGN_PARAGRAPH
    from docx.enum.table import WD_TABLE_ALIGNMENT
    from docx.oxml.ns import qn
    import tempfile

    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    data = {}
    if company.data_json:
        try:
            data = json.loads(company.data_json)
        except:
            pass
    # 也合并 session 中的财报数据
    if "last_finance_data" in session:
        data.update(session["last_finance_data"])

    doc = Document()

    # 全局默认字体
    style = doc.styles['Normal']
    style.font.name = 'Times New Roman'
    style.element.rPr.rFonts.set(qn('w:eastAsia'), '宋体')
    style.font.size = Pt(12)
    style.paragraph_format.line_spacing = 1.5

    def set_run_font(run, size=12, bold=False, name='宋体'):
        run.font.size = Pt(size)
        run.bold = bold
        run.font.name = name
        run._element.rPr.rFonts.set(qn('w:eastAsia'), name)

    def add_title(text, size=22):
        p = doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_after = Pt(6)
        set_run_font(p.add_run(text), size, True)
        return p

    def add_heading(text):
        p = doc.add_paragraph()
        p.paragraph_format.space_before = Pt(16)
        p.paragraph_format.space_after = Pt(6)
        set_run_font(p.add_run(text), 15, True)
        return p

    def add_body(text):
        if not text: return
        text = str(text).replace('\r\n', '\n').replace('\r', '\n').strip()
        text = re.sub(r'\n[ \t　]*\n+', '\n', text)
        p = doc.add_paragraph()
        p.paragraph_format.first_line_indent = Cm(0.7)
        set_run_font(p.add_run(text), 11)

    def add_table(headers, rows, col_widths=None):
        tbl_obj = doc.add_table(rows=1+len(rows), cols=len(headers), style='Table Grid')
        tbl_obj.alignment = WD_TABLE_ALIGNMENT.CENTER
        for i, h in enumerate(headers):
            c = tbl_obj.rows[0].cells[i]
            c.text = ''
            p = c.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            set_run_font(p.add_run(h), 10, True)
        for ri, row in enumerate(rows):
            for ci, v in enumerate(row):
                c = tbl_obj.rows[ri+1].cells[ci]
                c.text = ''
                set_run_font(c.paragraphs[0].add_run(str(v or '')), 10)
        if col_widths:
            for ri in range(len(rows)+1):
                for ci, w in enumerate(col_widths):
                    if ci < len(headers):
                        tbl_obj.rows[ri].cells[ci].width = Cm(w)
        return tbl_obj

    # ====== 企业简介 ======
    intro = data.get('company_intro','')
    scope = data.get('business_scope','')
    if intro:
        add_heading('企业简介')
        add_body(intro)
        if scope:
            add_body(f'经营范围：{scope}')

    # ====== 一、主要情况 ======
    add_heading('一、主要情况')
    add_table(
        ['项目','内容','项目','内容'],
        [
            ['企业名称', company.name, '所在地区', f"{data.get('province','')}{data.get('city','')}"],
            ['技术领域', data.get('tech_field',''), '申请日期', f"{data.get('app_year','')}年{data.get('app_month','')}月"],
            ['职工总数', data.get('staff_total',''), '科技人员', data.get('tech_staff','')],
            ['Ⅰ类IP', data.get('ip_class1_count','0'), 'Ⅱ类IP', data.get('ip_class2_count','0')],
        ]
    )
    # 三年经营数据
    add_table(
        ['年度','净资产(万元)','销售收入(万元)','利润总额(万元)'],
        [
            [data.get('year1_label','第一年'), data.get('year1_net_assets',''), data.get('year1_sales',''), data.get('year1_profit','')],
            [data.get('year2_label','第二年'), data.get('year2_net_assets',''), data.get('year2_sales',''), data.get('year2_profit','')],
            [data.get('year3_label','第三年'), data.get('year3_net_assets',''), data.get('year3_sales',''), data.get('year3_profit','')],
        ]
    )
    add_table(
        ['项目','金额(万元)'],
        [
            ['近三年研发费用总额', data.get('rd_total_3y','')],
            ['其中：境内研发费用', data.get('rd_domestic','')],
            ['近一年总收入', data.get('revenue_1y','')],
            ['近一年高新技术产品收入', data.get('hitech_revenue','')],
        ]
    )

    # ====== 二、知识产权 ======
    add_heading('二、知识产权汇总表')
    ips = []
    ip_count = _indexed_form_count(data, "ip_", "name", 10)
    for i in range(ip_count):
        nm = data.get(f'ip_{i}_name','')
        if nm:
            patent_no = data.get(f'ip_{i}_patent_no','') or data.get(f'ip_{i}_no','') or data.get(f'ip_{i}_auth_no','')
            ips.append([data.get(f'ip_{i}_seq','') or str(i + 1), nm, data.get(f'ip_{i}_type',''),
                       data.get(f'ip_{i}_status',''), patent_no,
                       data.get(f'ip_{i}_app_date',''), data.get(f'ip_{i}_applicant',''),
                       data.get(f'ip_{i}_date','')])
    if ips:
        add_table(['编号','名称','专利类型','法律状态','专利号','申请日期','专利权人','授权日期'], ips)
    else:
        add_body('（未填写）')

    # ====== 三、人力资源 ======
    add_heading('三、人力资源情况表')
    add_table(
        ['类别','人数','类别','人数'],
        [
            ['职工总数', data.get('hr_total',''), '科技人员', data.get('hr_tech','')],
            ['在职', data.get('hr_fulltime',''), '兼职', data.get('hr_parttime','')],
            ['临时聘用', data.get('hr_temp',''), '外籍', data.get('hr_foreign','')],
            ['留学归国', data.get('hr_returnee',''), '人才计划', data.get('hr_talent_plan','')],
        ]
    )
    add_table(
        ['学历','博士','硕士','本科','大专及以下'],
        [['人数', data.get('edu_phd',''), data.get('edu_master',''), data.get('edu_bachelor',''), data.get('edu_diploma','')]]
    )

    # ====== 四、研发活动 ======
    add_heading('四、企业研究开发活动情况表（近三年）')
    rd_count = 0
    for i in range(20):
        name = data.get(f'rd_{i}_name','')
        if not name: continue
        rd_count += 1
        doc.add_paragraph()
        p = doc.add_paragraph()
        set_run_font(p.add_run(f"项目{rd_count}：{data.get(f'rd_{i}_no',f'RD{(rd_count):02d}')} {name}"), 13, True)
        
        add_table(
            ['项目','内容'],
            [
                ['起止时间', data.get(f'rd_{i}_period','')],
                ['技术领域', data.get(f'rd_{i}_field','')],
                ['技术来源', data.get(f'rd_{i}_source','')],
                ['知识产权编号', data.get(f'rd_{i}_ip_no','')],
                ['研发经费预算(万元)', data.get(f'rd_{i}_budget','')],
                ['近三年总支出(万元)', data.get(f'rd_{i}_total','')],
            ]
        )
        for lb, k in [
            ('目的及组织实施方式（限400字）','purpose'),
            ('核心技术及创新点（限400字）','innovation'),
            ('取得的阶段性成果（限400字）','result'),
        ]:
            v = data.get(f'rd_{i}_{k}','')
            if v:
                p = doc.add_paragraph()
                set_run_font(p.add_run(f'{lb}：'), 11, True)
                add_body(v)

    # ====== 五、费用明细 ======
    add_heading('五、企业年度研究开发费用结构明细表（万元）')
    for yi, ylabel in enumerate(['第一年','第二年','第三年']):
        add_body(f'【{ylabel}】')
        rows = []
        for rdi in range(min(rd_count, 8)):
            rd_no = f'RD{(rdi+1):02d}'
            rows.append([rd_no, data.get(f'fee_{yi}_labor_rd{rdi}','')])
        if rows:
            hdrs = ['科目'] + [r[0] for r in rows] + ['合计']
            vals = ['人员人工费用'] + [r[1] for r in rows] + [data.get(f'fee_{yi}_labor_total','')]
            add_table(hdrs, [vals])

    # ====== 六、PS ======
    add_heading('六、上年度高新技术产品（服务）情况表')
    for i in range(20):
        psname = data.get(f'ps_{i}_name','')
        if not psname: continue
        doc.add_paragraph()
        p = doc.add_paragraph()
        set_run_font(p.add_run(f"{data.get(f'ps_{i}_no',f'PS{(i+1):02d}')}：{psname}"), 13, True)
        add_table(
            ['项目','内容'],
            [
                ['技术领域', data.get(f'ps_{i}_field','')],
                ['技术来源', data.get(f'ps_{i}_source','')],
                ['销售收入(万元)', data.get(f'ps_{i}_revenue','')],
                ['是否主要产品', '是' if data.get(f'ps_{i}_is_main','yes') == 'yes' else '否'],
                ['关联RD', data.get(f'ps_{i}_rds','')],
                ['知识产权编号', data.get(f'ps_{i}_ip_no','')],
            ]
        )
        for lb, k in [
            ('关键技术及主要技术指标（限400字）','tech'),
            ('与同类产品的竞争优势（限400字）','advantage'),
            ('知识产权对产品的支持作用（限400字）','ip_support'),
        ]:
            v = data.get(f'ps_{i}_{k}','')
            if v:
                p = doc.add_paragraph()
                set_run_font(p.add_run(f'{lb}：'), 11, True)
                add_body(v)

    # ====== 七、创新能力 ======
    add_heading('七、企业创新能力')
    cv_rows = []
    cv_descriptions = []
    for i in range(50):
        desc = re.sub(r'\n[ \t　]*\n+', '\n', str(data.get(f'cv_{i}_desc','') or '').strip())
        rd = data.get(f'cv_{i}_rd','')
        ip = data.get(f'cv_{i}_ip','')
        ps = data.get(f'cv_{i}_ps','')
        result_name = data.get(f'cv_{i}_result_name','')
        if desc or rd or ip or ps or result_name:
            seq = str(len(cv_rows) + 1)
            cv_rows.append([seq, rd, ip, ps, result_name])
            cv_descriptions.append((seq, result_name, desc))
    if cv_rows:
        set_run_font(doc.add_paragraph().add_run('成果转化明细表'), 12, True)
        add_table(['序号','RD项目','知识产权','PS产品','成果名称'], cv_rows, [1.1, 3.1, 3.4, 3.1, 4.2])
        set_run_font(doc.add_paragraph().add_run('转化结果描述'), 12, True)
        for seq, result_name, desc in cv_descriptions:
            p = doc.add_paragraph()
            set_run_font(p.add_run(f'{seq}. {result_name or "成果转化"}：'), 11, True)
            add_body(desc or '（未填写）')

    for lb, k in [
        ('知识产权对企业竞争力的作用','innovation_ip'),
        ('科技成果转化情况','innovation_transform'),
        ('研究开发与技术创新组织管理情况','innovation_rd_mgmt'),
        ('管理与科技人员情况','innovation_staff'),
    ]:
        v = data.get(k,'')
        p = doc.add_paragraph()
        set_run_font(p.add_run(f'{lb}（限400字）：'), 12, True)
        add_body(v or '（未填写）')

    # ====== 八、标准 ======
    add_heading('八、企业参与国家标准或行业标准制定情况')
    std_rows = []
    for i in range(5):
        sn = data.get(f'std_{i}_name','')
        if sn:
            std_rows.append([str(i+1), sn, data.get(f'std_{i}_level',''), data.get(f'std_{i}_no',''), data.get(f'std_{i}_role','')])
    if std_rows:
        add_table(['序号','标准名称','级别','编号','参与方式'], std_rows)
    else:
        add_body('（未填写）')

    tmp = tempfile.NamedTemporaryFile(suffix='.docx', delete=False)
    doc.save(tmp.name)
    tmp.close()
    from flask import send_file
    return send_file(tmp.name, mimetype='application/vnd.openxmlformats-officedocument.wordprocessingml.document',
                    as_attachment=True, download_name=f'高新技术企业认定申请书_{company.name}.docx')
