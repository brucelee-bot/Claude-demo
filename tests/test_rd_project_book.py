import unittest
from pathlib import Path

from modules.docgen.routes import (
    _build_staff_statement_from_rd_list,
    _collect_december_2025_rd_staff_rows,
    _collect_rd_project_rows,
    _collect_rd_staff_names,
    _rd_project_staff_assignment,
    _sync_staff_month_counts,
)


class RdProjectBookTests(unittest.TestCase):
    def test_project_number_comes_only_from_relation_table_rd_sequence(self):
        data = {
            "gaoxin_relation_table": {
                "rows": [{
                    "year": "2025",
                    "rd_code": "RD07",
                    "rd_activity": "测试研发项目",
                    "rd_period": "2025-01-01至2025-12-31",
                }],
            },
            "rd_0_no": "INTERNAL-OVERRIDE",
            "attachment_rd_project_0_rd_code": "SAVED-OVERRIDE",
        }

        project = _collect_rd_project_rows(data)[0]

        self.assertEqual(project["project_no"], "RD07")
        self.assertEqual(project["rd_code"], "RD07")

    def test_project_number_is_normalized_to_rd_with_two_digits(self):
        for raw_number, expected in (
            ("RD1", "RD01"),
            ("1", "RD01"),
            ("RD01", "RD01"),
            ("rd-12", "RD12"),
        ):
            with self.subTest(raw_number=raw_number):
                data = {
                    "gaoxin_relation_table": {
                        "rows": [{
                            "year": "2025",
                            "rd_code": raw_number,
                            "rd_activity": f"测试研发项目{raw_number}",
                            "rd_period": "2025-01-01至2025-12-31",
                        }],
                    },
                    "rd_0_no": "INTERNAL-OVERRIDE",
                }

                project = _collect_rd_project_rows(data)[0]

                self.assertEqual(project["project_no"], expected)
                self.assertEqual(project["rd_code"], raw_number)

    def test_missing_relation_sequence_falls_back_to_project_order(self):
        data = {
            "gaoxin_relation_table": {
                "rows": [
                    {
                        "year": "2025",
                        "rd_code": "",
                        "rd_activity": "第一研发项目",
                        "rd_period": "2025-01-01至2025-06-30",
                    },
                    {
                        "year": "2025",
                        "rd_code": "",
                        "rd_activity": "第二研发项目",
                        "rd_period": "2025-07-01至2025-12-31",
                    },
                ],
            },
            "rd_0_no": "SAVED-OVERRIDE",
            "rd_1_no": "RD99",
        }

        projects = _collect_rd_project_rows(data)

        self.assertEqual(
            [project["project_no"] for project in projects],
            ["RD01", "RD02"],
        )

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

        self.assertIn(
            '<table class="text-block keep-together" data-pymupdf-widths="100">',
            template,
        )
        self.assertIn(
            '<div class="table-heading">3. RD-IP-PS 关联明细</div>\n'
            '    <table class="keep-together" data-pymupdf-widths="20,80">',
            template,
        )
        self.assertNotIn('colspan="2" class="title-row"', template)
        self.assertNotIn("text_block('补充计划说明'", template)

    def test_print_template_uses_separate_cover_and_required_page_breaks(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "application_gaoxin_rd_project_print.html"
        ).read_text(encoding="utf-8")

        cover_end = template.index("</section>", template.index('<section class="cover-page">'))
        notice_start = template.index(
            '<section class="doc-part project-notice" data-pymupdf-page-break-before>'
        )
        basic_start = template.index(
            '<section class="doc-part" data-pymupdf-page-break-before>\n'
            '    <h2>一、项目基本情况与立项依据</h2>'
        )
        acceptance_start = template.index(
            '<section class="doc-part acceptance-part" data-pymupdf-page-break-before>\n'
            '    <h2>五、{{ temporal.acceptance_title }}</h2>'
        )

        self.assertLess(cover_end, notice_start)
        self.assertLess(notice_start, basic_start)
        self.assertLess(basic_start, acceptance_start)
        self.assertIn("page-break-after: always;", template)
        self.assertIn("page-break-before: always;", template)
        self.assertNotIn("project.rd_code", template)
        self.assertGreaterEqual(template.count("project.project_no"), 6)
        self.assertIn("企业研究开发项目管理文件", template)
        self.assertIn('class="cover-project-panel"', template)
        self.assertIn(
            "<tr><th>编制单位</th><td>{{ company.name }}</td></tr>",
            template,
        )
        self.assertIn('class="doc-part acceptance-part"', template)
        self.assertIn('class="table-stack keep-together acceptance-signoff"', template)

    def test_print_template_uses_uniform_table_border_widths(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "application_gaoxin_rd_project_print.html"
        ).read_text(encoding="utf-8")

        self.assertIn("border-top-width: 0.65pt;", template)
        self.assertIn("border-left-width: 0.65pt;", template)
        self.assertIn("border-right-width: 0.65pt;", template)
        self.assertIn("border-bottom-width: 0.65pt;", template)
        self.assertIn(
            ".rd-project-document .table-stack table + table { border-top: 0; }",
            template,
        )


if __name__ == "__main__":
    unittest.main()
