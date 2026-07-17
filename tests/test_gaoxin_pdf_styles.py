from pathlib import Path
import unittest


class GaoxinPdfStyleTests(unittest.TestCase):
    def setUp(self):
        self.template_dir = Path(__file__).resolve().parents[1] / "templates"
        self.print_templates = (
            "application_gaoxin_attachments_print.html",
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


if __name__ == "__main__":
    unittest.main()
