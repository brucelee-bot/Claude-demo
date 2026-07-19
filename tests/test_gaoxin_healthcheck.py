import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask
from flask_login import LoginManager

from models import Company, User, db
from modules.docgen.routes import docgen_bp
from modules.healthcheck.engine import run_health_check
from modules.scoring.routes import _build_score_data


class GaoxinHealthcheckTests(unittest.TestCase):
    def test_empty_data_is_blocked_and_marks_hard_facts_pending(self):
        result = run_health_check({}, {}, None, application_year=2026)

        self.assertEqual(result["status"], "blocked")
        self.assertGreaterEqual(len(result["export_blockers"]), 1)
        self.assertEqual(
            result["qualification"]["items"][0]["status"],
            "pending",
        )
        self.assertIn(
            "rd_expense_ratio",
            {item["id"].replace("pending-", "") for item in result["export_blockers"]},
        )

    def test_rd_ratio_uses_wan_yuan_revenue_bands(self):
        data = {
            "application_year": 2026,
            "registration_date": "2020-01-01",
            "tech_field": "电子信息",
            "ip_class1_count": 1,
            "staff_total": 100,
            "tech_staff": 20,
            "fin_2023_sales": 6000,
            "fin_2024_sales": 6000,
            "fin_2025_sales": 6000,
            "fin_2023_rd_expense": 240,
            "fin_2024_rd_expense": 240,
            "fin_2025_rd_expense": 240,
            "rd_total_3y": 720,
            "rd_domestic": 500,
            "revenue_1y": 6000,
            "hitech_revenue": 4000,
            "no_violation": "否",
        }

        result = run_health_check(data, {}, None, application_year=2026)
        item = next(
            item
            for item in result["qualification"]["items"]
            if item["id"] == "rd_expense_ratio"
        )

        self.assertEqual(item["status"], "pass")
        self.assertEqual(item["threshold"], "≥4%")

    def test_inconsistent_staff_and_revenue_data_is_reported(self):
        data = {
            "application_year": 2026,
            "staff_total": 10,
            "tech_staff": 11,
            "revenue_1y": 100,
            "hitech_revenue": 120,
            "rd_total_3y": 100,
            "rd_domestic": 120,
        }

        result = run_health_check(data, {}, None, application_year=2026)
        conflict_ids = {item["id"] for item in result["consistency"]["conflicts"]}

        self.assertEqual(
            conflict_ids,
            {"staff_ratio", "hitech_revenue", "domestic_rd"},
        )
        self.assertEqual(result["status"], "blocked")

    def test_patent_count_does_not_become_transformation_count(self):
        score_data = _build_score_data(
            {
                "company_name": "测试企业",
                "patent_count": "12",
                "transform_count": "",
            }
        )

        self.assertNotIn("transform_count", score_data)
        self.assertEqual(score_data["company_name"], "测试企业")

    def test_relation_identity_fields_are_not_counted_as_separate_ip_items(self):
        data = {
            "ip_class1_count": 2,
            "gaoxin_relation_table": {
                "rows": [
                    {
                        "rd_code": "RD01",
                        "rd_activity": "研发项目一",
                        "ip_code": "IP01",
                        "ip_auth_no": "ZL 2023 1 1121243.6",
                        "ip_name": "知识产权一",
                        "ps_code": "PS01",
                        "ps_name": "高新产品一",
                        "result_no": "成果01",
                        "result_name": "成果名称一",
                    },
                    {
                        "rd_code": "RD02",
                        "rd_activity": "研发项目二",
                        "ip_code": "IP02",
                        "ip_auth_no": "ZL 2023 1 1141373.6",
                        "ip_name": "知识产权二",
                        "ps_code": "PS01",
                        "ps_name": "高新产品一",
                        "result_no": "成果02",
                        "result_name": "成果名称一",
                    },
                ]
            },
        }
        attachments = {
            "ip": {
                "files": [
                    {"ip_seq": "IP01", "original_filename": "IP01.pdf"},
                    {"ip_seq": "IP02", "original_filename": "IP02.pdf"},
                ]
            }
        }

        result = run_health_check(data, attachments, None, application_year=2026)
        warning_ids = {
            item["id"] for item in result["consistency"]["warnings"]
        }
        relation_item = next(
            item
            for item in result["evidence"]["items"]
            if item["id"] == "rd_relation"
        )
        ip_item = next(
            item
            for item in result["evidence"]["items"]
            if item["id"] == "ip_mapping"
        )

        self.assertNotIn("ip-file-row-count", warning_ids)
        self.assertEqual(
            ip_item["detail"],
            "已上传 2 份证明，关系表识别 2 项。",
        )
        self.assertEqual(
            relation_item["detail"],
            "研发项目 2 个、产品 1 个、成果 2 个。",
        )

    def test_actual_ip_file_relation_count_mismatch_is_still_reported(self):
        data = {
            "gaoxin_relation_table": {
                "rows": [
                    {
                        "ip_code": "IP01",
                        "ip_auth_no": "ZL01",
                        "ip_name": "知识产权一",
                    },
                    {
                        "ip_code": "IP02",
                        "ip_auth_no": "ZL02",
                        "ip_name": "知识产权二",
                    },
                ]
            }
        }
        attachments = {
            "ip": {
                "files": [
                    {"ip_seq": "IP01", "original_filename": "IP01.pdf"},
                ]
            }
        }

        result = run_health_check(data, attachments, None, application_year=2026)
        warning = next(
            item
            for item in result["consistency"]["warnings"]
            if item["id"] == "ip-file-row-count"
        )

        self.assertEqual(
            warning["detail"],
            "证明 1 份，关系表 2 项，需人工确认是否一证多项或存在遗漏。",
        )


class GaoxinHealthcheckRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        )
        app.config.update(
            TESTING=True,
            SECRET_KEY="health-test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            UPLOAD_FOLDER=cls.temp_dir.name,
            OUTPUT_FOLDER=cls.temp_dir.name,
        )
        db.init_app(app)
        login_manager = LoginManager(app)

        @login_manager.user_loader
        def load_user(user_id):
            return db.session.get(User, int(user_id))

        @app.route("/")
        def dashboard():
            return "dashboard"

        auth_bp = Blueprint("auth", __name__)
        auth_bp.add_url_rule("/logout", "logout", lambda: "logout")
        scoring_bp = Blueprint("scoring", __name__)
        scoring_bp.add_url_rule("/", "index", lambda: "scoring")
        scoring_bp.add_url_rule("/gaoxin", "gaoxin", lambda: "gaoxin")
        rules_bp = Blueprint("rules", __name__)
        rules_bp.add_url_rule("/", "index", lambda: "rules")
        app.register_blueprint(auth_bp, url_prefix="/auth")
        app.register_blueprint(scoring_bp, url_prefix="/score")
        app.register_blueprint(rules_bp, url_prefix="/rules")
        app.register_blueprint(docgen_bp, url_prefix="/application")
        cls.app = app

        with app.app_context():
            db.create_all()
            user = User(username="health-route-test", password_hash="test")
            db.session.add(user)
            db.session.flush()
            company = Company(
                user_id=user.id,
                name="体检测试企业",
                app_type="高新技术",
                data_json=json.dumps({}),
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
        cls.temp_dir.cleanup()

    @patch(
        "modules.docgen.routes._prepare_export_attachment_files",
        side_effect=RuntimeError("export pipeline reached"),
    )
    def test_pdf_export_reaches_pipeline_even_with_health_blockers(self, prepare_files):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

        response = client.get(
            f"/application/gaoxin_attachments/{self.company_id}/pdf",
            headers={"Accept": "application/pdf"},
        )

        self.assertEqual(response.status_code, 500)
        payload = response.get_json()
        self.assertIn("export pipeline reached", payload["error"])
        prepare_files.assert_called_once()

    def test_legacy_health_page_redirects_to_assessment(self):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True

        response = client.get(f"/application/health/{self.company_id}")

        self.assertEqual(response.status_code, 302)
        self.assertIn(
            f"/application/assessment?company_id={self.company_id}",
            response.headers["Location"],
        )
