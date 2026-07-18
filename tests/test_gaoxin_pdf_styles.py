from pathlib import Path
import unittest


class GaoxinPdfStyleTests(unittest.TestCase):
    def setUp(self):
        self.template_dir = Path(__file__).resolve().parents[1] / "templates"
        self.print_templates = (
            "application_gaoxin_attachments_print.html",
            "application_gaoxin_ip_detail_print.html",
            "application_gaoxin_staff_tables_print.html",
            "application_gaoxin_rd_project_print.html",
            "application_gaoxin_ps_statement_print.html",
            "application_gaoxin_hitech_product_summary_print.html",
            "application_gaoxin_achievement_evidence_print.html",
            "application_gaoxin_achievement_summary_print.html",
            "application_gaoxin_system_doc_print.html",
            "application_gaoxin_system_evidence_print.html",
            "application_gaoxin_system_summary_print.html",
            "application_gaoxin_system_attachment_notice_print.html",
        )

    def test_all_gaoxin_print_templates_include_shared_pdf_styles(self):
        for template_name in self.print_templates:
            with self.subTest(template=template_name):
                template = (self.template_dir / template_name).read_text(encoding="utf-8")
                self.assertIn('{% include "_gaoxin_pdf_styles.html" %}', template)

    def test_gaoxin_print_templates_do_not_restore_web_style_artifacts(self):
        forbidden_fragments = (
            "Microsoft YaHei",
            "font-size: 30px",
            "border-left: 4px",
            "border-radius:",
            "#f97316",
        )
        for template_name in self.print_templates:
            template = (self.template_dir / template_name).read_text(encoding="utf-8")
            for fragment in forbidden_fragments:
                with self.subTest(template=template_name, fragment=fragment):
                    self.assertNotIn(fragment, template)

    def test_shared_styles_prioritize_reference_songti_fonts_and_print_contrast(self):
        shared_styles = (self.template_dir / "_gaoxin_pdf_styles.html").read_text(
            encoding="utf-8"
        )
        header_styles = (
            self.template_dir / "_generated_document_header_styles.html"
        ).read_text(encoding="utf-8")

        expected_font_stack = (
            '"Songti SC", "STSongti-SC", "STSong", "SimSun", serif'
        )
        self.assertIn(expected_font_stack, shared_styles)
        self.assertIn(expected_font_stack, header_styles)
        self.assertIn("color: #17212b;", shared_styles)
        self.assertIn("border-top: 0.65pt solid #8f99a7;", shared_styles)
        self.assertIn("border-left: 0.65pt solid #8f99a7;", shared_styles)
        self.assertIn("border-right: 0.65pt solid #8f99a7;", shared_styles)
        self.assertIn("border-bottom: 0.65pt solid #8f99a7;", shared_styles)
        self.assertNotIn("border: 0.65pt solid #8f99a7;", shared_styles)
        self.assertIn("border-collapse: separate;", shared_styles)
        self.assertIn("border-spacing: 0;", shared_styles)
        self.assertIn("background-color: #dfe7ef;", shared_styles)
        self.assertIn("background-color: #e9edf2;", shared_styles)
        self.assertNotIn("background: #", shared_styles)
        self.assertIn("text-align: center !important;", shared_styles)
        self.assertIn("vertical-align: middle !important;", shared_styles)

    def test_shared_table_grid_draws_each_edge_once(self):
        shared_styles = (self.template_dir / "_gaoxin_pdf_styles.html").read_text(
            encoding="utf-8"
        )

        table_rule = shared_styles.split("table {", 1)[1].split("}", 1)[0]
        cell_rule = shared_styles.split("th,\ntd {", 1)[1].split("}", 1)[0]
        self.assertIn("border-top:", table_rule)
        self.assertIn("border-left:", table_rule)
        self.assertNotIn("border-right:", table_rule)
        self.assertNotIn("border-bottom:", table_rule)
        self.assertIn("border-right:", cell_rule)
        self.assertIn("border-bottom:", cell_rule)
        self.assertNotIn("border-top:", cell_rule)
        self.assertNotIn("border-left:", cell_rule)

    def test_requested_summary_tables_define_pymupdf_column_ratios(self):
        expectations = {
            "application_gaoxin_achievement_summary_print.html": (
                'data-pymupdf-widths="9,25,27,23,16"',
            ),
            "application_gaoxin_system_summary_print.html": (
                'data-pymupdf-widths="18,30,52"',
            ),
            "application_gaoxin_achievement_evidence_print.html": (
                'data-pymupdf-widths="16,34,16,34"',
                'data-pymupdf-widths="16,84"',
                'data-pymupdf-widths="20,80"',
                'data-pymupdf-widths="20,30,20,30"',
            ),
            "application_gaoxin_rd_project_print.html": (
                'data-pymupdf-widths="8,16,18,46,12"',
                'data-pymupdf-widths="8,18,30,44"',
            ),
        }
        for template_name, fragments in expectations.items():
            template = (self.template_dir / template_name).read_text(encoding="utf-8")
            for fragment in fragments:
                with self.subTest(template=template_name, fragment=fragment):
                    self.assertIn(fragment, template)

    def test_system_summary_is_a_fixed_landscape_table(self):
        template = (
            self.template_dir / "application_gaoxin_system_summary_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn("@page { size: A4 landscape;", template)
        self.assertIn("table { table-layout: fixed; }", template)

    def test_requested_document_groups_use_stronger_reference_hierarchy(self):
        expectations = {
            "application_gaoxin_rd_project_print.html": (
                "background-color: #dfe7ef;",
                "企业研究开发项目管理文件",
                "cover-project-panel",
                'class="table-heading">3. 阶段计划与里程碑',
                'class="table-heading">3. RD-IP-PS 关联明细',
                'class="table-heading">1. 验收指标对照',
            ),
            "application_gaoxin_achievement_evidence_print.html": (
                "科技成果转化证明材料",
                "成果转化情况说明",
            ),
            "application_gaoxin_system_doc_print.html": (
                'class="chapter"',
                'class="article"',
            ),
            "application_gaoxin_system_evidence_print.html": (
                'class="section-band"',
                'class="{{ \'key-cell\' if loop.index is odd else \'value-cell\' }}"',
                'data-pymupdf-widths="{{ table.pymupdf_widths }}"',
            ),
        }
        for template_name, fragments in expectations.items():
            template = (self.template_dir / template_name).read_text(encoding="utf-8")
            for fragment in fragments:
                with self.subTest(template=template_name, fragment=fragment):
                    self.assertIn(fragment, template)

    def test_achievement_evidence_avoids_story_colspan_layouts(self):
        template = (
            self.template_dir / "application_gaoxin_achievement_evidence_print.html"
        ).read_text(encoding="utf-8")
        self.assertNotIn("colspan=", template)
        self.assertIn('class="table-heading">{{ achievement.type_label }}使用情况', template)
        self.assertIn(
            'class="table-heading">客户对{{ achievement.type_label }}的评价',
            template,
        )

    def test_rd_project_table_headings_do_not_use_story_colspan_rows(self):
        template = (
            self.template_dir / "application_gaoxin_rd_project_print.html"
        ).read_text(encoding="utf-8")
        for heading in (
            "3. 阶段计划与里程碑",
            "3. RD-IP-PS 关联明细",
            "1. 验收指标对照",
        ):
            with self.subTest(heading=heading):
                self.assertNotRegex(
                    template,
                    rf"<th[^>]+colspan=[^>]*>{heading}</th>",
                )

    def test_rd_project_avoids_story_colspan_layouts(self):
        template = (
            self.template_dir / "application_gaoxin_rd_project_print.html"
        ).read_text(encoding="utf-8")

        self.assertNotIn("colspan=", template)
        self.assertIn('class="cover-meta keep-together"', template)
        self.assertIn(
            "<div class=\"cover-project-name\">{{ project.rd_activity or '项目名称待补充' }}</div>",
            template,
        )
        self.assertIn(
            "<tr><th>项目名称</th><td>{{ project.rd_activity or '待补充' }}</td></tr>",
            template,
        )
        self.assertIn(
            '<tr><th>审批意见</th><td class="signature-space">',
            template,
        )
        self.assertNotIn("<th>填写依据</th>", template)

    def test_combined_portrait_templates_scope_document_specific_rules(self):
        expected_body_classes = {
            "application_gaoxin_rd_project_print.html": "rd-project-document",
            "application_gaoxin_ps_statement_print.html": "ps-statement-document",
            "application_gaoxin_achievement_evidence_print.html": "achievement-evidence-document",
            "application_gaoxin_system_doc_print.html": "system-document",
            "application_gaoxin_system_evidence_print.html": "system-evidence-document",
            "application_gaoxin_system_attachment_notice_print.html": "system-attachment-notice-document",
        }
        for template_name, body_class in expected_body_classes.items():
            with self.subTest(template=template_name):
                template = (self.template_dir / template_name).read_text(encoding="utf-8")
                self.assertIn(f'<body class="{body_class}">', template)

    def test_all_attachment_sections_start_on_a_new_page(self):
        template = (
            self.template_dir / "application_gaoxin_attachments_print.html"
        ).read_text(encoding="utf-8")
        routes = (
            Path(__file__).resolve().parents[1] / "modules" / "docgen" / "routes.py"
        ).read_text(encoding="utf-8")

        self.assertIn(
            '<section class="section {% if not loop.first %}section-break{% endif %}"{% if not loop.first %} data-pymupdf-page-break-before{% endif %}>',
            template,
        )
        self.assertIn(
            '<span class="section-marker">GAOXINSECTION{{ section.no }}</span>',
            template,
        )
        self.assertIn("break-before: page;", template)
        self.assertIn("attachment_sections=export_sections", routes)

    def test_wide_attachment_tables_are_landscape_documents(self):
        ip_template = (
            self.template_dir / "application_gaoxin_ip_detail_print.html"
        ).read_text(encoding="utf-8")
        staff_template = (
            self.template_dir / "application_gaoxin_staff_tables_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn("@page { size: A4 landscape;", ip_template)
        self.assertIn(
            'data-pymupdf-widths="6,19,10,10,15,12,16,12"',
            ip_template,
        )
        self.assertIn("@page { size: A4 landscape;", staff_template)
        self.assertIn("2025年12月份研发人员名单表", staff_template)
        self.assertIn(
            'data-pymupdf-widths="5,8,22,13,13,10,13,16"',
            staff_template,
        )
        self.assertIn("data-pymupdf-page-break-before", staff_template)

    def test_ps_technical_field_spans_three_value_columns(self):
        template = (
            self.template_dir / "application_gaoxin_ps_statement_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn('data-pymupdf-widths="16,84"', template)
        self.assertIn(
            '<tr><th>技术领域</th><td>{{ product.field or \'—\' }}</td></tr>',
            template,
        )
        self.assertNotIn("colspan=", template)


if __name__ == "__main__":
    unittest.main()
