import os
import re
import unicodedata


STAFF_CERTIFICATE_FIELDS = {
    "education_certificate": "education",
    "title_certificate": "title",
}


def _normalized_text(value):
    text = unicodedata.normalize("NFKC", str(value or ""))
    text = text.replace("\u200b", "").replace("\ufeff", "")
    text = re.sub(r"(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])", "", text)
    return re.sub(r"\s+", " ", text).strip()


def _normalized_name(value):
    return re.sub(r"[^\u4e00-\u9fffA-Za-z·]", "", _normalized_text(value))


def extract_education(text):
    normalized = _normalized_text(text)
    education_patterns = (
        ("博士", r"博士研究生|博士学位|博士"),
        ("硕士", r"硕士研究生|硕士学位|硕士|研究生学历"),
        ("本科", r"大学本科|本科毕业|本科学历|本科|学士学位|学士"),
        ("大专", r"大学专科|专科毕业|专科学历|大专|高职"),
        ("中专", r"中等专科|中专"),
        ("高中", r"普通高中|高中毕业|高中"),
    )
    for value, pattern in education_patterns:
        if re.search(pattern, normalized):
            return value
    return ""


def _clean_title_candidate(value):
    candidate = _normalized_text(value)
    candidate = re.split(
        r"证书|资格|专业|评审|批准|授予|发证|编号|管理号|姓名|性别|身份证|工作单位|$",
        candidate,
        maxsplit=1,
    )[0]
    return candidate.strip(" ：:，,；;。.【】[]（）()")


def extract_professional_title(text):
    normalized = _normalized_text(text)
    title_core = (
        r"(?:正高级|教授级|研究员级|副高级|高级|中级|初级|助理)?"
        r"(?:工程师|经济师|会计师|审计师|统计师|工艺美术师|实验师|研究员|"
        r"讲师|教师|医师|药师|技师|技术员)"
    )
    for label in ("资格名称", "专业技术资格", "专业技术职务", "任职资格", "职称"):
        match = re.search(rf"{label}\s*[：:]?\s*([^\n\r，,；;。]{{2,30}})", normalized)
        if not match:
            continue
        candidate = _clean_title_candidate(match.group(1))
        title_match = re.search(title_core, candidate)
        if title_match:
            return title_match.group(0)

    titles = re.findall(title_core, normalized)
    titles = [title for title in titles if title]
    if titles:
        return max(titles, key=lambda value: (len(value), value))

    for value, pattern in (
        ("高级职称", r"高级专业技术资格|副高级专业技术资格|正高级专业技术资格"),
        ("中级职称", r"中级专业技术资格"),
        ("初级职称", r"初级专业技术资格"),
    ):
        if re.search(pattern, normalized):
            return value
    return ""


def _match_staff_name(text, filename, staff_rows):
    source_text = _normalized_name(text)
    source_filename = _normalized_name(os.path.splitext(os.path.basename(filename or ""))[0])
    matches = []
    for row in staff_rows or []:
        name = str(row.get("name") or "").strip()
        normalized_name = _normalized_name(name)
        if len(normalized_name) < 2:
            continue
        if normalized_name in source_text or normalized_name in source_filename:
            matches.append({
                "index": row.get("index"),
                "name": name,
                "normalized_name": normalized_name,
            })

    if not matches:
        return None, "name_not_found"

    longest_length = max(len(item["normalized_name"]) for item in matches)
    longest = [item for item in matches if len(item["normalized_name"]) == longest_length]
    unique_people = {(item.get("index"), item["normalized_name"]) for item in longest}
    if len(unique_people) != 1:
        return None, "ambiguous_name"
    return longest[0], "matched"


def analyze_staff_certificate(text, filename, certificate_type, staff_rows):
    field = STAFF_CERTIFICATE_FIELDS.get(str(certificate_type or "").strip())
    if not field:
        raise ValueError("不支持的人员证书类型")

    value = extract_education(text) if field == "education" else extract_professional_title(text)
    matched, match_status = _match_staff_name(text, filename, staff_rows)
    status = "matched"
    if not value:
        status = "value_not_found"
    elif not matched:
        status = match_status

    return {
        "certificate_type": certificate_type,
        "field": field,
        "value": value,
        "matched_index": matched.get("index") if matched else None,
        "matched_name": matched.get("name") if matched else "",
        "status": status,
    }
