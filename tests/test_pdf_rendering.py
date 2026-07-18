import base64
import os
import re
import struct
import tempfile
import threading
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from flask import Flask, render_template

from modules.docgen.routes import (
    _chrome_executable,
    _collect_rd_project_rows,
    _combine_landscape_export_documents,
    _combine_portrait_export_documents,
    _ensure_landscape_page_rule,
    _export_rd_project_application_text,
    _generated_document_needs_landscape,
    _html_max_table_columns,
    _ordered_attachment_section_ranges,
    _pdf_cjk_font_path,
    _prepare_export_attachment_files,
    _portrait_export_document_batches,
    _render_portrait_export_document_batches,
    _rd_project_application_html,
    _rd_project_application_sections,
    _prepare_pymupdf_story_html,
    _remove_pymupdf_story_repeated_backgrounds,
    _render_export_pdf_file,
    _render_pdf_file,
    _stamp_pdf_file_headers,
    _system_evidence_table_widths,
)

try:
    import fitz
except ImportError:
    import pymupdf as fitz


class PdfRenderingTests(unittest.TestCase):
    def setUp(self):
        self.app = Flask(__name__)
        self.app.config["CHROME_BIN"] = "/path/that/does/not/exist"

    def test_missing_chrome_returns_none_instead_of_aborting_export(self):
        with (
            patch("modules.docgen.routes.os.path.isfile", return_value=False),
            patch("modules.docgen.routes.shutil.which", return_value=None),
        ):
            self.assertIsNone(_chrome_executable(self.app))

    def test_export_pdf_falls_back_to_pymupdf_and_keeps_chinese_text(self):
        html = """
        <!doctype html>
        <html lang="zh-CN">
        <head>
          <meta charset="utf-8">
          <style>
            @page { size: A4; margin: 28mm 16mm 18mm; }
            body { font-family: sans-serif; font-size: 12px; }
            table { width: 100%; border-collapse: collapse; }
            th, td { border: 1px solid #999; padding: 6px; }
          </style>
        </head>
        <body>
          <h1>科研项目书</h1>
          <table><tr><th>企业名称</th><td>测试科技有限公司</td></tr></table>
        </body>
        </html>
        """
        with tempfile.TemporaryDirectory() as output_dir:
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                pdf_path = _render_export_pdf_file(self.app, html, output_dir, "测试文档")

            self.assertIsNotNone(pdf_path)
            self.assertTrue(os.path.isfile(pdf_path))
            self.assertGreater(os.path.getsize(pdf_path), 0)
            document = fitz.open(pdf_path)
            try:
                self.assertGreater(document.page_count, 0)
                text = re.sub(r"\s+", "", "".join(page.get_text() for page in document))
                self.assertIn("科研项目书", text)
                self.assertIn("测试科技有限公司", text)
            finally:
                document.close()

    def test_rd_project_fallback_ignores_unpainted_story_shapes_outside_page(self):
        template_folder = Path(__file__).resolve().parents[1] / "templates"
        app = Flask(__name__, template_folder=str(template_folder))
        data = {
            "gaoxin_relation_table": {
                "rows": [{
                    "year": "2025",
                    "rd_code": "",
                    "rd_activity": "电力系统安全分析及继电保护定值计算服务技术研发",
                    "rd_period": "2025-01-01至2025-12-31",
                }],
            },
            "attachment_rd_staff_0_name": "张三",
            "rd_0_field": "电力系统自动化",
            "rd_0_budget": "100",
        }
        project = _collect_rd_project_rows(data)[0]
        application_text = _export_rd_project_application_text(project, "")

        with app.app_context():
            html = render_template(
                "application_gaoxin_rd_project_print.html",
                company=SimpleNamespace(name="测试科技有限公司"),
                company_english_name="TEST TECHNOLOGY CO., LTD.",
                project=project,
                application_text=application_text,
                application_html=_rd_project_application_html(application_text),
                application_sections=_rd_project_application_sections(application_text),
            )

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                pdf_path = _render_export_pdf_file(
                    app,
                    html,
                    output_dir,
                    "科研项目书 0",
                )

            self.assertIsNotNone(pdf_path)
            document = fitz.open(pdf_path)
            try:
                self.assertGreater(document.page_count, 1)
                text = "".join(page.get_text() for page in document)
                self.assertIn("科研项目书", text)
                self.assertIn("电力系统安全分析及继电保护定值计算服务技术研发", text)
                self.assertIn("审批意见", text)
                self.assertNotIn("填写依据", text)

                page_texts = [
                    re.sub(r"\s+", "", page.get_text())
                    for page in document
                ]
                cover_page_index = next(
                    index
                    for index, page_text in enumerate(page_texts)
                    if "科研项目书" in page_text
                    and "研发项目立项通知书" not in page_text
                )
                notice_page_index = next(
                    index
                    for index, page_text in enumerate(page_texts)
                    if "研发项目立项通知书" in page_text
                )
                basic_page_index = next(
                    index
                    for index, page_text in enumerate(page_texts)
                    if "一、项目基本情况与立项依据" in page_text
                )
                acceptance_page_index = next(
                    index
                    for index, page_text in enumerate(page_texts)
                    if "五、研发项目验收报告" in page_text
                )
                self.assertEqual(cover_page_index, 0)
                self.assertGreater(notice_page_index, cover_page_index)
                self.assertGreater(basic_page_index, notice_page_index)
                self.assertGreater(acceptance_page_index, basic_page_index)

                acceptance_page = next(
                    page for page in document
                    if "验收人员" in page.get_text()
                    and "审批意见" in page.get_text()
                )
                border_color = (143 / 255, 153 / 255, 167 / 255)
                right_edges = [
                    drawing["rect"]
                    for drawing in acceptance_page.get_drawings()
                    if drawing.get("fill")
                    and all(
                        abs(actual - expected) < 0.01
                        for actual, expected in zip(drawing["fill"], border_color)
                    )
                    and drawing["rect"].x0 > acceptance_page.rect.width - 50
                    and drawing["rect"].width < 1
                    and drawing["rect"].height > 20
                ]
                self.assertTrue(right_edges)
            finally:
                document.close()

    def test_pymupdf_fallback_honors_landscape_page_rule(self):
        html = """
        <html><head><style>
        @page { size: A4 landscape; margin: 28mm 12mm 14mm; }
        </style></head><body><h1>成果转化汇总表</h1></body></html>
        """
        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "landscape.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "横向文档")

            document = fitz.open(pdf_path)
            try:
                self.assertGreater(document[0].rect.width, document[0].rect.height)
                self.assertIn("成果转化汇总表", document[0].get_text())
            finally:
                document.close()

    def test_generated_documents_with_six_or_more_columns_use_landscape(self):
        narrow_html = (
            "<html><body><table><tr>"
            "<td>A</td><td>B</td><td>C</td><td>D</td>"
            "</tr></table></body></html>"
        )
        wide_html = (
            "<html><head><style>@page { size: A4; }</style></head><body>"
            "<table><tr><td colspan='2'>A</td><td>B</td><td>C</td>"
            "<td>D</td><td>E</td></tr></table></body></html>"
        )

        self.assertEqual(_html_max_table_columns(narrow_html), 4)
        self.assertEqual(_html_max_table_columns(wide_html), 6)
        self.assertFalse(_generated_document_needs_landscape(narrow_html))
        self.assertTrue(_generated_document_needs_landscape(wide_html))
        self.assertIn("size: A4 landscape;", _ensure_landscape_page_rule(wide_html))

    def test_attachment_section_ranges_are_strictly_ordered(self):
        ordered_numbers = [str(number) for number in range(2, 14)]
        starts = {
            section_no: index * 2
            for index, section_no in enumerate(ordered_numbers)
        }

        ranges = _ordered_attachment_section_ranges(
            starts,
            page_count=24,
            ordered_numbers=ordered_numbers,
        )

        self.assertEqual(
            ranges,
            [
                (section_no, index * 2, index * 2 + 1)
                for index, section_no in enumerate(ordered_numbers)
            ],
        )
        with self.assertRaisesRegex(RuntimeError, "分页标记缺失"):
            _ordered_attachment_section_ranges(
                {key: value for key, value in starts.items() if key != "8"},
                page_count=24,
                ordered_numbers=ordered_numbers,
            )
        reversed_starts = dict(starts)
        reversed_starts["8"], reversed_starts["9"] = (
            reversed_starts["9"],
            reversed_starts["8"],
        )
        with self.assertRaisesRegex(RuntimeError, "顺序异常"):
            _ordered_attachment_section_ranges(
                reversed_starts,
                page_count=24,
                ordered_numbers=ordered_numbers,
            )

    def test_pymupdf_fallback_expands_tables_across_available_page_width(self):
        html = """
        <html><head><style>
        @page { size: A4; margin: 28mm 16mm 18mm; }
        body { margin: 0; font-family: sans-serif; font-size: 10pt; }
        table { width: 100%; border-collapse: collapse; }
        th, td { border: 1pt solid #64748b; padding: 5pt; }
        th { background-color: #e9edf2; }
        </style></head><body>
        <table><tbody><tr><th>企业名称</th><td>测试科技有限公司</td></tr></tbody></table>
        </body></html>
        """
        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "full-width-table.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "全宽表格")

            document = fitz.open(pdf_path)
            try:
                page = document[0]
                table_rects = [
                    drawing["rect"]
                    for drawing in page.get_drawings()
                    if drawing["rect"].y0 > 75
                ]
                self.assertTrue(table_rects)
                left = min(rect.x0 for rect in table_rects)
                right = max(rect.x1 for rect in table_rects)
                available_width = page.rect.width - (2 * 16 * 72 / 25.4)
                self.assertGreater(right - left, available_width * 0.9)
            finally:
                document.close()

    def test_pymupdf_table_grid_draws_shared_edges_only_once(self):
        html = """
        <html><head><style>
        @page { size: A4; margin: 20mm; }
        body { margin: 0; }
        table {
          width: 100%;
          border: 0;
          border-top: 0.65pt solid #8f99a7;
          border-right: 0.65pt solid #8f99a7;
          border-collapse: separate;
          border-spacing: 0;
        }
        td {
          border: 0;
          border-left: 0.65pt solid #8f99a7;
          border-bottom: 0.65pt solid #8f99a7;
          padding: 10pt;
        }
        </style></head><body>
        <table data-pymupdf-widths="50,50">
          <tbody>
            <tr><td>A</td><td>B</td></tr>
            <tr><td>C</td><td>D</td></tr>
          </tbody>
        </table>
        </body></html>
        """

        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "single-edge-grid.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "单线网格")

            document = fitz.open(pdf_path)
            try:
                border_color = (143 / 255, 153 / 255, 167 / 255)
                border_rects = [
                    drawing["rect"]
                    for drawing in document[0].get_drawings()
                    if drawing.get("fill")
                    and all(
                        abs(actual - expected) < 0.01
                        for actual, expected in zip(drawing["fill"], border_color)
                    )
                ]

                self.assertEqual(len(border_rects), 10)
                self.assertTrue(
                    all(
                        abs(min(rect.width, rect.height) - 0.65) < 0.001
                        for rect in border_rects
                    )
                )
                right_edges = [
                    rect
                    for rect in border_rects
                    if rect.height > rect.width
                    and abs(rect.x1 - document[0].rect.width + (20 * 72 / 25.4)) < 1
                ]
                self.assertEqual(len(right_edges), 1)
            finally:
                document.close()

    def test_pymupdf_fallback_removes_repeated_background_fragments(self):
        rows = "".join(
            (
                f"<tr><td>{index}</td><td>第{index}行分页测试内容。"
                + "重复文字。" * 16
                + "</td></tr>"
            )
            for index in range(1, 55)
        )
        html = f"""
        <html><head><style>
        @page {{ size: A4; margin: 28mm 16mm 18mm; }}
        body {{ font-family: sans-serif; font-size: 9pt; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ border: 0.65pt solid #8f99a7; padding: 4pt 5pt; }}
        th {{ background-color: #e9edf2; }}
        </style></head><body>
        <table data-pymupdf-widths="20,80">
          <thead><tr><th>序号</th><th>内容</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """

        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "multipage-table.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "分页表格")

            document = fitz.open(pdf_path)
            try:
                self.assertGreater(document.page_count, 1)
                target = (233 / 255, 237 / 255, 242 / 255)

                def matching_backgrounds(page):
                    return [
                        drawing["rect"]
                        for drawing in page.get_drawings()
                        if drawing.get("fill")
                        and all(
                            abs(actual - expected) < 0.01
                            for actual, expected in zip(drawing["fill"], target)
                        )
                    ]

                first_page_backgrounds = matching_backgrounds(document[0])
                self.assertTrue(first_page_backgrounds)
                self.assertTrue(any(rect.height > 10 for rect in first_page_backgrounds))
                for page in document.pages(1):
                    black_fills = [
                        drawing["rect"]
                        for drawing in page.get_drawings()
                        if drawing.get("fill")
                        and all(channel < 0.01 for channel in drawing["fill"])
                        and drawing["rect"].width > 100
                        and drawing["rect"].height > 10
                    ]
                    self.assertFalse(black_fills)
                    self.assertFalse(
                        [
                            rect
                            for rect in matching_backgrounds(page)
                            if rect.height <= 8.1
                        ]
                    )
            finally:
                document.close()

    def test_story_background_cleanup_leaves_first_page_untouched(self):
        source = fitz.open()
        try:
            for _ in range(2):
                page = source.new_page()
                page.draw_rect(
                    fitz.Rect(50, 100, 200, 108),
                    color=None,
                    fill=(233 / 255, 237 / 255, 242 / 255),
                )
            with tempfile.TemporaryDirectory() as output_dir:
                pdf_path = os.path.join(output_dir, "manual-backgrounds.pdf")
                source.save(pdf_path)
                document = fitz.open(pdf_path)
                try:
                    self.assertEqual(
                        _remove_pymupdf_story_repeated_backgrounds(document),
                        0,
                    )
                    self.assertTrue(document[0].get_drawings())
                    self.assertTrue(document[1].get_drawings())
                finally:
                    document.close()
        finally:
            source.close()

    def test_pymupdf_fallback_removes_empty_ten_point_background_fragments(self):
        rows = "".join(
            (
                f"<tr><td>{index}</td><td>第{index}行续页背景检查。"
                + "分页内容。" * 18
                + "</td></tr>"
            )
            for index in range(1, 48)
        )
        html = f"""
        <html><head><style>
        @page {{ size: A4; margin: 28mm 16mm 18mm; }}
        body {{ font-family: sans-serif; font-size: 9pt; }}
        table {{ width: 100%; border-collapse: collapse; }}
        th, td {{ border: 0.65pt solid #8f99a7; padding: 5pt 6pt; }}
        th {{ background-color: #dfe7ef; }}
        </style></head><body>
        <table data-pymupdf-widths="20,80">
          <thead><tr><th>序号</th><th>内容</th></tr></thead>
          <tbody>{rows}</tbody>
        </table>
        </body></html>
        """

        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "ten-point-backgrounds.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "十点续页背景")

            document = fitz.open(pdf_path)
            try:
                self.assertGreater(document.page_count, 1)
                target = (223 / 255, 231 / 255, 239 / 255)
                for page in document.pages(1):
                    word_centers = [
                        ((word[0] + word[2]) / 2, (word[1] + word[3]) / 2)
                        for word in page.get_text("words")
                    ]
                    empty_short_fills = []
                    for drawing in page.get_drawings():
                        fill = drawing.get("fill")
                        rect = drawing["rect"]
                        if not fill or not all(
                            abs(actual - expected) < 0.01
                            for actual, expected in zip(fill, target)
                        ):
                            continue
                        if not 7.5 <= rect.height <= 10.5:
                            continue
                        if any(
                            rect.x0 <= center_x <= rect.x1
                            and rect.y0 <= center_y <= rect.y1
                            for center_x, center_y in word_centers
                        ):
                            continue
                        empty_short_fills.append(rect)
                    self.assertFalse(empty_short_fills)
            finally:
                document.close()

    def test_pymupdf_html_preparation_adds_one_invisible_sizer_per_table(self):
        prepared = _prepare_pymupdf_story_html(
            "<html><head></head><body>"
            "<table><tbody><tr><td>A</td><td colspan='2'>B</td></tr></tbody></table>"
            "<table><tr><td>C</td></tr></table>"
            "</body></html>",
            504,
        )
        from lxml import html as lxml_html

        root = lxml_html.document_fromstring(prepared)
        sizer_rows = root.xpath(
            "//tr[contains(concat(' ', normalize-space(@class), ' '), "
            "' pymupdf-table-sizer ')]"
        )
        self.assertEqual(len(sizer_rows), 2)
        self.assertEqual(len(sizer_rows[0].xpath("./td")), 3)
        self.assertEqual(len(sizer_rows[1].xpath("./td")), 1)
        self.assertIn("background-color: #ffffff", prepared)
        self.assertNotIn("\u00a0", prepared)
        self.assertEqual(len(root.xpath("//tr[contains(@class, 'pymupdf-table-sizer')]//img")), 4)

    def test_pymupdf_html_preparation_honors_explicit_table_width_ratios(self):
        prepared = _prepare_pymupdf_story_html(
            "<html><head></head><body>"
            "<table data-pymupdf-widths='18,30,52'>"
            "<tr><td>A</td><td>B</td><td>C</td></tr>"
            "</table>"
            "</body></html>",
            504,
        )
        from lxml import html as lxml_html

        root = lxml_html.document_fromstring(prepared)
        images = root.xpath("//tr[contains(@class, 'pymupdf-table-sizer')]/td/img")
        spacer_widths = [
            int(image.get("data-pymupdf-spacer-width"))
            for image in images
        ]
        self.assertEqual(len(spacer_widths), 3)
        self.assertAlmostEqual(spacer_widths[0] / sum(spacer_widths), 0.18, delta=0.02)
        self.assertAlmostEqual(spacer_widths[1] / sum(spacer_widths), 0.30, delta=0.02)
        self.assertAlmostEqual(spacer_widths[2] / sum(spacer_widths), 0.52, delta=0.02)

        png = base64.b64decode(images[0].get("src").split(",", 1)[1])
        width, height, bit_depth, color_type = struct.unpack(
            ">IIBB",
            png[16:26],
        )
        self.assertEqual(width, spacer_widths[0])
        self.assertEqual(height, 1)
        self.assertEqual(bit_depth, 8)
        self.assertEqual(color_type, 6)

    def test_pymupdf_html_preparation_ignores_invalid_table_width_ratios(self):
        prepared = _prepare_pymupdf_story_html(
            "<html><head></head><body>"
            "<table data-pymupdf-widths='10,0'>"
            "<tr><td>A</td><td>B</td></tr>"
            "</table>"
            "</body></html>",
            504,
        )
        from lxml import html as lxml_html

        root = lxml_html.document_fromstring(prepared)
        images = root.xpath("//tr[contains(@class, 'pymupdf-table-sizer')]/td/img")
        spacer_widths = [
            int(image.get("data-pymupdf-spacer-width"))
            for image in images
        ]
        self.assertEqual(len(spacer_widths), 2)
        self.assertAlmostEqual(spacer_widths[0] / sum(spacer_widths), 0.20, delta=0.02)
        self.assertAlmostEqual(spacer_widths[1] / sum(spacer_widths), 0.80, delta=0.02)

    def test_system_evidence_widths_prioritize_narrative_columns(self):
        raw_weights = _system_evidence_table_widths(
            ["审核环节", "审核人", "日期", "意见"]
        )
        weights = [float(value) for value in raw_weights.split(",")]
        self.assertEqual(len(weights), 4)
        self.assertGreater(weights[3], weights[0])
        self.assertGreater(weights[3], weights[1])
        self.assertGreater(weights[3], weights[2])
        self.assertAlmostEqual(sum(weights), 100, delta=0.05)

    def test_header_stamping_uses_builtin_chinese_font_without_system_fonts(self):
        html = """
        <html><head><style>
        @page { size: A4; margin: 28mm 16mm 18mm; }
        </style></head><body><p>正文内容</p></body></html>
        """
        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "header.pdf")
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                _render_pdf_file(self.app, html, pdf_path, "页眉测试")
            with patch("modules.docgen.routes._pdf_cjk_font_path", return_value=""):
                _stamp_pdf_file_headers(pdf_path, "测试科技有限公司", "TEST TECHNOLOGY CO., LTD.")

            document = fitz.open(pdf_path)
            try:
                text = "".join(page.get_text() for page in document)
                self.assertIn("测试科技有限公司", text)
                self.assertIn("TEST TECHNOLOGY CO., LTD.", text)
                self.assertIn("正文内容", text)
            finally:
                document.close()

    def test_header_font_uses_custom_file_only_when_explicitly_configured(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(_pdf_cjk_font_path(), "")

        with tempfile.NamedTemporaryFile(suffix=".ttf") as font_file:
            with patch.dict(
                os.environ,
                {"PDF_CJK_FONT": font_file.name},
                clear=True,
            ):
                self.assertEqual(_pdf_cjk_font_path(), font_file.name)

    def test_long_bilingual_headers_fit_portrait_and_landscape_pages(self):
        chinese_name = (
            "内蒙古中煤远兴能源化工有限公司全厂电力系统安全分析及"
            "继电保护定值计算服务项目管理中心"
        )
        english_name = (
            "INNER MONGOLIA CHINA COAL YUANXING ENERGY AND CHEMICAL "
            "INDUSTRY CO., LTD. POWER SYSTEM SAFETY ANALYSIS CENTER"
        )
        with tempfile.TemporaryDirectory() as output_dir:
            pdf_path = os.path.join(output_dir, "long-header.pdf")
            source = fitz.open()
            try:
                portrait = source.new_page(width=595, height=842)
                portrait.insert_text(
                    fitz.Point(72, 120),
                    "portrait body",
                    fontname="helv",
                    fontsize=12,
                )
                landscape = source.new_page(width=842, height=595)
                landscape.insert_text(
                    fitz.Point(72, 120),
                    "landscape body",
                    fontname="helv",
                    fontsize=12,
                )
                source.save(pdf_path)
            finally:
                source.close()

            with patch("modules.docgen.routes._pdf_cjk_font_path", return_value=""):
                _stamp_pdf_file_headers(pdf_path, chinese_name, english_name)

            document = fitz.open(pdf_path)
            try:
                self.assertEqual(document.page_count, 2)
                for page, body_text in zip(
                    document,
                    ("portrait body", "landscape body"),
                ):
                    page_text = page.get_text()
                    self.assertIn(chinese_name, page_text)
                    self.assertIn(english_name, page_text)
                    self.assertIn(body_text, page_text)
            finally:
                document.close()

    def test_multiple_fallback_documents_render_as_separate_pdfs(self):
        with tempfile.TemporaryDirectory() as output_dir:
            generated_paths = []
            for index, title in enumerate(("第一份材料", "第二份材料")):
                html = (
                    "<html><head><style>@page { size: A4; margin: 28mm 16mm 18mm; }</style></head>"
                    f"<body><h1>{title}</h1><p>{title}正文</p></body></html>"
                )
                pdf_path = os.path.join(output_dir, f"document-{index}.pdf")
                with patch("modules.docgen.routes._chrome_executable", return_value=None):
                    generated_paths.append(
                        _render_pdf_file(self.app, html, pdf_path, title)
                    )

            self.assertEqual(len(set(generated_paths)), 2)
            for pdf_path, title in zip(generated_paths, ("第一份材料", "第二份材料")):
                document = fitz.open(pdf_path)
                try:
                    self.assertIn(title, document[0].get_text())
                finally:
                    document.close()

    def test_combined_portrait_documents_preserve_body_scope_classes(self):
        documents = [
            {
                "label": "科研项目书",
                "html": (
                    '<html><head><style>.rd-project-document h1 { font-size: 15pt; }</style></head>'
                    '<body class="rd-project-document"><h1>科研项目书</h1></body></html>'
                ),
            },
            {
                "label": "制度正文",
                "html": (
                    '<html><head><style>.system-document { font-size: 10.5pt; }</style></head>'
                    '<body class="system-document"><h1>制度正文</h1></body></html>'
                ),
            },
        ]

        combined_html = _combine_portrait_export_documents(
            documents,
            "测试科技有限公司",
            "TEST TECHNOLOGY CO., LTD.",
        )

        self.assertIn(
            'class="batch-document batch-document-first rd-project-document"',
            combined_html,
        )
        self.assertIn(
            'class="batch-document system-document"',
            combined_html,
        )
        self.assertIn(".rd-project-document h1", combined_html)
        self.assertIn(".system-document", combined_html)
        self.assertEqual(
            combined_html.count('data-pymupdf-page-break-before="true"'),
            1,
        )

    def test_combined_rd_project_keeps_required_internal_page_breaks(self):
        template_folder = Path(__file__).resolve().parents[1] / "templates"
        app = Flask(__name__, template_folder=str(template_folder))
        data = {
            "gaoxin_relation_table": {
                "rows": [{
                    "year": "2025",
                    "rd_code": "RD03",
                    "rd_activity": "批量导出分页测试项目",
                    "rd_period": "2025-01-01至2025-12-31",
                }],
            },
        }
        project = _collect_rd_project_rows(data)[0]
        application_text = _export_rd_project_application_text(project, "")
        with app.app_context():
            project_html = render_template(
                "application_gaoxin_rd_project_print.html",
                company=SimpleNamespace(name="测试科技有限公司"),
                company_english_name="TEST TECHNOLOGY CO., LTD.",
                project=project,
                application_text=application_text,
                application_html=_rd_project_application_html(application_text),
                application_sections=_rd_project_application_sections(application_text),
            )

        combined_html = _combine_portrait_export_documents(
            [{"label": "科研项目书 RD03", "html": project_html}],
            "测试科技有限公司",
            "TEST TECHNOLOGY CO., LTD.",
        )

        self.assertEqual(combined_html.count('<section class="batch-document'), 7)
        self.assertEqual(
            combined_html.count('data-pymupdf-page-break-before="true"'),
            6,
        )

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                pdf_path = _render_export_pdf_file(
                    app,
                    combined_html,
                    output_dir,
                    "批量科研项目书",
                )

            self.assertIsNotNone(pdf_path)
            document = fitz.open(pdf_path)
            try:
                page_texts = [
                    re.sub(r"\s+", "", page.get_text())
                    for page in document
                ]
                headings = (
                    "研发项目立项通知书",
                    "一、项目基本情况与立项依据",
                    "五、研发项目验收报告",
                )
                heading_pages = [
                    next(
                        index
                        for index, page_text in enumerate(page_texts)
                        if heading in page_text
                    )
                    for heading in headings
                ]
                self.assertEqual(len(set(heading_pages)), len(headings))
                for page_index, heading in zip(heading_pages, headings):
                    first_body_text = next(
                        word[4]
                        for word in document[page_index].get_text(
                            "words",
                            sort=True,
                        )
                        if word[1] > 70
                    )
                    self.assertIn(first_body_text, heading)
            finally:
                document.close()

    def test_combined_portrait_documents_deduplicate_identical_styles(self):
        shared_style = ".shared-document h1 { font-size: 15pt; }"
        documents = [
            {
                "label": f"第{index + 1}份材料",
                "html": (
                    f"<html><head><style>{shared_style}</style></head>"
                    f'<body class="shared-document"><h1>第{index + 1}份材料</h1></body></html>'
                ),
            }
            for index in range(5)
        ]

        combined_html = _combine_portrait_export_documents(
            documents,
            "测试科技有限公司",
            "TEST TECHNOLOGY CO., LTD.",
        )

        self.assertEqual(combined_html.count(shared_style), 1)

    def test_combined_portrait_documents_render_once_with_distinct_page_ranges(self):
        documents = [
            {
                "label": title,
                "html": (
                    "<html><head><style>"
                    "@page { size: A4; margin: 28mm 16mm 18mm; }"
                    f".document-{index} h1 {{ font-size: 15pt; }}"
                    "</style></head>"
                    f'<body class="document-{index}"><h1>{title}</h1>'
                    f"<p>{title}正文</p></body></html>"
                ),
            }
            for index, title in enumerate(("第一份材料", "第二份材料"))
        ]
        combined_html = _combine_portrait_export_documents(
            documents,
            "测试科技有限公司",
            "TEST TECHNOLOGY CO., LTD.",
        )

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                pdf_path = _render_export_pdf_file(
                    self.app,
                    combined_html,
                    output_dir,
                    "批量内部材料",
                )

            self.assertIsNotNone(pdf_path)
            from modules.docgen.routes import _assign_portrait_document_page_ranges

            _assign_portrait_document_page_ranges(pdf_path, documents)
            self.assertEqual(documents[0]["pdf_path"], pdf_path)
            self.assertEqual(documents[1]["pdf_path"], pdf_path)
            self.assertEqual(documents[0]["from_page"], 0)
            self.assertEqual(documents[0]["to_page"], 0)
            self.assertEqual(documents[1]["from_page"], 1)
            self.assertEqual(documents[1]["to_page"], 1)

            document = fitz.open(pdf_path)
            try:
                self.assertEqual(document.page_count, 2)
                self.assertIn("第一份材料", document[0].get_text())
                self.assertNotIn("第二份材料", document[0].get_text())
                self.assertIn("第二份材料", document[1].get_text())
            finally:
                document.close()

    def test_portrait_documents_render_in_batches_with_page_ranges(self):
        documents = [
            {
                "label": f"第{index + 1}份材料",
                "html": (
                    "<html><head><style>"
                    "@page { size: A4; margin: 28mm 16mm 18mm; }"
                    "</style></head>"
                    f"<body><h1>第{index + 1}份材料</h1>"
                    f"<p>第{index + 1}份材料正文</p></body></html>"
                ),
            }
            for index in range(12)
        ]

        with tempfile.TemporaryDirectory() as output_dir:
            with patch("modules.docgen.routes._chrome_executable", return_value=None):
                pdf_paths = _render_portrait_export_document_batches(
                    self.app,
                    documents,
                    output_dir,
                    "测试科技有限公司",
                    "TEST TECHNOLOGY CO., LTD.",
                    batch_size=5,
                )

            self.assertEqual(len(pdf_paths), 3)
            self.assertEqual(len(set(pdf_paths)), 3)
            for index, document in enumerate(documents):
                batch_index = index // 5
                page_index = index % 5
                self.assertEqual(document["pdf_path"], pdf_paths[batch_index])
                self.assertEqual(document["from_page"], page_index)
                self.assertEqual(document["to_page"], page_index)

            page_counts = []
            for pdf_path in pdf_paths:
                rendered_pdf = fitz.open(pdf_path)
                try:
                    page_counts.append(rendered_pdf.page_count)
                finally:
                    rendered_pdf.close()
            self.assertEqual(page_counts, [5, 5, 2])

    def test_document_batches_group_matching_template_styles_across_export_order(self):
        def document(label, style, body_class):
            return {
                "label": label,
                "html": (
                    f"<html><head><style>{style}</style></head>"
                    f'<body class="{body_class}"><h1>{label}</h1></body></html>'
                ),
            }

        documents = [
            document("A1", ".template-a { font-size: 10pt; }", "template-a"),
            document("A2", ".template-a { font-size: 10pt; }", "template-a"),
            document("B1", ".template-b { font-size: 11pt; }", "template-b"),
            document("B2", ".template-b { font-size: 11pt; }", "template-b"),
            document("A3", ".template-a { font-size: 10pt; }", "template-a"),
        ]

        batches = _portrait_export_document_batches(documents, batch_size=5)

        self.assertEqual(
            [[document["label"] for document in batch] for batch in batches],
            [["A1", "A2", "A3"], ["B1", "B2"]],
        )

    def test_landscape_batch_combination_forces_landscape_pages(self):
        documents = [
            {
                "label": "横向材料",
                "html": (
                    "<html><head><style>@page { size: A4; margin: 20mm; }</style></head>"
                    '<body class="landscape-source"><h1>横向材料</h1></body></html>'
                ),
            },
        ]

        combined_html = _combine_landscape_export_documents(
            documents,
            "测试科技有限公司",
            "TEST TECHNOLOGY CO., LTD.",
        )

        self.assertIn("@page { size: A4 landscape; margin: 28mm 12mm 14mm; }", combined_html)
        self.assertIn("GAOXINPDFDOC", combined_html)

    def test_pymupdf_story_splits_oversized_table_rows_without_losing_text(self):
        long_text = (
            "第一段研发任务已经完成关键技术验证，并形成阶段性成果。"
            "第二段继续说明知识产权积累、产品验证和后续归档情况；"
            "第三段补充项目测试、应用场景和成果转化进展。"
        ) * 4
        html = (
            "<html><head></head><body><table><tbody>"
            "<tr><th>序号</th><th>任务</th><th>责任人</th></tr>"
            f"<tr><td>1</td><td>{long_text}</td><td>测试人员</td></tr>"
            "</tbody></table></body></html>"
        )

        prepared_html = _prepare_pymupdf_story_html(html, 500)

        from lxml import html as lxml_html

        root = lxml_html.document_fromstring(prepared_html)
        rows = root.xpath("//table/tbody/tr[not(contains(@class, 'pymupdf-table-sizer'))]")
        self.assertGreater(len(rows), 2)
        split_rows = rows[1:]
        reconstructed = "".join(
            " ".join(row.xpath("./td")[1].text_content().split())
            for row in split_rows
        )
        self.assertEqual(reconstructed, long_text)
        self.assertEqual(split_rows[0].xpath("./td")[0].text_content(), "1")
        self.assertEqual(split_rows[0].xpath("./td")[2].text_content(), "测试人员")
        self.assertTrue(
            all(not row.xpath("./td")[0].text_content() for row in split_rows[1:])
        )
        self.assertTrue(
            all(not row.xpath("./td")[2].text_content() for row in split_rows[1:])
        )

    def test_export_attachment_files_resolve_concurrently_and_deduplicate_paths(self):
        active_calls = 0
        max_active_calls = 0
        lock = threading.Lock()

        def resolve(local_path, relative_path):
            nonlocal active_calls, max_active_calls
            with lock:
                active_calls += 1
                max_active_calls = max(max_active_calls, active_calls)
            try:
                time.sleep(0.04)
                Path(local_path).parent.mkdir(parents=True, exist_ok=True)
                Path(local_path).write_bytes(relative_path.encode("utf-8"))
                return local_path
            finally:
                with lock:
                    active_calls -= 1

        references = [
            {"relative_path": "section/a.pdf", "label": "A"},
            {"relative_path": "section/b.pdf", "label": "B"},
            {"relative_path": "section/a.pdf", "label": "A副本"},
        ]
        with tempfile.TemporaryDirectory() as upload_dir:
            self.app.config.update(
                UPLOAD_FOLDER=upload_dir,
                PDF_ATTACHMENT_DOWNLOAD_WORKERS=3,
            )
            with patch(
                "modules.docgen.routes.ensure_local_file",
                side_effect=resolve,
            ) as mocked:
                resolved_count = _prepare_export_attachment_files(
                    self.app,
                    references,
                )

        self.assertEqual(mocked.call_count, 2)
        self.assertGreaterEqual(max_active_calls, 2)
        self.assertEqual(resolved_count, 3)
        self.assertEqual(references[0]["pdf_path"], references[2]["pdf_path"])
        self.assertTrue(all(reference.get("pdf_path") for reference in references))


if __name__ == "__main__":
    unittest.main()
