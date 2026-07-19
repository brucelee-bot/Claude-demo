import json
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from flask import Blueprint, Flask
from flask_login import LoginManager

from models import Company, ExportJob, User, db
from modules.docgen.routes import (
    GAOXIN_ATTACHMENT_EXPORT_JOB_TYPE,
    _gaoxin_attachment_export_fingerprint,
    docgen_bp,
)


class GaoxinPdfJobTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp_dir = tempfile.TemporaryDirectory()
        app = Flask(
            __name__,
            template_folder=str(Path(__file__).resolve().parents[1] / "templates"),
        )
        app.config.update(
            TESTING=True,
            SECRET_KEY="pdf-job-test-secret",
            SQLALCHEMY_DATABASE_URI="sqlite:///:memory:",
            SQLALCHEMY_TRACK_MODIFICATIONS=False,
            UPLOAD_FOLDER=str(Path(cls.temp_dir.name) / "uploads"),
            OUTPUT_FOLDER=str(Path(cls.temp_dir.name) / "outputs"),
        )
        Path(app.config["UPLOAD_FOLDER"]).mkdir(parents=True)
        Path(app.config["OUTPUT_FOLDER"]).mkdir(parents=True)
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
            owner = User(username="pdf-job-owner", password_hash="test")
            other = User(username="pdf-job-other", password_hash="test")
            db.session.add_all([owner, other])
            db.session.flush()
            company = Company(
                user_id=owner.id,
                name="PDF任务测试企业",
                app_type="高新技术",
                data_json=json.dumps({"application_year": 2026}),
            )
            db.session.add(company)
            db.session.commit()
            cls.owner_id = owner.id
            cls.other_id = other.id
            cls.company_id = company.id

    @classmethod
    def tearDownClass(cls):
        with cls.app.app_context():
            db.session.remove()
            db.drop_all()
        cls.temp_dir.cleanup()

    def setUp(self):
        with self.app.app_context():
            ExportJob.query.delete()
            company = db.session.get(Company, self.company_id)
            company.data_json = json.dumps({"application_year": 2026})
            db.session.commit()

    def _client(self, user_id=None):
        client = self.app.test_client()
        with client.session_transaction() as session:
            session["_user_id"] = str(user_id or self.owner_id)
            session["_fresh"] = True
        return client

    def _create_job(self, **values):
        with self.app.app_context():
            company = db.session.get(Company, self.company_id)
            data = json.loads(company.data_json)
            defaults = {
                "id": values.pop("id", "11111111-1111-1111-1111-111111111111"),
                "company_id": self.company_id,
                "user_id": self.owner_id,
                "job_type": GAOXIN_ATTACHMENT_EXPORT_JOB_TYPE,
                "fingerprint": _gaoxin_attachment_export_fingerprint(company, data),
                "status": "queued",
                "stage": "等待生成",
                "progress": 2,
                "download_name": "测试附件.pdf",
            }
            defaults.update(values)
            job = ExportJob(**defaults)
            db.session.add(job)
            db.session.commit()
            return job.id

    def test_create_is_blocked_when_health_check_has_blockers(self):
        response = self._client().post(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs"
        )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.get_json()["code"], "HEALTH_CHECK_BLOCKED")
        with self.app.app_context():
            self.assertEqual(ExportJob.query.count(), 0)

    @patch(
        "modules.docgen.routes._company_health_check",
        return_value={"export_blockers": []},
    )
    def test_create_and_reuse_active_job(self, _health_check):
        client = self._client()
        url = f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs"

        first = client.post(url)
        second = client.post(url)

        self.assertEqual(first.status_code, 202)
        self.assertEqual(second.status_code, 202)
        self.assertEqual(
            first.get_json()["job"]["id"],
            second.get_json()["job"]["id"],
        )
        with self.app.app_context():
            self.assertEqual(ExportJob.query.count(), 1)

    @patch(
        "modules.docgen.routes._company_health_check",
        return_value={"export_blockers": []},
    )
    def test_ready_job_is_cache_hit_and_downloads_local_file(self, _health_check):
        relative_path = "generated_exports/test/result.pdf"
        output_path = Path(self.app.config["OUTPUT_FOLDER"]) / relative_path
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"%PDF-1.4\ncached-test\n%%EOF")
        job_id = self._create_job(
            status="ready",
            stage="PDF 已生成，可下载",
            progress=100,
            result_path=relative_path,
            result_size=26,
            completed_at=datetime.utcnow(),
        )
        client = self._client()

        cached = client.post(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs"
        )
        downloaded = client.get(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs/{job_id}/download"
        )

        self.assertEqual(cached.status_code, 200)
        self.assertTrue(cached.get_json()["cache_hit"])
        self.assertEqual(cached.get_json()["job"]["id"], job_id)
        self.assertEqual(downloaded.status_code, 200)
        self.assertEqual(downloaded.mimetype, "application/pdf")
        self.assertTrue(downloaded.data.startswith(b"%PDF-1.4"))

    def test_job_status_is_private_to_owner(self):
        job_id = self._create_job()

        response = self._client(self.other_id).get(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs/{job_id}"
        )

        self.assertEqual(response.status_code, 404)

    def test_download_rejects_unfinished_job(self):
        job_id = self._create_job(status="running", progress=40)

        response = self._client().get(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs/{job_id}/download"
        )

        self.assertEqual(response.status_code, 409)
        self.assertIn("尚未生成完成", response.get_json()["error"])

    def test_stale_running_job_is_marked_failed(self):
        job_id = self._create_job(
            status="running",
            progress=40,
            started_at=datetime.utcnow() - timedelta(minutes=20),
            updated_at=datetime.utcnow() - timedelta(minutes=20),
        )

        response = self._client().get(
            f"/application/gaoxin_attachments/{self.company_id}/pdf/jobs/{job_id}"
        )

        self.assertEqual(response.status_code, 200)
        job = response.get_json()["job"]
        self.assertEqual(job["status"], "failed")
        self.assertIn("中断", job["stage"])

    def test_fingerprint_changes_with_company_material(self):
        with self.app.app_context():
            company = db.session.get(Company, self.company_id)
            before = _gaoxin_attachment_export_fingerprint(
                company,
                json.loads(company.data_json),
            )
            company.data_json = json.dumps(
                {"application_year": 2026, "company_address": "新地址"}
            )
            db.session.commit()
            after = _gaoxin_attachment_export_fingerprint(
                company,
                json.loads(company.data_json),
            )

        self.assertNotEqual(before, after)

    def test_template_uses_resumable_job_flow(self):
        template = (
            Path(__file__).resolve().parents[1]
            / "templates"
            / "application_gaoxin_attachments.html"
        ).read_text(encoding="utf-8")

        self.assertIn("exportPdfJobCreateUrl", template)
        self.assertIn("resumeExportJob", template)
        self.assertIn("launchAttempts", template)
        self.assertNotIn("const exportPdfUrl", template)


if __name__ == "__main__":
    unittest.main()
