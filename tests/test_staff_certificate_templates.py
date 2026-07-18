import unittest
from pathlib import Path


class StaffCertificateTemplateTests(unittest.TestCase):
    def setUp(self):
        self.template_dir = Path(__file__).resolve().parents[1] / "templates"

    def test_edit_template_supports_batch_certificate_uploads_and_title_field(self):
        template = (
            self.template_dir / "application_gaoxin_attachments.html"
        ).read_text(encoding="utf-8")

        self.assertIn("毕业证书（PDF/图片）", template)
        self.assertIn("职称证书（PDF/图片）", template)
        self.assertIn('multiple data-staff-certificate-input', template)
        self.assertIn(".jpg,.jpeg,.png,.webp,.bmp,.tif,.tiff", template)
        self.assertIn("allowedExtensions = ['.pdf', '.jpg', '.jpeg', '.png'", template)
        self.assertIn("uploadStaffCertificates('education_certificate'", template)
        self.assertIn("uploadStaffCertificates('title_certificate'", template)
        self.assertIn("_title", template)

    def test_print_template_lists_title_and_certificate_attachments(self):
        attachment_template = (
            self.template_dir / "application_gaoxin_attachments_print.html"
        ).read_text(encoding="utf-8")
        roster_template = (
            self.template_dir / "application_gaoxin_staff_tables_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn("<th>职称</th>", roster_template)
        self.assertIn("education_certificate", attachment_template)
        self.assertIn("title_certificate", attachment_template)
        self.assertIn('class="staff-roster-table"', roster_template)
        self.assertIn('class="staff-id"', roster_template)
        self.assertIn(".staff-id { font-size: 8pt; }", roster_template)


if __name__ == "__main__":
    unittest.main()
