import unittest
from pathlib import Path

from modules.docgen.routes import (
    _build_staff_statement_from_rd_list,
    _collect_december_2025_rd_staff_rows,
    _collect_rd_staff_names,
    _rd_project_staff_assignment,
    _sync_staff_month_counts,
)


class RdProjectBookTests(unittest.TestCase):
    def test_collect_rd_staff_prefers_maintained_order_and_deduplicates(self):
        data = {
            "attachment_rd_staff_0_name": "张三",
            "attachment_rd_staff_1_name": "李四",
            "hr_staff_rows": [
                {"姓名": "李四", "是否科技人员": "是"},
                {"姓名": "王五", "是否科技人员": "是"},
                {"姓名": "赵六", "是否科技人员": "否"},
            ],
        }

        self.assertEqual(_collect_rd_staff_names(data), ["张三", "李四", "王五"])

    def test_project_leader_rotates_stably_within_rd_staff(self):
        names = ["詹吉庆", "何涛", "周天天", "杜建坤", "李昕"]

        first = _rd_project_staff_assignment(names, 0)
        sixth = _rd_project_staff_assignment(names, 5)

        self.assertEqual(first["leader"], "詹吉庆")
        self.assertEqual(sixth["leader"], "詹吉庆")
        assigned_names = {
            name
            for value in first.values()
            for name in value.split("、")
        }
        self.assertTrue(assigned_names <= set(names))

    def test_missing_staff_remains_explicit(self):
        assignment = _rd_project_staff_assignment([], 0)

        self.assertEqual(assignment["leader"], "待补充")
        self.assertTrue(all(value == "待补充" for value in assignment.values()))

    def test_december_roster_counts_only_rows_with_names(self):
        data = {
            "attachment_rd_staff_0_seq": "1",
            "attachment_rd_staff_0_name": "张三",
            "attachment_rd_staff_0_contract": "是",
            "attachment_rd_staff_1_seq": "2",
            "attachment_rd_staff_1_name": "",
            "attachment_rd_staff_1_contract": "是",
            "attachment_rd_staff_2_seq": "3",
            "attachment_rd_staff_2_name": "李四",
            "attachment_rd_staff_2_contract": "否",
            "hr_staff_rows": [{"姓名": "不应回填"}],
        }

        rows = _collect_december_2025_rd_staff_rows(data)

        self.assertEqual([row["name"] for row in rows], ["张三", "李四"])
        self.assertEqual(rows[1]["seq"], "3")

    def test_december_roster_falls_back_to_imported_staff_rows(self):
        data = {
            "hr_staff_rows": [
                {"序号": "1", "姓名": "王五", "是否签订合同": "是"},
                {"序号": "2", "姓名": "", "是否签订合同": "是"},
            ],
        }

        rows = _collect_december_2025_rd_staff_rows(data)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], "王五")
        self.assertEqual(rows[0]["contract"], "是")
        self.assertEqual(rows[0]["is_tech"], "是")
        self.assertNotIn("entry_date", rows[0])
        self.assertNotIn("work_type", rows[0])

    def test_monthly_staff_counts_follow_named_december_roster(self):
        data = {
            "attachment_rd_staff_0_name": "张三",
            "attachment_rd_staff_0_is_tech": "是",
            "attachment_rd_staff_1_name": "李四",
            "attachment_rd_staff_1_is_tech": "否",
            "attachment_rd_staff_2_name": "王五",
            "attachment_rd_staff_2_is_tech": "",
            "attachment_rd_staff_3_name": "",
            "attachment_rd_staff_3_is_tech": "是",
            "attachment_staff_month_0_start_total": "999",
        }

        _sync_staff_month_counts(data)

        for index in range(12):
            self.assertEqual(data[f"attachment_staff_month_{index}_month"], f"2025年{index + 1}月")
            for field in ("start_total", "end_total", "avg_total"):
                self.assertEqual(data[f"attachment_staff_month_{index}_{field}"], 3)
            for field in ("start_tech", "end_tech", "avg_tech"):
                self.assertEqual(data[f"attachment_staff_month_{index}_{field}"], 2)
            self.assertNotIn(f"attachment_staff_month_{index}_year_avg_total", data)
            self.assertNotIn(f"attachment_staff_month_{index}_year_avg_tech", data)
        self.assertEqual(data["attachment_staff_year_avg_total"], 3)
        self.assertEqual(data["attachment_staff_year_avg_tech"], 2)

    def test_staff_statement_uses_roster_facts_without_old_sample_values(self):
        data = {
            "attachment_rd_staff_0_name": "张三",
            "attachment_rd_staff_0_contract": "是",
            "attachment_rd_staff_0_social_security": "是",
            "attachment_rd_staff_0_is_tech": "是",
            "attachment_rd_staff_0_title": "高级工程师",
            "attachment_rd_staff_1_name": "李四",
            "attachment_rd_staff_1_contract": "是",
            "attachment_rd_staff_1_social_security": "是",
            "attachment_rd_staff_1_is_tech": "",
        }

        statement = _build_staff_statement_from_rd_list(data, "测试公司")

        self.assertIn("截至2025年12月31日", statement)
        self.assertIn("名单共登记2人", statement)
        self.assertIn("张三、李四", statement)
        self.assertIn("名单所列2人均已签订劳动合同", statement)
        self.assertIn("名单所列2人均明确标注为科技人员", statement)
        self.assertIn("科技人员共2人，占总人数的100%", statement)
        self.assertIn("职称分布为高级工程师1人", statement)
        self.assertNotIn("工作性质", statement)
        self.assertNotIn("入职时间", statement)
        self.assertNotIn("121人", statement)
        self.assertNotIn("19人", statement)
        self.assertNotIn("2022年", statement)

    def test_staff_statement_calculates_technology_staff_ratio(self):
        data = {
            "attachment_rd_staff_0_name": "张三",
            "attachment_rd_staff_0_is_tech": "是",
            "attachment_rd_staff_1_name": "李四",
            "attachment_rd_staff_1_is_tech": "否",
            "attachment_rd_staff_2_name": "王五",
            "attachment_rd_staff_2_is_tech": "是",
        }

        statement = _build_staff_statement_from_rd_list(data, "测试公司")

        self.assertIn("科技人员共2人，占总人数的66.67%", statement)

    def test_print_template_keeps_narrative_and_relation_tables_together(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "application_gaoxin_rd_project_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn('<table class="text-block keep-together">', template)
        self.assertIn(
            '<table class="keep-together">\n'
            '      <thead><tr><th colspan="2" class="title-row">'
            "3. RD-IP-PS 关联明细",
            template,
        )
        self.assertNotIn("text_block('补充计划说明'", template)


if __name__ == "__main__":
    unittest.main()
