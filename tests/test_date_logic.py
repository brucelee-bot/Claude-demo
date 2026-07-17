import unittest
from datetime import date

from modules.docgen.date_logic import (
    enforce_transformation_wording,
    enforce_temporal_wording,
    evidence_record_date_context,
    event_date_context,
    parse_date_range,
    project_temporal_context,
    split_project_stages,
)


AS_OF = date(2026, 7, 16)


class DateLogicTests(unittest.TestCase):
    def test_completed_period_is_date_eligible_but_not_auto_accepted(self):
        temporal = project_temporal_context("2025.1.6-2025.9.28", AS_OF)

        self.assertEqual(temporal["status"], "已完成")
        self.assertEqual(temporal["status_display"], "已到计划结束时间")
        self.assertTrue(temporal["can_accept"])
        self.assertTrue(temporal["date_eligible_for_acceptance"])
        self.assertFalse(temporal["acceptance_record_supported"])
        self.assertEqual(temporal["issue_date"], "2025年1月6日")
        self.assertEqual(temporal["approval_date"], "2025年1月6日")
        self.assertEqual(temporal["acceptance_date"], "2025年9月28日")
        self.assertEqual(temporal["acceptance_date_label"], "验收日期")
        self.assertNotIn("同意验收", temporal["acceptance_result"])
        self.assertIn("实际验收结论", temporal["acceptance_result"])

    def test_ongoing_period_cannot_be_accepted(self):
        temporal = project_temporal_context("2026.01-2026.12", AS_OF)

        self.assertEqual(temporal["status"], "研发中")
        self.assertFalse(temporal["can_accept"])
        self.assertEqual(temporal["issue_date"], "2026年1月1日")
        self.assertEqual(temporal["acceptance_date"], "2026年12月31日")
        self.assertEqual(temporal["acceptance_date_label"], "计划验收日期")
        self.assertEqual(temporal["acceptance_signature_label"], "计划日期")
        self.assertIn("不得写", temporal["tense_instruction"])

    def test_future_period_remains_planned(self):
        temporal = project_temporal_context("2027年1月-2027年12月", AS_OF)

        self.assertEqual(temporal["status"], "计划中")
        self.assertFalse(temporal["can_accept"])
        self.assertEqual(temporal["start_display"], "2027年1月")

    def test_missing_or_invalid_period_is_not_inferred(self):
        missing = project_temporal_context("", AS_OF)
        invalid = project_temporal_context("时间另行安排", AS_OF)

        self.assertEqual(missing["status"], "待补充")
        self.assertEqual(invalid["status"], "待补充")
        self.assertFalse(missing["stages"])
        self.assertFalse(invalid["can_accept"])

    def test_open_ended_period_is_ongoing(self):
        temporal = project_temporal_context("2023年至今", AS_OF)

        self.assertEqual(temporal["status"], "研发中")
        self.assertFalse(temporal["can_accept"])
        self.assertEqual(temporal["end_display"], "至今")
        self.assertFalse(temporal["stages"])

    def test_stage_ranges_are_ordered_non_overlapping_and_bounded(self):
        parsed = parse_date_range("2026/01/01至2026/12/31")
        stages = split_project_stages(parsed["start"], parsed["end"], AS_OF)

        self.assertEqual(len(stages), 4)
        self.assertEqual(stages[0]["start_iso"], "2026-01-01")
        self.assertEqual(stages[-1]["end_iso"], "2026-12-31")
        for previous, current in zip(stages, stages[1:]):
            previous_end = date.fromisoformat(previous["end_iso"])
            current_start = date.fromisoformat(current["start_iso"])
            self.assertEqual((current_start - previous_end).days, 1)
        self.assertEqual(stages[0]["status"], "已到计划节点")
        self.assertNotIn("已完成", {stage["status"] for stage in stages})

    def test_temporal_wording_is_downgraded_without_corrupting_not_completed(self):
        ongoing = project_temporal_context("2026.01-2026.12", AS_OF)
        text = "项目尚未完成，已完成开发并已形成成果，同意验收。"
        result = enforce_temporal_wording(text, ongoing)

        self.assertIn("尚未完成", result)
        self.assertIn("正在推进开发", result)
        self.assertIn("阶段形成成果", result)
        self.assertNotIn("同意验收", result)

    def test_ended_period_does_not_preserve_unsupported_acceptance_claims(self):
        ended = project_temporal_context("2023.4.8-2023.11.29", AS_OF)
        text = (
            "该项目达到预期目标，同意验收！"
            "验收小组认为该项目开发是成功的。"
            "该项目经总经办检测，符合项目技术要求。"
        )
        result = enforce_temporal_wording(text, ended)

        self.assertNotIn("同意验收", result)
        self.assertNotIn("开发是成功的", result)
        self.assertNotIn("符合项目技术要求", result)
        self.assertIn("实际验收结论", result)

    def test_transformation_claims_are_neutralized_without_records(self):
        ended = project_temporal_context("2023.4.8-2023.11.29", AS_OF)
        text = (
            "成果转化成功证明材料\n"
            "成果转化方式：自行投资，实施转化。"
            "该成果并应用于PS01，提升了运行效率。"
        )
        result = enforce_transformation_wording(text, ended)

        self.assertIn("成果转化核验材料", result)
        self.assertNotIn("实施转化", result)
        self.assertNotIn("提升了", result)
        self.assertNotIn("转化成功证明材料", result)
        self.assertIn("待核实", result)

    def test_neutral_achievement_description_keeps_complete_evidence_chain(self):
        from modules.docgen.routes import _neutral_achievement_description

        text = _neutral_achievement_description({
            "result_name": "智能校核技术成果",
            "rd_code": "RD01",
            "rd_name": "电网继电保护智能校核",
            "period": "2023.4.8-2023.11.29",
            "ip_code": "IP01",
            "ip_name": "基于EMS的电网在线继电保护",
            "ip_auth_no": "ZL20230001",
            "ps": "PS01 - 继电保护整定服务",
            "ps_name": "继电保护整定服务",
            "ps_kind": "service",
            "technology": "继电保护定值校核与风险识别",
        })

        for label in (
            "科技成果名称：",
            "项目时间状态：已到计划结束时间",
            "关联研发项目：RD01",
            "计划周期：2023.4.8-2023.11.29",
            "关联知识产权：IP01",
            "关联服务：PS01",
            "技术关联说明：",
            "时间及状态说明：",
            "核验说明：",
            "成果转化核验材料：",
        ):
            self.assertIn(label, text)
        for unsupported_claim in ("完成转化", "投入应用", "提升了", "验收通过"):
            self.assertNotIn(unsupported_claim, text)

    def test_record_date_before_project_start_is_planned(self):
        temporal = project_temporal_context(
            "2023.4.8-2023.11.29",
            date(2023, 1, 20),
        )
        result = enforce_temporal_wording(
            "项目完成了开发，形成了成果并应用于产品，材料已归档。",
            temporal,
        )

        self.assertEqual(temporal["status"], "计划中")
        self.assertIn("计划完成", result)
        self.assertIn("拟形成", result)
        self.assertIn("拟应用于", result)
        self.assertIn("拟归档", result)

    def test_future_event_date_is_not_treated_as_occurred(self):
        context = event_date_context("2027年01月20日", AS_OF)

        self.assertEqual(context["status"], "未来计划")
        self.assertTrue(context["is_future"])
        self.assertFalse(context["can_claim_occurred"])

    def test_project_bound_evidence_rejects_date_before_project_start(self):
        context = evidence_record_date_context(
            "研发投入归集审批表",
            "2023年01月20日",
            "2023.4.8-2023.11.29",
            AS_OF,
        )

        self.assertEqual(context["event_type"], "project_bound")
        self.assertTrue(context["before_project_start"])
        self.assertFalse(context["usable"])
        self.assertEqual(context["display"], "待根据实际记录填写")
        self.assertEqual(context["status_display"], "待根据实际记录填写")

    def test_project_initiation_may_precede_project_start(self):
        context = evidence_record_date_context(
            "研发项目立项申请表",
            "2023年01月20日",
            "2023.4.8-2023.11.29",
            AS_OF,
        )

        self.assertEqual(context["event_type"], "pre_project")
        self.assertTrue(context["usable"])
        self.assertFalse(context["before_project_start"])
        self.assertEqual(context["display"], "2023年01月20日")
        self.assertEqual(context["status_display"], "计划中")

    def test_project_bound_evidence_accepts_actual_date_after_start(self):
        context = evidence_record_date_context(
            "研发费用辅助账月度登记表",
            "2023年06月30日",
            "2023.4.8-2023.11.29",
            AS_OF,
        )

        self.assertTrue(context["usable"])
        self.assertEqual(context["display"], "2023年06月30日")
        self.assertEqual(context["status_display"], "研发中")


if __name__ == "__main__":
    unittest.main()
