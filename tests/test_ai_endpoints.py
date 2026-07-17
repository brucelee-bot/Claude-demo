import json
import unittest
from unittest.mock import MagicMock, patch

import requests
from flask import Flask
from flask_login import LoginManager

from models import Company, ScoreRecord, User, db
from modules.ai.analyzer import analyze
from modules.ai.llm_client import call_llm
from modules.docgen.routes import docgen_bp
from modules.parser import finance_extractor
from modules.parser.routes import parser_bp
from modules.scoring.routes import scoring_bp


AI_WRITE_FIELDS = (
    "purpose",
    "innovation",
    "result",
    "rd_application",
    "hitech_product_summary",
    "ps_statement",
    "ps_tech",
    "ps_advantage",
    "ps_support",
    "innovation_ip",
    "innovation_transform",
    "innovation_rd_mgmt",
    "innovation_staff",
    "cv_desc",
    "achievement_test_report",
    "achievement_user_report",
)


class AiRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask(__name__)
        app.config.update(
            TESTING=True,
            SECRET_KEY="test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
        )
        db.init_app(app)
        login_manager = LoginManager(app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        app.register_blueprint(parser_bp, url_prefix="/parser")
        app.register_blueprint(docgen_bp, url_prefix="/application")
        app.register_blueprint(scoring_bp, url_prefix="/score")
        cls.app = app

        with app.app_context():
            db.create_all()
            user = User(username="ai-test", password_hash="test")
            db.session.add(user)
            db.session.flush()
            company = Company(
                user_id=user.id,
                name="测试科技有限公司",
                app_type="高新技术",
                data_json="{}",
            )
            db.session.add(company)
            db.session.commit()
            cls.user_id = user.id
            cls.company_id = company.id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.drop_all()

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

    def test_company_intro_route_succeeds_and_caps_target_words(self):
        with patch(
            "modules.ai.llm_client.call_llm",
            return_value={"success": True, "content": "测试企业专注于技术研发。"},
        ) as mocked:
            response = self.client.post(
                "/parser/ai_polish_intro",
                json={
                    "company_name": "测试科技有限公司",
                    "draft": "从事软件开发",
                    "target_words": 9999,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["target_words"], 2000)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 45)
        self.assertEqual(mocked.call_args.kwargs["max_attempts"], 2)

    def test_company_intro_rejects_empty_draft_without_calling_ai(self):
        with patch("modules.ai.llm_client.call_llm") as mocked:
            response = self.client.post(
                "/parser/ai_polish_intro",
                json={"draft": "", "target_words": 500},
            )

        self.assertEqual(response.status_code, 400)
        self.assertFalse(mocked.called)

    def test_business_scope_route_uses_short_request_policy(self):
        with patch(
            "modules.ai.llm_client.call_llm",
            return_value={"success": True, "content": "一般项目：软件开发。"},
        ) as mocked:
            response = self.client.post(
                "/parser/ai_polish_business_scope",
                json={"draft": "软件开发", "target_words": 180},
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 35)
        self.assertEqual(mocked.call_args.kwargs["max_attempts"], 2)

    def test_all_ai_write_fields_return_success_with_mocked_upstream(self):
        for field in AI_WRITE_FIELDS:
            with self.subTest(field=field), patch(
                "modules.ai.llm_client.call_llm",
                return_value={"success": True, "content": "生成内容"},
            ):
                response = self.client.post(
                    "/parser/ai_write",
                    json={
                        "field": field,
                        "context": {
                            "company_name": "测试科技有限公司",
                            "rd_name": "智能校核技术研发",
                            "rd_period": "2025.01-2025.12",
                            "ps_name": "智能校核服务",
                        },
                        "target_words": 3000 if field == "rd_application" else 400,
                    },
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["success"])

    def test_rd_application_allows_3000_words_and_does_not_retry(self):
        with patch(
            "modules.ai.llm_client.call_llm",
            return_value={"success": True, "content": "科研项目书正文"},
        ) as mocked:
            response = self.client.post(
                "/parser/ai_write",
                json={
                    "field": "rd_application",
                    "context": {
                        "company_name": "测试科技有限公司",
                        "rd_name": "智能校核技术研发",
                    },
                    "target_words": 3000,
                },
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.get_json()["target_words"], 3000)
        self.assertEqual(mocked.call_count, 3)
        self.assertEqual(mocked.call_args.kwargs["timeout"], 40)
        self.assertEqual(mocked.call_args.kwargs["max_attempts"], 2)
        self.assertEqual(mocked.call_args.kwargs["max_tokens"], 1000)

    def test_rd_application_reports_parallel_section_failure(self):
        with patch(
            "modules.ai.llm_client.call_llm",
            side_effect=(
                {"success": True, "content": "第一组"},
                {"success": False, "error": "上游超时"},
                {"success": True, "content": "第三组"},
            ),
        ):
            response = self.client.post(
                "/parser/ai_write",
                json={
                    "field": "rd_application",
                    "context": {"rd_name": "智能校核技术研发"},
                    "target_words": 3000,
                },
            )

        self.assertEqual(response.status_code, 500)
        self.assertIn("分段生成失败", response.get_json()["error"])

    def test_ai_write_returns_readable_upstream_error(self):
        with patch(
            "modules.ai.llm_client.call_llm",
            return_value={"success": False, "error": "上游模型超时"},
        ):
            response = self.client.post(
                "/parser/ai_write",
                json={"field": "purpose", "context": {}, "target_words": 400},
            )

        self.assertEqual(response.status_code, 500)
        self.assertEqual(response.get_json()["error"], "上游模型超时")

    def test_relation_ai_routes(self):
        cases = (
            (
                "generate_result",
                {"row": {"ip_name": "智能校核专利", "sales_contract_keywords": "继电保护服务"}},
                {"success": True, "content": '{"result_name":"智能校核技术成果"}'},
                "result_name",
            ),
            (
                "generate_rd_activity",
                {"rows": [{"ip_name": "智能校核专利", "result_name": "智能校核技术成果"}]},
                {"success": True, "content": '{"rd_activity":"继电保护智能校核技术的研发"}'},
                "rd_activity",
            ),
            (
                "generate_ps_name",
                {"rows": [{"rd_activity": "继电保护智能校核技术的研发"}]},
                {"success": True, "content": '{"ps_name":"继电保护智能校核服务"}'},
                "ps_name",
            ),
        )
        for endpoint, payload, llm_result, response_key in cases:
            with self.subTest(endpoint=endpoint), patch(
                "modules.docgen.routes.call_llm",
                return_value=llm_result,
            ):
                response = self.client.post(
                    f"/application/gaoxin_relation_table/{self.company_id}/{endpoint}",
                    json=payload,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["ok"])
                self.assertTrue(response.get_json()[response_key])

    def test_sales_contract_keyword_route_calls_ai_once(self):
        with self.app.app_context():
            company = db.session.get(Company, self.company_id)
            company.data_json = json.dumps(
                {
                    "gaoxin_attachments": {
                        "relation_sales_contract": {
                            "files": [
                                {
                                    "id": "contract-1",
                                    "contract_code": "SC2024-01",
                                    "year": "2024",
                                    "original_filename": "智能校核服务合同.pdf",
                                    "relative_path": "contracts/test.pdf",
                                }
                            ]
                        }
                    }
                },
                ensure_ascii=False,
            )
            db.session.commit()

        with (
            patch("modules.docgen.routes._safe_attachment_path", return_value="/tmp/test.pdf"),
            patch("modules.docgen.routes.os.path.exists", return_value=True),
            patch(
                "modules.docgen.routes._extract_pdf_text",
                return_value="服务名称：继电保护智能校核服务\n用于电力系统安全分析。",
            ),
            patch(
                "modules.docgen.routes.call_llm",
                return_value={
                    "success": True,
                    "content": '{"summary":"提供继电保护智能校核服务","keywords":"继电保护；智能校核；安全分析"}',
                },
            ) as mocked,
        ):
            response = self.client.post(
                f"/application/gaoxin_relation_table/{self.company_id}/sales_contract_keywords",
                json={"file_id": "contract-1", "row": {}, "force": True},
            )

        self.assertEqual(response.status_code, 200)
        self.assertIn("继电保护", response.get_json()["keywords"])
        self.assertEqual(mocked.call_args.kwargs["max_attempts"], 1)

    def test_system_document_ai_routes_use_single_attempt(self):
        for endpoint, payload in (
            (
                "ai_generate",
                {
                    "doc_key": "rd_project",
                    "base": {"company_name": "测试科技有限公司"},
                    "target_words": 800,
                },
            ),
            (
                "ai_generate_evidence",
                {
                    "doc_key": "rd_project",
                    "base": {"company_name": "测试科技有限公司"},
                    "doc_content": "科研项目立项管理制度正文",
                },
            ),
        ):
            with self.subTest(endpoint=endpoint), patch(
                "modules.docgen.routes.call_llm",
                return_value={"success": True, "content": "一、总则\n测试正文"},
            ) as mocked:
                response = self.client.post(
                    f"/application/gaoxin_system_docs/{self.company_id}/{endpoint}",
                    json=payload,
                )
                self.assertEqual(response.status_code, 200)
                self.assertTrue(response.get_json()["success"])
                self.assertEqual(mocked.call_args.kwargs["max_attempts"], 1)

    def test_scoring_submission_never_requests_llm_analysis(self):
        def local_analysis(result, data=None, use_llm=True):
            self.assertFalse(use_llm)
            return analyze(result, data, use_llm=False)

        with patch("modules.scoring.routes.analyze", side_effect=local_analysis) as mocked:
            response = self.client.post(
                "/score/zhuanjing",
                data={"company_name": "快速评分测试企业"},
            )

        self.assertEqual(response.status_code, 302)
        self.assertTrue(mocked.called)
        with self.app.app_context():
            company = Company.query.filter_by(name="快速评分测试企业").one()
            self.assertIsNotNone(ScoreRecord.query.filter_by(company_id=company.id).first())


class LlmClientTests(unittest.TestCase):
    def test_default_retry_limit_is_two_attempts(self):
        session = MagicMock()
        session.post.side_effect = requests.exceptions.Timeout("timed out")
        with (
            patch(
                "modules.ai.llm_client._CONFIG",
                {"base_url": "https://example.test/v1", "api_key": "key", "model": "test"},
            ),
            patch("requests.Session", return_value=session),
            patch("modules.ai.llm_client.time.sleep"),
        ):
            result = call_llm([{"role": "user", "content": "test"}], timeout=1)

        self.assertFalse(result["success"])
        self.assertEqual(session.post.call_count, 2)

    def test_non_retryable_4xx_is_not_retried(self):
        response = MagicMock(status_code=400, text='{"error":"bad request"}')
        session = MagicMock()
        session.post.return_value = response
        with (
            patch(
                "modules.ai.llm_client._CONFIG",
                {"base_url": "https://example.test/v1", "api_key": "key", "model": "test"},
            ),
            patch("requests.Session", return_value=session),
            patch("modules.ai.llm_client.time.sleep"),
        ):
            result = call_llm(
                [{"role": "user", "content": "test"}],
                max_attempts=4,
            )

        self.assertFalse(result["success"])
        self.assertIn("HTTP 400", result["error"])
        self.assertEqual(session.post.call_count, 1)

    def test_upstream_failed_400_is_retried_once(self):
        failed = MagicMock(
            status_code=400,
            text='{"error":{"message":"Upstream request failed"}}',
        )
        succeeded = MagicMock(
            status_code=200,
            text='{"choices":[{"message":{"content":"ok"}}]}',
        )
        session = MagicMock()
        session.post.side_effect = [failed, succeeded]
        with (
            patch(
                "modules.ai.llm_client._CONFIG",
                {"base_url": "https://example.test/v1", "api_key": "key", "model": "test"},
            ),
            patch("requests.Session", return_value=session),
            patch("modules.ai.llm_client.time.sleep"),
        ):
            result = call_llm(
                [{"role": "user", "content": "test"}],
                max_attempts=2,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "ok")
        self.assertEqual(session.post.call_count, 2)
        self.assertEqual(session.post.call_args_list[0].kwargs["json"]["model"], "test")

    def test_upstream_failed_400_switches_to_fallback_model(self):
        failed = MagicMock(
            status_code=400,
            text='{"error":{"message":"Upstream request failed"}}',
        )
        succeeded = MagicMock(
            status_code=200,
            text='{"choices":[{"message":{"content":"fallback ok"}}]}',
        )
        session = MagicMock()
        session.post.side_effect = [failed, succeeded]
        with (
            patch(
                "modules.ai.llm_client._CONFIG",
                {
                    "base_url": "https://api.psydo.top/v1",
                    "api_key": "key",
                    "model": "gpt-5.5",
                },
            ),
            patch.dict(
                "modules.ai.llm_client.os.environ",
                {"LLM_FALLBACK_MODELS": "gpt-5.4-mini,gpt-5.4"},
            ),
            patch("requests.Session", return_value=session),
            patch("modules.ai.llm_client.time.sleep"),
        ):
            result = call_llm(
                [{"role": "user", "content": "test"}],
                max_attempts=2,
            )

        self.assertTrue(result["success"])
        self.assertEqual(result["content"], "fallback ok")
        requested_models = [
            call.kwargs["json"]["model"]
            for call in session.post.call_args_list
        ]
        self.assertEqual(requested_models, ["gpt-5.5", "gpt-5.4-mini"])

    def test_rule_analysis_can_explicitly_skip_llm(self):
        score = {
            "total_score": 75,
            "full_score": 100,
            "pass_score": 71,
            "passed": True,
            "rule_type": "高新技术",
            "breakdown": [],
        }
        with patch(
            "modules.ai.llm_client.analyze_scoring_result",
            side_effect=AssertionError("LLM must not be called"),
        ):
            result = analyze(score, use_llm=False)

        self.assertEqual(result["risk_level"], "低")
        self.assertIn("75", result["overall"])


class FinanceAiTests(unittest.TestCase):
    def test_finance_validation_reports_configured_llm_fields(self):
        with (
            patch.object(
                finance_extractor,
                "_extract_rule_data",
                return_value={"company_name": "测试科技有限公司"},
            ),
            patch.object(
                finance_extractor,
                "_claude_extract_financials_from_raw",
                return_value=({}, "Claude unavailable"),
            ),
            patch.object(
                finance_extractor,
                "_secondary_llm_extract_financials_from_raw",
                return_value=({"company_name": "测试科技有限公司"}, ""),
            ),
            patch.object(
                finance_extractor,
                "_detect_tax_period_year_from_file",
                return_value="2025",
            ),
        ):
            result = finance_extractor.extract_with_validation("/tmp/test.pdf")

        self.assertEqual(result["sources"]["llm_fields"], 1)
        self.assertEqual(result["sources"]["gpt55_fields"], 1)


if __name__ == "__main__":
    unittest.main()
