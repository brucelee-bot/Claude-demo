import unittest

from modules.docgen.product_terms import normalize_ps_reference_text


class ProductTermTests(unittest.TestCase):
    def test_service_text_removes_ambiguous_product_service_wording(self):
        text = (
            "高新技术产品（服务）汇总表中，本产品（服务）已建立资料对应关系，"
            "上年度高新技术产品（服务）情况表待复核，支撑产品服务优化和高新技术服务服务PS01。"
        )

        result = normalize_ps_reference_text(
            text,
            "电力系统继电保护整定优化与安全稳定分析服务",
            "service",
        )

        self.assertIn("高新技术服务汇总表", result)
        self.assertIn("本服务", result)
        self.assertIn("上年度高新技术服务情况表", result)
        self.assertIn("支撑服务优化", result)
        self.assertIn("高新技术服务PS01", result)
        self.assertNotIn("产品（服务）", result)
        self.assertNotIn("服务服务", result)

    def test_product_text_uses_product_wording(self):
        result = normalize_ps_reference_text(
            "本产品（服务）的产品（服务）名称应与证明材料一致。",
            "智能继电保护装置",
            "product",
        )

        self.assertEqual(result, "本产品的产品名称应与证明材料一致。")


if __name__ == "__main__":
    unittest.main()
