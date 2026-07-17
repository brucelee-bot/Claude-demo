import json
import os
import shutil
import uuid
from datetime import datetime, timezone

from flask import render_template, request, redirect, url_for, flash, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from models import db, Company, ScoreRecord
from modules.scoring import scoring_bp
from modules.scoring.engine import calculate, calculate_growth_rates
from modules.ai.analyzer import analyze
from modules.docgen.sales_contracts import (
    ensure_sales_contract_codes,
    remap_sales_contract_rows,
    selectable_sales_contracts,
)
from modules.storage import persist_file


def _get_or_create_company(company_name: str, app_type: str, data: dict) -> Company:
    """同一用户、公司名称只保留一个 Company；重复评分时更新原记录。"""
    company = (
        Company.query
        .filter_by(user_id=current_user.id, name=company_name)
        .order_by(Company.id.desc())
        .first()
    )
    if company:
        try:
            merged_data = json.loads(company.data_json or "{}")
        except (json.JSONDecodeError, TypeError):
            merged_data = {}
        merged_data.update(data)
        company.app_type = app_type
        company.data_json = json.dumps(merged_data, ensure_ascii=False)
    else:
        company = Company(
            user_id=current_user.id,
            name=company_name,
            app_type=app_type,
            data_json=json.dumps(data, ensure_ascii=False),
        )
        db.session.add(company)
        db.session.flush()
    return company


def _upsert_score_record(company: Company, score_type: str, result: dict, ai_analysis: dict) -> ScoreRecord:
    """同一公司只保留一条评分记录；再次评分时更新原记录。"""
    record = ScoreRecord.query.filter_by(company_id=company.id).first()
    if not record:
        record = ScoreRecord(company_id=company.id)
        db.session.add(record)
    record.score_type = score_type
    record.total_score = result["total_score"]
    record.breakdown_json = json.dumps(result["breakdown"], ensure_ascii=False)
    record.ai_analysis = json.dumps(ai_analysis, ensure_ascii=False)
    return record


def _persist_session_ip_certs(company: Company) -> None:
    try:
        from modules.parser.routes import _get_ip_certs
        certs = _get_ip_certs()
    except Exception:
        certs = session.get("ip_certificates", [])
    if certs:
        from modules.docgen.routes import sync_ip_cert_pdfs_to_attachments
        certs = sync_ip_cert_pdfs_to_attachments(company, certs)
        company.ip_certs_json = json.dumps(certs, ensure_ascii=False)


def _load_company_data(company: Company) -> dict:
    try:
        data = json.loads(company.data_json or "{}")
    except (json.JSONDecodeError, TypeError):
        data = {}
    return data if isinstance(data, dict) else {}


def _seed_required_material_relation_rows(data: dict, company: Company, contracts: list) -> None:
    relation_table = data.get("gaoxin_relation_table")
    if not isinstance(relation_table, dict):
        relation_table = {}
    rows = relation_table.get("rows")
    rows = [dict(row) for row in rows if isinstance(row, dict)] if isinstance(rows, list) else []

    try:
        certs = json.loads(company.ip_certs_json or "[]")
    except (json.JSONDecodeError, TypeError):
        certs = []
    certs = certs if isinstance(certs, list) else []

    def empty_row():
        return {
            "year": "2025",
            "rd_code": "",
            "rd_activity": "",
            "rd_period": "",
            "ip_code": "",
            "ip_name": "",
            "ip_auth_no": "",
            "ps_code": "",
            "ps_name": "",
            "result_no": "",
            "result_name": "",
            "sales_contract_file_id": "",
            "sales_contract_code": "",
            "sales_contract_filename": "",
            "sales_contract_summary": "",
            "sales_contract_keywords": "",
        }

    for index, cert in enumerate(certs, start=1):
        details = ((cert or {}).get("parsed") or {}).get("details") or {}
        name = str(details.get("name") or "").strip()
        auth_no = str(details.get("patent_no") or details.get("grant_no") or "").strip()
        row = next(
            (
                item for item in rows
                if (auth_no and str(item.get("ip_auth_no") or "").strip() == auth_no)
                or (name and str(item.get("ip_name") or "").strip() == name)
            ),
            None,
        )
        if row is None:
            row = empty_row()
            rows.append(row)
        row["ip_code"] = row.get("ip_code") or f"IP{index:02d}"
        row["ip_name"] = row.get("ip_name") or name
        row["ip_auth_no"] = row.get("ip_auth_no") or auth_no

    ensure_sales_contract_codes(contracts, rows)
    remap_sales_contract_rows(rows, contracts)
    contracts_by_id = {
        str(contract.get("id") or "").strip(): contract
        for contract in contracts
        if str(contract.get("id") or "").strip()
    }
    for row in rows:
        file_id = str(row.get("sales_contract_file_id") or "").strip()
        contract = contracts_by_id.get(file_id)
        if not contract:
            continue
        row["sales_contract_code"] = contract.get("contract_code", "")
        row["sales_contract_filename"] = contract.get("original_filename", "")
        row["sales_contract_summary"] = contract.get("summary", "")
        row["sales_contract_keywords"] = contract.get("keywords", "")

    relation_table["rows"] = rows
    relation_table.setdefault("tech_field_path", "")
    data["gaoxin_relation_table"] = relation_table


def _persist_required_materials(company: Company) -> None:
    try:
        from modules.parser.routes import _clear_required_materials, _get_required_materials

        materials = _get_required_materials()
    except Exception:
        return

    data = _load_company_data(company)
    staff = materials.get("staff") if isinstance(materials.get("staff"), dict) else {}
    staff_rows = staff.get("rows") if isinstance(staff.get("rows"), list) else []
    staff_summary = staff.get("summary") if isinstance(staff.get("summary"), dict) else {}
    if staff_rows:
        data["hr_staff_rows"] = staff_rows
        data.update(staff_summary)
        data["staff_total"] = staff_summary.get("hr_total", len(staff_rows))
        if staff_summary.get("tech_staff") is not None:
            data["tech_staff"] = staff_summary["tech_staff"]

    from modules.docgen.routes import (
        _attachment_relative_path,
        _load_gaoxin_attachments_from_data,
        _safe_attachment_path,
        _source_upload_path,
    )

    attachments = _load_gaoxin_attachments_from_data(data)
    contract_files = attachments.setdefault("relation_sales_contract", {"files": []}).setdefault("files", [])
    relation_rows = ((data.get("gaoxin_relation_table") or {}).get("rows") or [])
    ensure_sales_contract_codes(contract_files, relation_rows)
    remap_sales_contract_rows(relation_rows, contract_files)
    ensure_sales_contract_codes(materials.get("sales_contracts") or [])
    persisted_contracts = []
    existing_by_hash = {
        str(item.get("sha256") or ""): item
        for item in contract_files
        if isinstance(item, dict) and item.get("sha256")
    }
    existing_by_name = {
        (
            str(item.get("year") or ""),
            str(item.get("original_filename") or ""),
        ): item
        for item in contract_files
        if isinstance(item, dict) and item.get("original_filename")
    }
    preserved_staged_paths = set()
    for staged in materials.get("sales_contracts") or []:
        if not isinstance(staged, dict):
            continue
        source_hash = str(staged.get("sha256") or "")
        original_filename = str(staged.get("original_filename") or "销售合同.pdf")
        contract_year = str(staged.get("year") or "").strip()
        existing = existing_by_hash.get(source_hash) or existing_by_name.get(
            (contract_year, original_filename)
        )
        if existing:
            if contract_year and not existing.get("year"):
                existing["year"] = contract_year
            if staged.get("contract_sequence"):
                existing["contract_sequence"] = staged.get("contract_sequence")
            if staged.get("contract_code"):
                existing["contract_code"] = staged.get("contract_code")
            persisted_contracts.append(existing)
            continue

        staged_relative_path = str(staged.get("relative_path") or "").replace("\\", "/").strip("/")
        has_blob_reference = any(
            str(staged.get(key) or "").strip()
            for key in ("blob_url", "blob_download_url", "blob_etag")
        )
        source_path = _source_upload_path(staged_relative_path)
        source_exists = bool(source_path and os.path.exists(source_path))
        if not source_exists and not (has_blob_reference and staged_relative_path):
            continue
        safe_name = secure_filename(original_filename) or f"sales_contract_{uuid.uuid4().hex}.pdf"
        if not safe_name.lower().endswith(".pdf"):
            safe_name = f"{os.path.splitext(safe_name)[0]}.pdf"
        file_id = str(staged.get("id") or uuid.uuid4().hex)
        stored_filename = f"{file_id}_{safe_name}"
        if source_exists:
            relative_path = _attachment_relative_path(
                company.user_id,
                company.id,
                "relation_sales_contract",
                stored_filename,
            )
            target_path = _safe_attachment_path(relative_path)
            os.makedirs(os.path.dirname(target_path), exist_ok=True)
            shutil.copy2(source_path, target_path)
            persist_file(target_path, relative_path)
        else:
            relative_path = staged_relative_path
            stored_filename = str(staged.get("stored_filename") or "").strip() or os.path.basename(
                staged_relative_path
            )
            preserved_staged_paths.add(staged_relative_path)
        file_meta = {
            "id": file_id,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "relative_path": relative_path,
            "uploaded_at": staged.get("uploaded_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "relation_sales_contract",
            "year": contract_year,
            "contract_sequence": staged.get("contract_sequence"),
            "contract_code": staged.get("contract_code", ""),
            "summary": staged.get("summary", ""),
            "keywords": staged.get("keywords", ""),
            "sha256": source_hash,
            "auto_synced": True,
        }
        for key in ("blob_url", "blob_download_url", "blob_etag", "size"):
            if staged.get(key) not in (None, ""):
                file_meta[key] = staged.get(key)
        contract_files.append(file_meta)
        persisted_contracts.append(file_meta)
        if source_hash:
            existing_by_hash[source_hash] = file_meta
        existing_by_name[(contract_year, original_filename)] = file_meta
        if source_exists:
            try:
                os.remove(source_path)
            except OSError:
                pass

    ensure_sales_contract_codes(contract_files, relation_rows)
    remap_sales_contract_rows(relation_rows, contract_files)
    data["gaoxin_attachments"] = attachments
    _seed_required_material_relation_rows(data, company, persisted_contracts)
    finance_data = session.get("last_finance_data")
    if not isinstance(finance_data, dict) or not finance_data:
        finance_data = {
            key: value
            for key, value in data.items()
            if key.startswith("fin_") and value not in (None, "")
        }
    try:
        patent_certs = json.loads(company.ip_certs_json or "[]")
    except (json.JSONDecodeError, TypeError):
        patent_certs = []
    patent_certs = patent_certs if isinstance(patent_certs, list) else []
    effective_staff_rows = staff_rows or (
        data.get("hr_staff_rows") if isinstance(data.get("hr_staff_rows"), list) else []
    )
    data["required_materials"] = {
        "finance": {
            "recognized": bool(finance_data),
            "field_count": len(finance_data) if isinstance(finance_data, dict) else 0,
            "validation": session.get("last_finance_validation") or {},
        },
        "patents": {"count": len(patent_certs)},
        "staff": {
            "filename": staff.get("filename", "")
            or ((data.get("required_materials") or {}).get("staff") or {}).get("filename", ""),
            "count": len(effective_staff_rows),
        },
        "sales_contracts": [
            {
                "id": item.get("id", ""),
                "original_filename": item.get("original_filename", ""),
                "year": item.get("year", ""),
                "contract_sequence": item.get("contract_sequence", ""),
                "contract_code": item.get("contract_code", ""),
                "summary": item.get("summary", ""),
                "keywords": item.get("keywords", ""),
            }
            for item in selectable_sales_contracts(contract_files, relation_rows)
            if isinstance(item, dict)
        ],
    }
    company.data_json = json.dumps(data, ensure_ascii=False)
    if preserved_staged_paths:
        _clear_required_materials(preserve_relative_paths=preserved_staged_paths)
    else:
        _clear_required_materials()


@scoring_bp.route("/", methods=["GET"])
@login_required
def index():
    """评分首页 — 选择申报类型"""
    return render_template("score_index.html")


@scoring_bp.route("/zhuanjing", methods=["GET", "POST"])
@login_required
def zhuanjing():
    """专精特新中小企业评分"""
    if request.method == "POST":
        form_data = request.form.to_dict()
        score_data = _build_zhuanjing_score_data(form_data)
        result = calculate(score_data, rule_type="专精特新")
        ai_analysis = analyze(result, score_data)

        company_name = form_data.get("company_name", "未命名企业").strip()
        company = _get_or_create_company(company_name, "专精特新", score_data)
        _upsert_score_record(company, "专精特新", result, ai_analysis)
        db.session.commit()

        session["last_score_result"] = result
        session["last_ai_analysis"] = ai_analysis
        session["last_company_name"] = company_name
        session["last_company_id"] = company.id

        flash(f"评分完成！总分 {result['total_score']} 分", "success")
        return redirect(url_for("scoring.result", company_id=company.id))

    return render_template("score_zhuanjing_form.html", rule_type="专精特新")


@scoring_bp.route("/xiaojuren", methods=["GET", "POST"])
@login_required
def xiaojuren():
    """国家级专精特新小巨人企业评分"""
    if request.method == "POST":
        form_data = request.form.to_dict()
        score_data = _build_zhuanjing_score_data(form_data)
        result = calculate(score_data, rule_type="小巨人")
        ai_analysis = analyze(result, score_data)

        # 基本条件校验
        conditions = _check_xiaojuren_conditions(form_data)
        result["basic_conditions"] = conditions

        company_name = form_data.get("company_name", "未命名企业").strip()
        company = _get_or_create_company(company_name, "小巨人", score_data)
        _upsert_score_record(company, "小巨人", result, ai_analysis)
        db.session.commit()

        session["last_score_result"] = result
        session["last_ai_analysis"] = ai_analysis
        session["last_company_name"] = company_name
        session["last_company_id"] = company.id
        session["last_basic_conditions"] = conditions

        # 条件未全满足时追加警告
        if not conditions["all_passed"]:
            flash(f"评分完成！总分 {result['total_score']} 分，⚠️ 基本条件 {conditions['passed_count']}/{conditions['total_count']} 未全部满足", "error")
        else:
            flash(f"评分完成！总分 {result['total_score']} 分", "success")
        return redirect(url_for("scoring.result", company_id=company.id))

    return render_template("score_zhuanjing_form.html", rule_type="小巨人")


@scoring_bp.route("/gaoxin", methods=["GET", "POST"])
@login_required
def gaoxin():
    """高新技术企业评分"""
    if request.method == "POST":
        form_data = request.form.to_dict()

        # 构建评分数据
        score_data = _build_score_data(form_data)

        # 计算成长性指标
        if _has_financial_data(form_data):
            financials = {
                "year1_net_assets": float(form_data.get("year1_net_assets", 0) or 0),
                "year2_net_assets": float(form_data.get("year2_net_assets", 0) or 0),
                "year3_net_assets": float(form_data.get("year3_net_assets", 0) or 0),
                "year1_sales": float(form_data.get("year1_sales", 0) or 0),
                "year2_sales": float(form_data.get("year2_sales", 0) or 0),
                "year3_sales": float(form_data.get("year3_sales", 0) or 0),
            }
            rates = calculate_growth_rates(financials)
            score_data.update(rates)
        else:
            # 手动输入增长率（表单输入百分比，需转换为小数）
            raw_rate = float(form_data.get("growth_net_assets_rate", 0) or 0)
            score_data["growth_net_assets_rate"] = raw_rate / 100 if raw_rate > 1 else raw_rate
            raw_rate = float(form_data.get("growth_sales_rate", 0) or 0)
            score_data["growth_sales_rate"] = raw_rate / 100 if raw_rate > 1 else raw_rate

        # 执行评分
        result = calculate(score_data, rule_type="高新技术")
        finance_validation = session.get("last_finance_validation")
        if finance_validation:
            result["finance_validation"] = finance_validation

        # AI 定性分析
        ai_analysis = analyze(result, score_data)

        # 保存到数据库：同一公司+申报类型再次评分时更新原记录
        company_name = form_data.get("company_name", "未命名企业").strip()
        company = _get_or_create_company(company_name, "高新技术", score_data)
        _persist_session_ip_certs(company)
        _persist_required_materials(company)
        _upsert_score_record(company, "高新技术", result, ai_analysis)
        db.session.commit()

        # 存入 session 供结果页使用
        session["last_score_result"] = result
        session["last_ai_analysis"] = ai_analysis
        session["last_company_name"] = company_name
        session["last_company_id"] = company.id

        flash(f"评分完成！总分 {result['total_score']} 分", "success")
        return redirect(url_for("scoring.result", company_id=company.id))

    # 读取 IP 分析 token
    token = request.args.get("ip_token", "")
    ip_eval = None
    if token:
        from modules.parser.routes import _ip_results
        ip_eval = _ip_results.pop(token, None)
    else:
        session.pop("ip_certificates", None)

    return render_template("score_gaoxin_form.html", ip_eval=ip_eval, company_id=session.get("last_company_id"))


@scoring_bp.route("/result")
@login_required
def result():
    """评分结果页 — 从 DB 加载最新记录"""
    company_id = request.args.get("company_id") or session.get("last_company_id")
    result_json = session.get("last_score_result")
    ai_analysis = session.get("last_ai_analysis")
    company_name = session.get("last_company_name", "未知企业")

    if not result_json:
        # 会话过期或直接打开结果页时，从指定企业或当前用户的最近评分恢复。
        record_query = (
            ScoreRecord.query
            .join(Company)
            .filter(Company.user_id == current_user.id)
        )
        if company_id:
            record_query = record_query.filter(Company.id == int(company_id))
        record = record_query.order_by(ScoreRecord.created_at.desc(), ScoreRecord.id.desc()).first()
        if record:
            # 根据项目类型确定达标线和规则名
            if record.score_type == "专精特新":
                pass_score = 50
                rule_name = "浙江省专精特新中小企业认定评分标准"
            elif record.score_type == "小巨人":
                pass_score = 60
                rule_name = "国家级专精特新小巨人企业认定评分标准"
            else:
                pass_score = 71
                rule_name = "国家高新技术企业认定评分标准"

            result_json = {
                "rule_type": record.score_type,
                "rule_name": rule_name,
                "total_score": record.total_score,
                "full_score": 100,
                "pass_score": pass_score,
                "passed": record.total_score >= pass_score,
                "breakdown": json.loads(record.breakdown_json),
                "warnings": [] if record.total_score >= pass_score else [f"距离达标还差 {pass_score - record.total_score} 分"],
            }
            company_name = record.company.name
            if record.ai_analysis:
                ai_analysis = json.loads(record.ai_analysis)
            else:
                # 旧记录无 AI 分析，用规则引擎生成
                from modules.ai.analyzer import analyze
                ai_analysis = analyze(result_json)

    if not result_json:
        flash("暂无评分结果，请先进行评分", "error")
        return redirect(url_for("scoring.index"))

    basic_conditions = session.get("last_basic_conditions")
    return render_template("score_result.html", result=result_json, company_name=company_name,
                           ai_analysis=ai_analysis, basic_conditions=basic_conditions)


@scoring_bp.route("/history")
@login_required
def history():
    """历史评分记录"""
    all_records = (
        ScoreRecord.query
        .join(Company)
        .filter(Company.user_id == current_user.id)
        .order_by(ScoreRecord.created_at.desc())
        .all()
    )
    records = []
    seen = set()
    for record in all_records:
        key = record.company.name
        if key in seen:
            continue
        seen.add(key)
        records.append(record)
        if len(records) >= 20:
            break
    return render_template("score_history.html", records=records)


def _build_score_data(form_data: dict) -> dict:
    """从表单数据构建评分输入"""
    data = {"company_name": form_data.get("company_name", "")}

    # IP 指标
    for key in ["ip_tech_level", "ip_core_support", "ip_quantity", "ip_acquisition", "ip_standard"]:
        data[key] = form_data.get(key, "E")
        score_key = f"{key}_score"
        if form_data.get(score_key):
            data[score_key] = int(form_data[score_key])

    # 科技成果转化：年平均数 = 专利数量 / 3
    patent_count = int(float(form_data.get("patent_count", 0) or 0))
    transform_count = patent_count / 3 if patent_count else float(form_data.get("transform_count", 0) or 0)
    data["patent_count"] = patent_count
    data["transform_count"] = transform_count

    # R&D 管理
    rd_defaults = {
        "rd_system": 5,
        "rd_institution": 5,
        "rd_transform_incentive": 3,
        "rd_talent": 3,
    }
    for key, default in rd_defaults.items():
        data[key] = int(form_data.get(key, default) or default)

    # 保留评分页上传/识别出的原始财务字段，供后续申请书自动回填。
    for key, value in form_data.items():
        if not value:
            continue
        if key.startswith("fin_") or key.startswith("year") or key in {
            "staff_total", "tech_staff", "rd_total_3y", "revenue_1y",
            "growth_net_assets_rate", "growth_sales_rate",
        }:
            data[key] = value

    return data


def _has_financial_data(form_data: dict) -> bool:
    """检查是否提供了原始财务数据"""
    return any(
        form_data.get(f"year{i}_{field}")
        for i in [1, 2, 3]
        for field in ["net_assets", "sales"]
    )


def _build_zhuanjing_score_data(form_data: dict) -> dict:
    """从专精特新表单构建评分数据"""
    data = {"company_name": form_data.get("company_name", "")}

    # 专业化
    data["revenue_ratio"] = form_data.get("revenue_ratio", "D")
    raw = form_data.get("revenue_growth", 0) or 0
    data["revenue_growth"] = float(raw) / 100 if float(raw) > 1 else float(raw)
    data["market_years"] = int(form_data.get("market_years", 0) or 0)
    data["product_domain"] = form_data.get("product_domain", "C")

    # 精细化
    data["digital_level"] = form_data.get("digital_level", "C")
    for opt in ["quality_award", "iso9001", "own_brand", "standard_participation"]:
        data[opt] = bool(form_data.get(opt))
    raw = form_data.get("net_profit_rate", 0) or 0
    data["net_profit_rate"] = float(raw) / 100 if float(raw) > 1 else float(raw)
    raw = form_data.get("debt_ratio", 0) or 0
    data["debt_ratio"] = float(raw) / 100 if float(raw) > 1 else float(raw)

    # 特色化
    data["local_feature"] = int(form_data.get("local_feature", 0) or 0)

    # 创新能力
    data["ip_quality"] = form_data.get("ip_quality", "E")
    data["rd_amount"] = float(form_data.get("rd_amount", 0) or 0)
    raw = form_data.get("rd_ratio", 0) or 0
    data["rd_ratio"] = float(raw) / 100 if float(raw) > 1 else float(raw)
    raw = form_data.get("rd_staff_ratio", 0) or 0
    data["rd_staff_ratio"] = float(raw) / 100 if float(raw) > 1 else float(raw)
    data["rd_institution_level"] = form_data.get("rd_institution_level", "E")

    # 允许手动覆盖
    for key in list(form_data.keys()):
        if key.endswith("_score") and form_data.get(key):
            data[key] = int(form_data[key])

    return data


# ===== 小巨人基本条件校验 =====

XIAOJUREN_CONDITIONS = [
    {"id": "cond_1", "title": "成立时间≥3年", "hint": "企业成立于申报年前至少3年"},
    {"id": "cond_2", "title": "主营业务收入占比≥70%", "hint": "近2年主营业务收入占营业收入总额≥70%"},
    {"id": "cond_3", "title": "近2年研发费用占比达标", "hint": "按营收规模分档：≥1亿→3%，5000万-1亿→4%，<5000万→5%"},
    {"id": "cond_4", "title": "主导产品细分市场占有率>10%", "hint": "全国或全省细分市场排名前列"},
    {"id": "cond_5", "title": "未被列入经营异常名录", "hint": "近3年无严重违法失信行为，信用记录良好"},
    {"id": "cond_6", "title": "属于工业六基或制造强国领域", "hint": "符合《工业四基》或制造强国十大重点产业领域"},
    {"id": "cond_7", "title": "近2年主营业务收入平均增长率≥5%", "hint": "成长性指标达标"},
]


def _check_xiaojuren_conditions(form_data: dict) -> dict:
    """
    校验小巨人 7 项基本条件

    返回:
    {
        "all_passed": True/False,
        "passed_count": 6,
        "total_count": 7,
        "items": [
            {"id": "cond_1", "title": "成立时间≥3年", "hint": "...", "passed": True},
            ...
        ]
    }
    """
    items = []
    passed_count = 0

    for cond in XIAOJUREN_CONDITIONS:
        passed = form_data.get(cond["id"]) == "1"
        if passed:
            passed_count += 1
        items.append({
            "id": cond["id"],
            "title": cond["title"],
            "hint": cond["hint"],
            "passed": passed,
        })

    return {
        "all_passed": passed_count == len(XIAOJUREN_CONDITIONS),
        "passed_count": passed_count,
        "total_count": len(XIAOJUREN_CONDITIONS),
        "items": items,
    }
