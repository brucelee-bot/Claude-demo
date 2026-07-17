import os
import tempfile
import unittest
from unittest.mock import patch

from flask import Flask

from modules.docgen.routes import (
    _chrome_executable,
    _render_export_pdf_file,
    _render_pdf_file,
    _stamp_pdf_file_headers,
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
                text = "".join(page.get_text() for page in document)
                self.assertIn("科研项目书", text)
                self.assertIn("测试科技有限公司", text)
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


if __name__ == "__main__":
    unittest.main()
