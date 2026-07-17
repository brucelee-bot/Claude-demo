import unittest
from pathlib import Path


class StaffStatementSectionTests(unittest.TestCase):
    def test_staff_difference_section_is_absent_from_edit_and_print_templates(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"

        for template_name in (
            "application_gaoxin_attachments.html",
            "application_gaoxin_attachments_print.html",
        ):
            with self.subTest(template=template_name):
                template = (template_dir / template_name).read_text(encoding="utf-8")
                self.assertNotIn("人员情况差异说明", template)
                self.assertNotIn("attachment_staff_difference_note", template)

    def test_december_roster_removes_entry_and_work_type_and_auto_syncs_counts(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"
        edit_template = (template_dir / "application_gaoxin_attachments.html").read_text(encoding="utf-8")
        print_template = (template_dir / "application_gaoxin_attachments_print.html").read_text(encoding="utf-8")

        self.assertNotIn("attachment_rd_staff_{{ i }}_entry_date", edit_template)
        self.assertNotIn("attachment_rd_staff_{{ i }}_work_type", edit_template)
        self.assertNotIn("<th>入职时间</th>", print_template)
        self.assertNotIn("<th>工作性质</th>", print_template)
        self.assertIn("function syncStaffMonthCounts()", edit_template)
        self.assertIn("readonly", edit_template)

    def test_annual_staff_averages_are_rendered_once_below_monthly_rows(self):
        template_dir = Path(__file__).resolve().parents[1] / "templates"

        for template_name in (
            "application_gaoxin_attachments.html",
            "application_gaoxin_attachments_print.html",
        ):
            with self.subTest(template=template_name):
                template = (template_dir / template_name).read_text(encoding="utf-8")
                self.assertNotIn("attachment_staff_month_{{ i }}_year_avg_total", template)
                self.assertNotIn("attachment_staff_month_{{ i }}_year_avg_tech", template)
                self.assertEqual(template.count("年平均职工总数"), 1)
                self.assertEqual(template.count("年平均科技人员数"), 1)
                self.assertIn("attachment_staff_year_avg_total", template)
                self.assertIn("attachment_staff_year_avg_tech", template)


if __name__ == "__main__":
    unittest.main()
