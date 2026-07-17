import unittest
from unittest.mock import patch

from modules.docgen.routes import (
    STAFF_CERTIFICATE_UPLOAD_EXTENSIONS,
    _extract_staff_certificate_text,
)
from modules.docgen.staff_certificates import (
    analyze_staff_certificate,
    extract_education,
    extract_professional_title,
)


class StaffCertificateTests(unittest.TestCase):
    def setUp(self):
        self.staff_rows = [
            {"index": 0, "name": "詹吉庆"},
            {"index": 1, "name": "何涛"},
            {"index": 2, "name": "周天天"},
        ]

    def test_education_certificate_matches_roster_name_and_degree(self):
        result = analyze_staff_certificate(
            "普通高等学校毕业证书 姓名 詹 吉 庆，完成大学本科学习，准予毕业。",
            "毕业证.pdf",
            "education_certificate",
            self.staff_rows,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["matched_index"], 0)
        self.assertEqual(result["matched_name"], "詹吉庆")
        self.assertEqual(result["value"], "本科")

    def test_filename_can_supply_exact_roster_name(self):
        result = analyze_staff_certificate(
            "专业技术资格证书 资格名称：高级工程师",
            "何涛_职称证书.pdf",
            "title_certificate",
            self.staff_rows,
        )

        self.assertEqual(result["status"], "matched")
        self.assertEqual(result["matched_index"], 1)
        self.assertEqual(result["value"], "高级工程师")

    def test_unknown_name_does_not_write_to_roster(self):
        result = analyze_staff_certificate(
            "姓名：张三，硕士研究生学历。",
            "张三毕业证.pdf",
            "education_certificate",
            self.staff_rows,
        )

        self.assertEqual(result["status"], "name_not_found")
        self.assertIsNone(result["matched_index"])
        self.assertEqual(result["value"], "硕士")

    def test_missing_certificate_value_is_reported(self):
        result = analyze_staff_certificate(
            "姓名：周天天，证书编号：123456。",
            "证书.pdf",
            "title_certificate",
            self.staff_rows,
        )

        self.assertEqual(result["status"], "value_not_found")
        self.assertEqual(result["matched_name"], "周天天")
        self.assertEqual(result["value"], "")

    def test_education_and_title_extractors_use_specific_values(self):
        self.assertEqual(extract_education("授予工学学士学位"), "本科")
        self.assertEqual(extract_professional_title("任职资格：助理工程师"), "助理工程师")

    def test_certificate_upload_supports_pdf_and_common_image_formats(self):
        self.assertEqual(
            STAFF_CERTIFICATE_UPLOAD_EXTENSIONS,
            {".pdf", ".jpg", ".jpeg", ".png", ".webp", ".bmp", ".tif", ".tiff"},
        )

    def test_certificate_text_extraction_dispatches_images_to_ocr(self):
        with patch(
            "modules.docgen.routes._extract_image_text_with_cli_ocr",
            return_value="姓名：何涛 学历：硕士",
        ) as image_ocr:
            text = _extract_staff_certificate_text("/tmp/何涛毕业证书.JPG")

        self.assertEqual(text, "姓名：何涛 学历：硕士")
        image_ocr.assert_called_once_with("/tmp/何涛毕业证书.JPG")


if __name__ == "__main__":
    unittest.main()
