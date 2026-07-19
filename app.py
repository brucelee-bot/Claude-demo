import json
import os
from flask import Flask, render_template
from dotenv import load_dotenv
load_dotenv()
from config import Config
from models import db, User
from flask_login import LoginManager, current_user


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    os.makedirs(app.config["UPLOAD_FOLDER"], exist_ok=True)
    os.makedirs(app.config["OUTPUT_FOLDER"], exist_ok=True)

    print(f"LLM_API_BASE={app.config.get('LLM_API_BASE', '')}")
    print(f"LLM_MODEL={app.config.get('LLM_MODEL', '')}")

    db.init_app(app)

    login_manager = LoginManager()
    login_manager.login_view = "auth.login"
    login_manager.login_message = "请先登录"
    login_manager.init_app(app)

    @login_manager.user_loader
    def load_user(user_id):
        return User.query.get(int(user_id))

    # 注册蓝图
    from modules.auth.routes import auth_bp
    from modules.scoring.routes import scoring_bp
    from modules.docgen.routes import docgen_bp
    from modules.parser.routes import parser_bp
    from modules.scoring.rules_routes import rules_bp

    app.register_blueprint(auth_bp, url_prefix="/auth")
    app.register_blueprint(scoring_bp, url_prefix="/score")
    app.register_blueprint(docgen_bp, url_prefix="/application")
    app.register_blueprint(parser_bp, url_prefix="/parser")
    app.register_blueprint(rules_bp, url_prefix="/rules")

    # 首页 = 仪表盘
    @app.route("/")
    def dashboard():
        from models import ScoreRecord, ApplicationDraft, Company, ScoringRule
        from flask import request
        from modules.docgen.routes import _assessment_input_state
        from modules.healthcheck.engine import run_health_check, score_result_from_record

        active_tab = request.args.get("tab", "gaoxin")

        if not current_user.is_authenticated:
            return render_template("dashboard.html", active_tab=active_tab)

        def pass_score_for(score_type):
            if score_type == "高新技术":
                return 71
            if score_type == "小巨人":
                return 60
            return 50

        def latest_score_for(company):
            return (
                ScoreRecord.query
                .filter_by(company_id=company.id)
                .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
                .first()
            )

        def latest_draft_for(company):
            return (
                ApplicationDraft.query
                .filter_by(company_id=company.id)
                .order_by(ApplicationDraft.created_at.desc(), ApplicationDraft.id.desc())
                .first()
            )

        company_id = request.args.get("company_id", type=int)
        current_company = None
        if company_id:
            current_company = Company.query.filter_by(id=company_id, user_id=current_user.id).first()

        latest_user_score = (
            ScoreRecord.query
            .join(Company)
            .filter(Company.user_id == current_user.id)
            .order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc())
            .first()
        )
        if not current_company and latest_user_score:
            current_company = latest_user_score.company
        if not current_company:
            current_company = (
                Company.query
                .filter_by(user_id=current_user.id)
                .order_by(Company.created_at.desc(), Company.id.desc())
                .first()
            )

        current_score = latest_score_for(current_company) if current_company else None
        current_draft = latest_draft_for(current_company) if current_company else None
        current_app_type = current_score.score_type if current_score else (current_company.app_type if current_company else "高新技术")
        pass_score = pass_score_for(current_app_type)
        current_total_score = round(float(current_score.total_score or 0), 1) if current_score else 0
        is_passed = bool(current_score and current_total_score >= pass_score)
        current_company_data = {}
        assessment_state = {"ready": False, "application_ready": False}
        if current_company:
            try:
                current_company_data = json.loads(current_company.data_json or "{}")
            except (json.JSONDecodeError, TypeError):
                current_company_data = {}
            assessment_state = _assessment_input_state(current_company, current_company_data)
        health = None
        if current_company and current_app_type == "高新技术":
            health = run_health_check(
                current_company_data,
                current_company_data.get("gaoxin_attachments", {}),
                score_result_from_record(current_score),
            )

        progress_steps = [
            {"title": "企业资料", "done": bool(current_company), "current": bool(current_company and not current_score)},
            {"title": "企业评分", "done": bool(current_score), "current": bool(current_score and not assessment_state["application_ready"])},
            {"title": "申报书", "done": bool(assessment_state["application_ready"]), "current": bool(current_score and assessment_state["application_ready"] and not assessment_state["ready"])},
            {"title": "申报评估", "done": bool(assessment_state["ready"]), "current": bool(assessment_state["ready"] and health and health["status"] != "ready")},
            {"title": "PDF导出", "done": False, "current": bool(current_draft)},
        ]

        if not current_company:
            next_action = {"label": "立即评分", "endpoint": "scoring.gaoxin", "hint": "先录入企业资料并完成首次评分。"}
        elif not current_score:
            endpoint = "scoring.gaoxin" if current_app_type == "高新技术" else ("scoring.xiaojuren" if current_app_type == "小巨人" else "scoring.zhuanjing")
            next_action = {"label": "立即评分", "endpoint": endpoint, "hint": "完成评分后才能查看达标情况和诊断建议。"}
        elif not assessment_state["application_ready"]:
            endpoint = "docgen.gaoxin_relation_table" if current_app_type == "高新技术" else "docgen.fill"
            next_action = {"label": "生成申报书", "endpoint": endpoint, "hint": "使用当前企业数据生成申报材料。"}
        elif not assessment_state["ready"]:
            next_action = {"label": "进入申报评估", "endpoint": "docgen.assessment", "hint": "评分和申报书资料已具备，查看统一评估结果。"}
        else:
            next_action = {"label": "进入申报评估", "endpoint": "docgen.assessment", "hint": "查看评分分析、资格条件、证据完整度和优先待办。"}

        recent_companies = []
        companies = (
            Company.query
            .filter_by(user_id=current_user.id)
            .order_by(Company.created_at.desc(), Company.id.desc())
            .limit(6)
            .all()
        )
        for company in companies:
            score = latest_score_for(company)
            draft = latest_draft_for(company)
            company_pass_score = pass_score_for(score.score_type if score else company.app_type)
            company_health = None
            if company.app_type == "高新技术" or (score and score.score_type == "高新技术"):
                try:
                    company_data = json.loads(company.data_json or "{}")
                except (json.JSONDecodeError, TypeError):
                    company_data = {}
                company_health = run_health_check(
                    company_data,
                    company_data.get("gaoxin_attachments", {}),
                    score_result_from_record(score),
                )
            recent_companies.append({
                "company": company,
                "score": score,
                "draft": draft,
                "pass_score": company_pass_score,
                "passed": bool(score and (score.total_score or 0) >= company_pass_score),
                "health": company_health,
            })

        stats = {
            "total_scores": ScoreRecord.query.join(Company).filter(Company.user_id == current_user.id).count(),
            "total_drafts": ApplicationDraft.query.join(Company).filter(Company.user_id == current_user.id).count(),
            "total_companies": Company.query.filter_by(user_id=current_user.id).count(),
            "active_rules": ScoringRule.query.filter_by(is_active=True).count() or 3,
        }

        return render_template(
            "dashboard.html",
            stats=stats,
            active_tab=active_tab,
            current_company=current_company,
            current_score=current_score,
            current_draft=current_draft,
            current_app_type=current_app_type,
            current_total_score=current_total_score,
            pass_score=pass_score,
            is_passed=is_passed,
            health=health,
            progress_steps=progress_steps,
            next_action=next_action,
            recent_companies=recent_companies,
        )

    # 错误处理
    @app.errorhandler(404)
    def not_found(e):
        return render_template("base.html", content="<h2>404 — 页面未找到</h2>"), 404

    with app.app_context():
        db.create_all()

    if not os.environ.get("VERCEL"):
        try:
            from modules.ai.llm_client import warmup_llm_async
            if os.environ.get("WERKZEUG_RUN_MAIN") in (None, "true"):
                warmup_llm_async()
        except Exception:
            pass

    return app


app = create_app()


if __name__ == "__main__":
    app.run(debug=True, use_reloader=True, port=8081)
