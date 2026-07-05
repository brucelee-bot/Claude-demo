"""规则管理路由"""
import json
from flask import Blueprint, render_template, request, redirect, url_for, flash
from flask_login import login_required, current_user
from models import db, ScoringRule

rules_bp = Blueprint("rules", __name__)


@rules_bp.route("/")
@login_required
def index():
    rules = ScoringRule.query.order_by(ScoringRule.rule_type, ScoringRule.created_at.desc()).all()
    return render_template("rules_index.html", rules=rules)


@rules_bp.route("/new", methods=["GET", "POST"])
@login_required
def new():
    if request.method == "POST":
        name = request.form.get("rule_name", "").strip()
        rtype = request.form.get("rule_type", "")
        config_text = request.form.get("config_json", "")

        if not name or not rtype:
            flash("规则名称和类型不能为空", "error")
            return render_template("rules_edit.html", rule=None)

        rule = ScoringRule(
            rule_type=rtype,
            rule_name=name,
            config_json=config_text,
            version=1,
            is_active=False,
        )
        db.session.add(rule)
        db.session.commit()
        flash(f"规则「{name}」已创建", "success")
        return redirect(url_for("rules.index"))

    return render_template("rules_edit.html", rule=None)


@rules_bp.route("/<int:rule_id>/edit", methods=["GET", "POST"])
@login_required
def edit(rule_id):
    rule = ScoringRule.query.get_or_404(rule_id)

    if request.method == "POST":
        rule.rule_name = request.form.get("rule_name", rule.rule_name)
        rule.rule_type = request.form.get("rule_type", rule.rule_type)
        rule.config_json = request.form.get("config_json", rule.config_json)
        db.session.commit()
        flash(f"规则「{rule.rule_name}」已更新", "success")
        return redirect(url_for("rules.index"))

    return render_template("rules_edit.html", rule=rule)


@rules_bp.route("/<int:rule_id>/toggle", methods=["POST"])
@login_required
def toggle(rule_id):
    """激活/停用规则"""
    rule = ScoringRule.query.get_or_404(rule_id)
    rule.is_active = not rule.is_active
    db.session.commit()
    status = "激活" if rule.is_active else "停用"
    flash(f"规则「{rule.rule_name}」已{status}", "success")
    return redirect(url_for("rules.index"))


@rules_bp.route("/<int:rule_id>/delete", methods=["POST"])
@login_required
def delete(rule_id):
    rule = ScoringRule.query.get_or_404(rule_id)
    db.session.delete(rule)
    db.session.commit()
    flash(f"规则「{rule.rule_name}」已删除", "success")
    return redirect(url_for("rules.index"))


@rules_bp.route("/<int:rule_id>")
@login_required
def view(rule_id):
    rule = ScoringRule.query.get_or_404(rule_id)
    try:
        config = json.loads(rule.config_json)
        indicators = config.get("indicators", [])
    except (json.JSONDecodeError, TypeError):
        indicators = []
    return render_template("rules_view.html", rule=rule, indicators=indicators)
