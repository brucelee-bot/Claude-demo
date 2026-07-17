"""Shared company header helpers for generated Word documents."""

from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml.ns import qn
from docx.shared import Cm, Pt, RGBColor


KNOWN_COMPANY_ENGLISH_NAMES = {
    "北京新亚盛创电气技术有限公司": "Bei Jing Xin Ya Sheng Chuang Dian Qi Ji Shu You Xian Gong Si",
}


def company_name_to_pinyin(company_name):
    company_name = str(company_name or "").strip()
    if not company_name:
        return ""
    if company_name in KNOWN_COMPANY_ENGLISH_NAMES:
        return KNOWN_COMPANY_ENGLISH_NAMES[company_name]
    try:
        from pypinyin import lazy_pinyin

        return " ".join(part.capitalize() for part in lazy_pinyin(company_name) if part).strip()
    except ImportError:
        return company_name if company_name.isascii() else ""


def add_company_header(doc, company_name, company_english_name=""):
    chinese_name = str(company_name or "").strip()
    english_name = str(company_english_name or "").strip() or company_name_to_pinyin(chinese_name)
    header_text = "  |  ".join(part for part in [chinese_name, english_name] if part)
    if not header_text:
        return

    for section in doc.sections:
        section.header_distance = Cm(0.65)
        header = section.header
        header.is_linked_to_previous = False
        paragraph = header.paragraphs[0]
        paragraph.clear()
        paragraph.alignment = WD_ALIGN_PARAGRAPH.CENTER
        paragraph.paragraph_format.space_after = Pt(0)
        run = paragraph.add_run(header_text)
        run.font.name = "Arial"
        run._element.rPr.rFonts.set(qn("w:eastAsia"), "微软雅黑")
        run.font.size = Pt(8)
        run.font.color.rgb = RGBColor(107, 114, 128)
