import re


_SERVICE_ENDINGS = (
    "服务",
    "咨询",
    "运维",
    "运营",
    "检测",
    "检验",
    "评估",
    "设计",
    "培训",
)


def infer_ps_kind(ps_name, explicit_kind=""):
    kind = str(explicit_kind or "").strip().lower()
    if kind in {"service", "服务"}:
        return "service"
    if kind in {"product", "产品"}:
        return "product"

    name = re.sub(r"^\s*PS\d+\s*[-－—:：]?\s*", "", str(ps_name or "").strip(), flags=re.I)
    if any(name.endswith(ending) for ending in _SERVICE_ENDINGS):
        return "service"
    return "product"


def ps_type_label(ps_name="", explicit_kind=""):
    return "服务" if infer_ps_kind(ps_name, explicit_kind) == "service" else "产品"


def normalize_ps_reference_text(text, ps_name="", explicit_kind=""):
    text = str(text or "")
    if not text:
        return text

    label = ps_type_label(ps_name, explicit_kind)
    combined = r"产品\s*(?:[（(]\s*服务\s*[）)]|[/／]\s*服务|或\s*服务)"
    replacements = [
        (rf"本\s*高新技术\s*{combined}", f"本高新技术{label}"),
        (rf"该\s*高新技术\s*{combined}", f"该高新技术{label}"),
        (rf"高新技术\s*{combined}", f"高新技术{label}"),
        (rf"本\s*{combined}", f"本{label}"),
        (rf"该\s*{combined}", f"该{label}"),
        (combined, label),
    ]
    for pattern, replacement in replacements:
        text = re.sub(pattern, replacement, text)

    if label == "服务":
        service_replacements = [
            ("产品服务", "服务"),
            ("本高新技术产品", "本高新技术服务"),
            ("该高新技术产品", "该高新技术服务"),
            ("高新技术产品", "高新技术服务"),
            ("本产品", "本服务"),
            ("该产品", "该服务"),
            ("同类产品", "同类服务"),
            ("产品名称", "服务名称"),
            ("产品概况", "服务概况"),
            ("产品特点", "服务特点"),
            ("产品核心技术", "服务核心技术"),
            ("产品技术水平", "服务技术水平"),
            ("产品体系", "服务体系"),
            ("产品功能", "服务功能"),
            ("产品优势", "服务优势"),
        ]
        for source, replacement in service_replacements:
            text = text.replace(source, replacement)
        text = re.sub(
            r"(^|[。！？；：\n])(\s*)产品(?=(?:已|具有|采用|面向|可|通过|依托|能够|主要|属于))",
            rf"\1\2服务",
            text,
        )
        text = re.sub(
            r"(覆盖|支撑|应用于|提升|形成|完善|优化|推广)产品(?=(?:核心|技术|功能|性能|体系|应用|产业化|竞争力|市场))",
            rf"\1服务",
            text,
        )
    else:
        text = text.replace("产品服务", "产品")
    text = re.sub(r"服务\s*服务", "服务", text)
    text = re.sub(r"产品\s*产品", "产品", text)
    return text
