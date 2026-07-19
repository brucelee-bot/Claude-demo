from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from datetime import datetime

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(200), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    companies = db.relationship("Company", backref="owner", lazy=True)

    def __repr__(self):
        return f"<User {self.username}>"


class Company(db.Model):
    __tablename__ = "companies"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False)
    name = db.Column(db.String(200), nullable=False)
    app_type = db.Column(db.String(50), default="专精特新")  # 专精特新 / 高新技术
    data_json = db.Column(db.Text)  # 完整表单 / 上传数据
    ip_certs_json = db.Column(db.Text, default="[]")  # IP证书持久化
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    scores = db.relationship("ScoreRecord", backref="company", lazy=True)
    drafts = db.relationship("ApplicationDraft", backref="company", lazy=True)

    def __repr__(self):
        return f"<Company {self.name}>"


class ScoreRecord(db.Model):
    __tablename__ = "score_records"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    score_type = db.Column(db.String(50))  # 专精特新 / 高新技术
    total_score = db.Column(db.Float)
    breakdown_json = db.Column(db.Text)  # 分项得分明细
    ai_analysis = db.Column(db.Text)  # AI 定性分析
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ApplicationDraft(db.Model):
    __tablename__ = "application_drafts"

    id = db.Column(db.Integer, primary_key=True)
    company_id = db.Column(db.Integer, db.ForeignKey("companies.id"), nullable=False)
    app_type = db.Column(db.String(50))  # 专精特新 / 高新技术
    sections_json = db.Column(db.Text)  # 各章节内容 {"section_id": "content"}
    docx_path = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ScoringRule(db.Model):
    __tablename__ = "scoring_rules"

    id = db.Column(db.Integer, primary_key=True)
    rule_type = db.Column(db.String(50))  # 专精特新 / 高新技术
    rule_name = db.Column(db.String(200))
    config_json = db.Column(db.Text)  # 规则配置 JSON
    version = db.Column(db.Integer, default=1)
    is_active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ExportJob(db.Model):
    __tablename__ = "export_jobs"

    id = db.Column(db.String(36), primary_key=True)
    company_id = db.Column(
        db.Integer,
        db.ForeignKey("companies.id"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id"),
        nullable=False,
        index=True,
    )
    job_type = db.Column(db.String(50), nullable=False, default="gaoxin_attachments_pdf")
    fingerprint = db.Column(db.String(64), nullable=False, index=True)
    status = db.Column(db.String(20), nullable=False, default="queued", index=True)
    stage = db.Column(db.String(200), nullable=False, default="等待生成")
    progress = db.Column(db.Integer, nullable=False, default=0)
    error_message = db.Column(db.Text)
    result_path = db.Column(db.String(500))
    download_name = db.Column(db.String(300))
    result_size = db.Column(db.Integer)
    duration_seconds = db.Column(db.Float)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    updated_at = db.Column(
        db.DateTime,
        default=datetime.utcnow,
        onupdate=datetime.utcnow,
        nullable=False,
    )
