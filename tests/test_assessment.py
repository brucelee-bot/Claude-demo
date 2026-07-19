import json
import unittest
from pathlib import Path

from flask import Blueprint, Flask
from flask_login import LoginManager

from models import ApplicationDraft, Company, ScoreRecord, User, db
from modules.docgen.routes import docgen_bp
from modules.scoring.routes import scoring_bp


class AssessmentRouteTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        )
        app.config.update(
            TESTING=True,
            SECRET_KEY="assessment-test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
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
        app.register_blueprint(auth_bp, url_prefix="/auth")
        app.register_blueprint(docgen_bp, url_prefix="/application")
        app.register_blueprint(scoring_bp, url_prefix="/score")
        cls.app = app

        with app.app_context():
            db.create_all()
            user = User(username="assessment-test", password_hash="test")
            db.session.add(user)
            db.session.flush()
            company = Company(
                user_id=user.id,
                name="评估测试企业",
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

    def setUp(self):
        self.client = self.app.test_client()
        with self.client.session_transaction() as session:
            session["_user_id"] = str(self.user_id)
            session["_fresh"] = True
        with self.app.app_context():
            ApplicationDraft.query.delete()
            ScoreRecord.query.delete()
            company = db.session.get(Company, self.company_id)
            company.data_json = json.dumps({})
            db.session.commit()

    def _add_score(self):
        with self.app.app_context():
            db.session.add(
                ScoreRecord(
                    company_id=self.company_id,
                    score_type="高新技术",
                    total_score=76,
                    breakdown_json=json.dumps(
                        [{"name": "知识产权", "score": 20}],
                        ensure_ascii=False,
                    ),
                    ai_analysis=json.dumps(
                        {
                            "overall": "评分已完成，仍需补强证明材料。",
                            "priority": "优先补齐证明材料。",
                            "risk_level": "中",
                            "weaknesses": ["证据材料不足"],
                            "recommendations": ["补充证明材料"],
                        },
                        ensure_ascii=False,
                    ),
                )
            )
            db.session.commit()

    def _add_application_input(self):
        with self.app.app_context():
            company = db.session.get(Company, self.company_id)
            company.data_json = json.dumps(
                {
                    "_application_input_saved": True,
                    "company_name": "评估测试企业",
                    "gaoxin_relation_table": {
                        "rows": [
                            {
                                "rd_code": "RD01",
                                "rd_activity": "研发项目一",
                            }
                        ]
                    },
                },
                ensure_ascii=False,
            )
            db.session.commit()

    def test_without_score_only_shows_missing_score_and_no_judgement(self):
        response = self.client.get(
            f"/application/assessment?company_id={self.company_id}"
        )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("评估尚未开启", body)
        self.assertIn("完成评分", body)
        self.assertNotIn("评分分析与优先补强", body)
        self.assertNotIn("资格条件", body)

    def test_score_without_application_input_stays_blocked(self):
        self._add_score()

        response = self.client.get(
            f"/application/assessment?company_id={self.company_id}"
        )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("填写申报书", body)
        self.assertNotIn("评分分析与优先补强", body)

    def test_score_and_application_input_show_unified_assessment(self):
        self._add_score()
        self._add_application_input()

        response = self.client.get(
            f"/application/assessment?company_id={self.company_id}"
        )

        body = response.get_data(as_text=True)
        self.assertEqual(response.status_code, 200)
        self.assertIn("评分分析与优先补强", body)
        self.assertIn("资格条件", body)
        self.assertIn("证据完整度", body)
        self.assertNotIn("评估尚未开启", body)

    def test_old_assistant_and_health_urls_redirect_to_one_assessment(self):
        for path in (
            "/application/assistant",
            "/application/assistant/brief",
            f"/application/health/{self.company_id}",
        ):
            response = self.client.get(path)
            self.assertEqual(response.status_code, 302)
            self.assertIn("/application/assessment", response.headers["Location"])

    def test_legacy_health_json_does_not_evaluate_before_inputs_are_ready(self):
        response = self.client.get(
            f"/application/health/{self.company_id}/json"
        )

        payload = response.get_json()
        self.assertEqual(response.status_code, 200)
        self.assertFalse(payload["ready"])
        self.assertIsNone(payload["health"])
        self.assertEqual(
            {item["key"] for item in payload["missing"]},
            {"score", "application"},
        )

    def test_navigation_has_one_assessment_entry(self):
        self._add_score()
        self._add_application_input()

        response = self.client.get(
            f"/application/assessment?company_id={self.company_id}"
        )

        body = response.get_data(as_text=True)
        self.assertEqual(body.count(">申报评估</a>"), 1)
        self.assertNotIn(">助手</a>", body)
        self.assertNotIn(">申报体检</a>", body)


if __name__ == "__main__":
    unittest.main()
