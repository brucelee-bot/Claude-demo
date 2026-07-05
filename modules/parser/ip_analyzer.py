"""
知识产权证书解析器 — 从专利/标准 PDF 中提取类型、数量、获得方式等信息
"""
import os, re
from typing import Any


# 专利类型识别
PATENT_TYPE_PATTERNS = [
    # (正则, 类型, 是否Ⅰ类, 分值)
    (r"发明专利|发明\s*专利|INVENTION\s*PATENT", "invention", True, 8),
    (r"实用新型|实用\s*新型|UTILITY\s*MODEL", "utility", False, 5),
    (r"外观设计|外观\s*设计|DESIGN\s*PATENT", "design", False, 3),
    (r"软件著作权|计算机软件|SOFTWARE\s*COPYRIGHT", "copyright", False, 2),
]

# 获得方式识别
ACQUISITION_PATTERNS = [
    (r"原始取得|自主研发|原始\s*取得", "self"),
    (r"受让|转让|受赠|并购|继受\s*取得", "transfer"),
]

# 海外专利识别
OVERSEAS_PATTERNS = [
    r"US\d{5,12}",   # 美国
    r"EP\d{5,10}",   # 欧洲
    r"JP\d{6,10}",   # 日本
    r"WO\d{4,8}",    # PCT
    r"KR\d{7,9}",    # 韩国
    r"PCT/",         # PCT申请号
    r"PCT\s*专利|PCT\s*PATENT|海外|国际\s*专利",
]

# 授权状态 — 有专利证书 + 授权公告号即已授权
GRANT_PATTERNS = [
    r"发明专利证书|实用新型专利证书|外观设计专利证书",
    r"授权\s*公告\s*号[：:]\s*(\S[\S]*?)(?:\n|$)",
]


def _ocr_image_bytes(img_bytes: bytes) -> str:
    """对图片字节执行 OCR；依赖缺失或识别失败时返回空字符串。"""
    try:
        import io
        from PIL import Image
        import pytesseract

        image = Image.open(io.BytesIO(img_bytes))
        return pytesseract.image_to_string(image, lang="chi_sim+eng") or ""
    except Exception:
        return ""


def _extract_text_from_pdf(filepath: str) -> str:
    """从 PDF 提取文本；文本层为空时将页面渲染成图片后 OCR。"""
    try:
        import pymupdf

        doc = pymupdf.open(filepath)
        try:
            text = "\n".join((page.get_text() or "") for page in doc).strip()
            if len(text) >= 30:
                return text

            ocr_texts = []
            for page in doc:
                pix = page.get_pixmap(matrix=pymupdf.Matrix(2, 2), alpha=False)
                ocr_texts.append(_ocr_image_bytes(pix.tobytes("png")))
            return "\n".join(ocr_texts).strip()
        finally:
            doc.close()
    except Exception:
        return ""


def _extract_text_from_image(filepath: str) -> str:
    """从图片提取文本。"""
    try:
        with open(filepath, "rb") as f:
            return _ocr_image_bytes(f.read())
    except Exception:
        return ""


def extract_text(filepath: str) -> str:
    """根据文件类型提取文本"""
    ext = os.path.splitext(filepath)[1].lower()
    if ext == ".pdf":
        return _extract_text_from_pdf(filepath)
    elif ext in (".png", ".jpg", ".jpeg"):
        return _extract_text_from_image(filepath)
    return ""


def parse_patent(text: str) -> dict[str, Any]:
    """
    从专利证书文本中解析专利信息
    返回: {
        "patent_type": "invention" | "utility" | "design" | "copyright" | None,
        "is_class1": True/False,       # 是否Ⅰ类知识产权
        "is_granted": True/False,       # 是否已授权
        "is_overseas": True/False,      # 是否海外专利
        "acquisition": "self" | "transfer" | None,  # 获得方式
        "has_standard": True/False,     # 是否标准文件
    }
    """
    result: dict[str, Any] = {
        "patent_type": None,
        "is_class1": False,
        "is_granted": False,
        "is_overseas": False,
        "acquisition": None,
        "has_standard": False,
    }

    if not text.strip():
        return result

    # 专利类型
    for pattern, ptype, is_class1, _ in PATENT_TYPE_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            result["patent_type"] = ptype
            result["is_class1"] = is_class1
            break

    # 授权状态 — 有证书标题或授权公告号即为已授权
    for pattern in GRANT_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            result["is_granted"] = True
            break

    # 海外专利
    for pattern in OVERSEAS_PATTERNS:
        if re.search(pattern, text, re.IGNORECASE):
            result["is_overseas"] = True
            break

    # 获得方式
    for pattern, method in ACQUISITION_PATTERNS:
        if re.search(pattern, text):
            result["acquisition"] = method
            break

    # 标准文件检测
    if re.search(r"国家\s*标准|行业\s*标准|团体\s*标准|地方\s*标准|GB\s*/|GB/|ISO\s*\d|参与.*标准|编制.*标准", text, re.IGNORECASE):
        result["has_standard"] = True

    result["details"] = extract_patent_details(text)
    if result["details"]:
        result["is_granted"] = True
    return result




def extract_patent_details(text: str) -> dict:
    """从专利证书提取详细信息"""
    import re
    info = {}

    # 预处理：去掉中文字间的空格（PDF 常见问题）
    # "专 利 号" → "专利号", "发 明 人" → "发明人"
    text = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[\u4e00-\u9fff])', '', text)
    text = re.sub(r'(?<=[\u4e00-\u9fff])[ \t]+(?=[：:])', '', text)
    for label in [
        "发明人", "设计人", "申请人", "专利号", "专利权人", "著作权人", "地址",
        "实用新型名称", "实用新型专利名称", "发明创造名称", "发明名称", "软件名称",
        "授权公告日", "授权公告号", "专利申请日", "申请日", "权利取得方式", "权利范围", "登记号",
    ]:
        text = re.sub(r"\s*".join(map(re.escape, label)), label, text)

    # 名称后的截断标签
    _STOP = (
        r"专利\s*号|专利\s*申请|申请\s*日|授权\s*公告|授权\s*日|专利权\s*人|专利\s*权\s*人|"
        r"发明\s*人|设计\s*人|申请\s*人|地\s*址|证书\s*号|登记\s*号|"
        r"软件\s*版本|开发\s*完成|首次\s*发表|权利\s*取得\s*方式|权利\s*范围|著作\s*权\s*人|登记\s*号|标准\s*号|"
        r"实施\s*日期|发布\s*单位|起草\s*单位|法律\s*状态|专利\s*类型|"
        r"主\s*分\s*类\s*号|IPC|CPC|G06F|B65D|B31B|B32B|C08L"
    )
    _NAME_LABEL = (
        r"发明\s*创造\s*名称|实用\s*新型\s*专利\s*名称|实用\s*新型\s*名称|"
        r"外观\s*设计\s*专利\s*名称|外观\s*设计\s*名称|发明\s*专利\s*名称|"
        r"发明\s*名称|软件\s*名称|名称"
    )
    _NAME_STOP = (
        r"发明\s*人|设计\s*人|申请\s*人|专利\s*权\s*人|专利权\s*人|地\s*址|"
        r"专利\s*号|申请\s*日|专利\s*申请\s*日|授权\s*公告\s*日|授权\s*公告\s*号|"
        r"证书\s*号|登记\s*号|著作\s*权\s*人|权利\s*取得\s*方式|权利\s*范围|软件\s*版本|开发\s*完成|首次\s*发表|主\s*分\s*类\s*号|IPC|CPC"
    )

    def _clean_patent_name(value: str) -> str:
        value = str(value or "").strip()
        value = re.sub(r'(?<=[一-鿿])[ \t]+(?=[一-鿿])', '', value)
        value = re.sub(rf"^(?:{_NAME_LABEL})\s*[：:]?\s*", "", value)
        value = re.split(rf"(?:{_NAME_STOP})\s*[：:]?", value, maxsplit=1)[0]
        value = "".join(line.strip() for line in value.splitlines() if line.strip()).strip(" ：:;；，,")
        value = re.sub(r"(?:V|v|版本)?\s*\d+(?:\.\d+)+$", "", value).strip(" ：:;；，,")
        return value

    def _extract_patent_name(src: str) -> str:
        label_match = re.search(rf"(?:{_NAME_LABEL})\s*[：:]?", src)
        if label_match:
            tail = src[label_match.end():]
            stop_match = re.search(rf"(?:{_NAME_STOP})\s*[：:]?", tail)
            candidate = tail[:stop_match.start()] if stop_match else tail
            name = _clean_patent_name(candidate)
            if name:
                return name

        inventor_match = re.search(r"发明\s*人\s*[：:]?", src)
        if inventor_match:
            before = src[:inventor_match.start()]
            lines = [line.strip() for line in before.splitlines() if line.strip()]
            for line in reversed(lines):
                cleaned = _clean_patent_name(line)
                if cleaned and not re.search(r"专利证书|中华人民共和国|国家知识产权局", cleaned):
                    return cleaned
        return ""


    def _normalize_date(value: str) -> str:
        return re.sub(r"[年月]", "-", str(value or "")).replace("日", "").replace("/", "-").replace(" ", "")

    def _extract_software_application_date(src: str) -> str:
        candidates = re.findall(r"\d{4}\s*[年\-/]\s*\d{1,2}\s*[月\-/]\s*\d{1,2}\s*[日]?", src)
        if not candidates:
            return ""
        for marker in ["中国版权保护中心", "登记机构", "发证", "登记证书"]:
            idx = src.rfind(marker)
            if idx >= 0:
                nearby = src[idx:idx + 300]
                local = re.findall(r"\d{4}\s*[年\-/]\s*\d{1,2}\s*[月\-/]\s*\d{1,2}\s*[日]?", nearby)
                if local:
                    return _normalize_date(local[-1])
        return _normalize_date(candidates[-1])

    patterns = {
        "name": [
            rf"发明\s*创造\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"实用新型\s*专利\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"实用新型\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"外观\s*设计\s*专利\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"外观\s*设计\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"发明\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"发明\s*专利\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"软件\s*名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
            rf"名称[：:]\s*([\s\S]+?)(?:\n(?:{_STOP})[：:]|\Z)",
        ],
        "patent_no": [
            r"登记\s*号[：:]\s*([A-Z0-9]{4,}SR\d+|\d{4}SR\d+|[A-Z0-9]{6,})",
            r"软件\s*登记\s*号[：:]\s*([A-Z0-9]{4,}SR\d+|\d{4}SR\d+|[A-Z0-9]{6,})",
            r"专利\s*号[：:]\s*(ZL\s*[\d\s.X]+)",
            r"专利\s*号[：:]\s*([\d\s.X]+)",
            r"申请号[：:]\s*([\d\s.X]+)",
        ],
        "grant_no": [
            r"授权\s*公告\s*号[：:]\s*([A-Z]{0,3}\s*\d{5,12}\s*[A-Z]?)",
            r"公告\s*号[：:]\s*([A-Z]{0,3}\s*\d{5,12}\s*[A-Z]?)",
            r"专利\s*号[：:]\s*([A-Z]{0,3}\s*\d{5,12}\s*[A-Z]?)",
            r"证书号[：:]?\s*第?(\d+)\s*号",
        ],
        "grant_date": [
            r"授权\s*公告\s*日[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
            r"授权\s*公告\s*日期[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
            r"授权\s*公告\s*日期[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
        ],
        "app_date": [
            r"申请\s*日[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
            r"专利\s*申请\s*日[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
            r"申请\s*日期[：:]\s*(\d{4}\s*[年\-\/]\s*\d{1,2}\s*[月\-\/]\s*\d{1,2}\s*[日]?)",
        ],
        "applicant": [
            r"专利权\s*人[：:]\s*(.+?)(?:\n|地址|$)",
            r"申请\s*人[：:]\s*(.+?)(?:\n|地址|$)",
            r"著作权\s*人[：:]\s*(.+?)(?:\n|地址|$)",
        ],
    }

    if extracted_name := _extract_patent_name(text):
        info["name"] = extracted_name

    for field, pats in patterns.items():
        if field == "name" and info.get("name"):
            continue
        for p in pats:
            m = re.search(p, text)
            if m:
                val = m.group(1).strip()
                if field == "name":
                    val = _clean_patent_name(val)
                    if not val:
                        continue
                # 日期统一格式
                if field in ("grant_date", "app_date"):
                    val = _normalize_date(val)
                info[field] = val
                break

    if re.search(r"软件著作权|计算机软件|中国版权保护中心|登记号", text) and not info.get("app_date"):
        soft_date = _extract_software_application_date(text)
        if soft_date:
            info["app_date"] = soft_date

    if info:
        info["legal_status"] = "授权"

    # 如果没有 patent_no 但 grant_no 匹配到专利号格式，互换
    if "patent_no" not in info and "grant_no" in info:
        gn = info["grant_no"]
        if re.match(r"ZL\s*\d", gn):
            info["patent_no"] = gn

    return info

def evaluate_ip(certificates: list[dict]) -> dict:
    """
    根据多份证书的解析结果，综合评定 IP 各项指标

    certificates: [{"filename": "专利证书1.pdf", "parsed": {...}}, ...]

    返回评分建议:
    {
        "ip_tech_level": "A",           # (1) 技术先进程度
        "ip_tech_level_score": 8,
        "ip_core_support": "B",         # (2) 核心支持 — 默认 B，需人工确认
        "ip_core_support_score": 6,
        "ip_quantity": "A",             # (3) 知识产权数量
        "ip_quantity_score": 8,
        "ip_acquisition": "A",          # (4) 获得方式
        "ip_acquisition_score": 5,
        "ip_standard": "B",             # (5) 标准参与
        "ip_standard_score": 0,
        "details": "..."                # 人工可读的解释
    }
    """
    if not certificates:
        return _empty_eval()

    parsed_list = [c.get("parsed", {}) for c in certificates]

    class1_count = sum(1 for p in parsed_list if p.get("is_class1"))
    utility_count = sum(1 for p in parsed_list if p.get("patent_type") == "utility")
    copyright_count = sum(1 for p in parsed_list if p.get("patent_type") == "copyright")
    design_count = sum(1 for p in parsed_list if p.get("patent_type") == "design")
    class2_count = utility_count + design_count + copyright_count
    total_count = len(parsed_list)

    has_overseas = any(p.get("is_overseas") for p in parsed_list)
    has_granted_invention = any(
        p.get("is_class1") and p.get("is_granted") for p in parsed_list
    )
    has_invention_application = any(p.get("is_class1") for p in parsed_list)
    has_transfer = any(p.get("acquisition") == "transfer" for p in parsed_list)
    has_self_developed = any(p.get("acquisition") == "self" for p in parsed_list)
    has_standard = any(p.get("has_standard") for p in parsed_list)

    # (1) 技术先进程度
    if has_overseas and has_granted_invention:
        tech_level, tech_score = "A", 8
    elif has_granted_invention:
        tech_level, tech_score = "B", 6
    elif has_invention_application or utility_count > 0:
        tech_level, tech_score = "C", 4
    elif utility_count > 0:
        tech_level, tech_score = "D", 2
    else:
        tech_level, tech_score = "E", 0

    # (3) 知识产权数量
    if class1_count >= 1:
        quant_level, quant_score = "A", 8
    elif class2_count >= 5:
        quant_level, quant_score = "B", 6
    elif class2_count >= 3:
        quant_level, quant_score = "C", 4
    elif class2_count >= 1:
        quant_level, quant_score = "D", 2
    else:
        quant_level, quant_score = "E", 0

    # (4) 获得方式
    if not has_transfer:
        acq_level, acq_score = "A", 5  # 全部自主研发
    elif has_self_developed and has_transfer:
        acq_level, acq_score = "B", 3  # 混合
    else:
        acq_level, acq_score = "B", 2  # 仅受让

    # (5) 标准参与
    if has_standard:
        std_level, std_score = "A", 2
    else:
        std_level, std_score = "B", 0

    details = []
    details.append(f"共识别 {total_count} 份知识产权文件")
    details.append(f"  Ⅰ类(发明) {class1_count} 项, Ⅱ类(新型/外观/软著) {class2_count} 项")
    details.append(f"  授权状态: 已授权发明 {1 if has_granted_invention else 0} 项")
    details.append(f"  海外专利: {'有' if has_overseas else '无'}")
    details.append(f"  获得方式: {'自主研发' if has_self_developed and not has_transfer else '受让/混合' if has_transfer else '未知'}")
    details.append(f"  标准文件: {'有' if has_standard else '无'}")

    return {
        "ip_tech_level": tech_level,
        "ip_tech_level_score": tech_score,
        "ip_core_support": "B",  # 默认较强，需人工确认
        "ip_core_support_score": 6,
        "ip_quantity": quant_level,
        "ip_quantity_score": quant_score,
        "ip_acquisition": acq_level,
        "ip_acquisition_score": acq_score,
        "ip_standard": std_level,
        "ip_standard_score": std_score,
        "details": "\n".join(details),
    }


def _empty_eval() -> dict:
    return {
        "ip_tech_level": "E", "ip_tech_level_score": 0,
        "ip_core_support": "B", "ip_core_support_score": 6,
        "ip_quantity": "E", "ip_quantity_score": 0,
        "ip_acquisition": "B", "ip_acquisition_score": 0,
        "ip_standard": "B", "ip_standard_score": 0,
        "details": "未上传知识产权文件",
    }
