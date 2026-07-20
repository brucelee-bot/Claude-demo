"""文件上传 + 财务数据提取 路由"""
import os
import json
import re
import uuid
import hashlib
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from flask import Blueprint, request, jsonify, send_file, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from config import Config
from modules.docgen.date_logic import (
    enforce_transformation_wording,
    enforce_temporal_wording,
    project_temporal_context,
)
from modules.docgen.product_terms import (
    infer_ps_kind,
    normalize_ps_reference_text,
    ps_statement_case_template,
    ps_type_label,
)
from modules.docgen.sales_contracts import (
    SALES_CONTRACT_YEARS,
    ensure_sales_contract_codes,
    next_sales_contract_identity,
)
from modules.storage import (
    blob_enabled,
    blob_metadata,
    delete_file,
    ensure_local_file,
    generate_client_upload_token,
    persist_file,
)

parser_bp = Blueprint("parser", __name__)

ALLOWED_EXTENSIONS = {'xlsx', 'xls', 'pdf', 'docx'}
UPLOAD_DEBUG_VERSION = "finance-upload-v2"



_ip_cert_store = {}
_required_material_store = {}


def _normalize_ai_text_spacing(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n[ \t　]*\n+", "\n", text)


def _generate_rd_application_sections(
    call_llm,
    project_desc,
    field_guide,
    template_instruction,
    global_time_rule,
    target_words,
):
    section_groups = (
        (
            "立项与技术方案",
            "立项背景与必要性、拟解决的技术问题、研发目标与考核指标、研发内容、技术路线",
        ),
        (
            "实施与管理",
            "创新点、项目组织与任务分工、经费预算、计划进度、过程记录与质量控制",
        ),
        (
            "成果与验收",
            "预期成果与阶段成果、RD-IP-PS关联、验收指标对照、验收意见、验收结论",
        ),
    )
    section_target = max(300, int(target_words / 5))
    section_max_words = int(section_target * 1.1)
    section_max_tokens = 1000

    def generate(group):
        group_name, headings = group
        prompt = f"""你正在分段撰写一份完整的高新技术企业科研项目书。本次只撰写“{group_name}”这一组章节。

企业及项目上下文：
{project_desc}

本组必须依次使用以下标题：
{headings}

完整项目书撰写要求：
{field_guide}{template_instruction}{global_time_rule}

本组要求：
1. 只输出本组列出的章节，不要输出其他组章节，不要重复项目书总标题。
2. 各章节必须结合项目实际，明确研究对象、应用场景和技术路线。
3. 本组合计约 {section_target} 字，允许上下浮动 10%，不得超过 {section_max_words} 字。
4. 未提供的量化指标、人员、费用明细、检测数据和日期写“待补充”，不得编造。
5. 用户提供模板时，只参考模板中与本组标题对应的内容和结构。
6. 不要输出 Markdown 符号，段落之间只保留一个换行，直接输出正文。"""
        return call_llm(
            [
                {
                    "role": "system",
                    "content": "你是高新技术企业科研项目书撰写专家，只输出指定章节正文。",
                },
                {"role": "user", "content": prompt},
            ],
            temperature=0.5,
            max_tokens=section_max_tokens,
            timeout=40,
            max_attempts=2,
        )

    with ThreadPoolExecutor(max_workers=len(section_groups)) as executor:
        results = list(executor.map(generate, section_groups))

    failed = [
        result.get("error") or "上游模型未返回内容"
        for result in results
        if not result.get("success")
    ]
    if failed:
        return {
            "success": False,
            "error": f"科研项目书分段生成失败：{failed[0]}",
        }

    sections = [
        _normalize_ai_text_spacing(result.get("content") or "")
        for result in results
    ]
    if any(not section for section in sections):
        return {"success": False, "error": "科研项目书部分章节为空，请重试"}
    return {"success": True, "content": "\n".join(sections)}


def _ip_store_key() -> str:
    key = session.get("ip_cert_store_key")
    if not key:
        key = uuid.uuid4().hex
        session["ip_cert_store_key"] = key
    return key


def _material_store_key() -> str:
    key = session.get("required_material_store_key")
    if not key:
        key = uuid.uuid4().hex
        session["required_material_store_key"] = key
    return key


def _get_required_materials() -> dict:
    key = _material_store_key()
    materials = _required_material_store.setdefault(
        key,
        {"finance": [], "staff": {}, "sales_contracts": []},
    )
    if not isinstance(materials.get("finance"), list):
        materials["finance"] = []
    if not isinstance(materials.get("staff"), dict):
        materials["staff"] = {}
    if not isinstance(materials.get("sales_contracts"), list):
        materials["sales_contracts"] = []
    ensure_sales_contract_codes(materials["sales_contracts"])
    return materials


def _clear_required_materials(preserve_relative_paths=None) -> None:
    preserved = {
        str(path or "").replace("\\", "/").strip("/")
        for path in (preserve_relative_paths or [])
        if str(path or "").strip()
    }
    key = session.get("required_material_store_key")
    if key:
        materials = _required_material_store.pop(key, None) or {}
        for item in materials.get("sales_contracts") or []:
            if isinstance(item, dict):
                relative_path = str(item.get("relative_path") or "").replace("\\", "/").strip("/")
                if relative_path not in preserved:
                    _delete_relative_upload(relative_path)
    session.pop("required_material_store_key", None)


def _get_ip_certs() -> list:
    key = session.get("ip_cert_store_key")
    if key and key in _ip_cert_store:
        return _ip_cert_store[key]
    certs = session.get("ip_certificates", [])
    if certs:
        _ip_cert_store[_ip_store_key()] = certs
        session.pop("ip_certificates", None)
        return certs
    return []


def _set_ip_certs(certs: list) -> None:
    _ip_cert_store[_ip_store_key()] = certs
    session.pop("ip_certificates", None)


def _jsonify_upload(payload: dict, status: int = 200):
    data = dict(payload)
    data["debug"] = UPLOAD_DEBUG_VERSION
    return jsonify(data), status


def _safe_upload_filename(filename: str) -> str:
    name = os.path.basename(filename or "").replace("/", "_").replace("\\", "_").strip()
    if name:
        return name
    return secure_filename(filename)


def _relative_upload_path(*parts):
    return os.path.join(*[str(part).strip(os.sep) for part in parts if str(part or "").strip(os.sep)])


def _upload_abs_path(relative_path):
    root = os.path.abspath(Config.UPLOAD_FOLDER)
    target = os.path.abspath(os.path.join(root, relative_path))
    if target != root and not target.startswith(root + os.sep):
        raise ValueError("非法文件路径")
    return ensure_local_file(target, relative_path)


def _delete_relative_upload(relative_path):
    relative_path = str(relative_path or "").strip()
    if not relative_path:
        return
    root = os.path.abspath(Config.UPLOAD_FOLDER)
    target = os.path.abspath(os.path.join(root, relative_path))
    if target != root and not target.startswith(root + os.sep):
        return
    try:
        if os.path.exists(target):
            os.remove(target)
    except OSError:
        pass
    delete_file(relative_path)


def _file_sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _ensure_ip_cert_identity(cert):
    if not isinstance(cert, dict):
        return cert
    if not cert.get("id"):
        cert["id"] = uuid.uuid4().hex
    return cert


def _ip_cert_duplicate_key(cert):
    parsed = cert.get("parsed") or {}
    details = parsed.get("details") or {}
    source_pdf = cert.get("source_pdf") or {}
    return {
        "sha256": str(source_pdf.get("sha256") or "").strip(),
        "name": str(details.get("name") or "").strip(),
        "patent_no": str(details.get("patent_no") or details.get("grant_no") or "").strip(),
    }


def _find_duplicate_ip_cert(certs, cert):
    incoming = _ip_cert_duplicate_key(cert)
    for existing in certs:
        if not isinstance(existing, dict):
            continue
        current = _ip_cert_duplicate_key(existing)
        if incoming["sha256"] and current["sha256"] == incoming["sha256"]:
            return existing
        if incoming["patent_no"] and current["patent_no"] == incoming["patent_no"]:
            return existing
        if incoming["name"] and current["name"] == incoming["name"]:
            return existing
    return None


def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS


@parser_bp.route("/upload", methods=["POST"])
@login_required
def upload():
    """上传财报文件，自动提取数据"""
    if 'file' not in request.files:
        return _jsonify_upload({"success": False, "error": "未选择文件"}, 400)

    file = request.files['file']
    if file.filename == '':
        return _jsonify_upload({"success": False, "error": "文件名为空"}, 400)

    if not allowed_file(file.filename):
        return _jsonify_upload({"success": False, "error": f"不支持的文件格式，仅支持: {', '.join(ALLOWED_EXTENSIONS)}"}, 400)

    filename = _safe_upload_filename(file.filename)
    filepath = os.path.join(Config.UPLOAD_FOLDER, filename)
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    file.save(filepath)

    from modules.parser.finance_extractor import extract_with_validation

    try:
        extraction = extract_with_validation(filepath)
        data = extraction.get("data", {})
        validation = extraction.get("validation", {})
        sources = extraction.get("sources", {})
    except ValueError as e:
        msg = str(e)
        if "格式不支持" in msg:
            code = 415
            tip = "请将文件另存为 .xlsx 后重试"
        elif "空表" in msg or "表头" in msg:
            code = 422
            tip = "请确认财务报表中包含年份、科目和数值列"
        elif "缺少 .xls 解析依赖" in msg:
            code = 500
            tip = "服务器缺少旧版 Excel 解析依赖，请安装 python-calamine/xlrd3；临时可另存为 .xlsx 上传"
        elif "损坏" in msg or "不兼容" in msg:
            code = 422
            tip = "请用 Excel/LibreOffice 打开后另存为 .xlsx 再上传"
        else:
            code = 500
            tip = "请检查文件是否为有效的财务报表"
        return _jsonify_upload({"success": False, "error": msg, "code": code, "tip": tip}, code)
    except Exception as e:
        return _jsonify_upload({"success": False, "error": f"数据提取失败: {str(e)}", "code": 500, "tip": "请检查文件是否可正常打开"}, 500)

    # 空结果按解析失败处理，避免前端显示成功但无法自动填充
    if not isinstance(data, dict) or not any(k != "error" and v for k, v in data.items()):
        try:
            os.remove(filepath)
        except OSError:
            pass
        return _jsonify_upload({
            "success": False,
            "error": "未识别到可自动填充的财务数据",
            "code": 422,
            "tip": "请确认文件包含资产负债表或利润表，并且有可复制的科目名称、年份和金额；扫描版 PDF 请确保文字清晰。",
        }, 422)

    # 持久化财务数据到 session，供评分页和申报书页直接回填
    materials = _get_required_materials()
    file_meta = {
        "id": uuid.uuid4().hex,
        "filename": filename,
        "data": data,
        "validation": validation,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    materials["finance"].append(file_meta)
    merged_data = {}
    for item in materials["finance"]:
        if isinstance(item, dict) and isinstance(item.get("data"), dict):
            merged_data.update(item["data"])
    session["last_finance_data"] = merged_data
    session["last_finance_validation"] = validation

    try:
        os.remove(filepath)
    except OSError:
        pass

    return _jsonify_upload({
        "success": True,
        "data": data,
        "validation": validation,
        "sources": sources,
        "count": len(data),
        "filename": filename,
        "file": {
            "id": file_meta["id"],
            "filename": filename,
            "uploaded_at": file_meta["uploaded_at"],
        },
    })


@parser_bp.route("/upload_ip", methods=["POST"])
@login_required
def upload_ip():
    """上传专利 PDF，解析证书字段并生成高新评分建议。"""
    if "file" not in request.files:
        return jsonify({"success": False, "error": "未选择文件"}), 400

    file = request.files["file"]
    if file.filename == "":
        return jsonify({"success": False, "error": "文件名为空"}), 400

    original_filename = file.filename
    safe_filename = secure_filename(_safe_upload_filename(original_filename)) or f"ip_{uuid.uuid4().hex}.pdf"
    if not safe_filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "专利上传仅支持 PDF 文件"}), 400

    file_id = uuid.uuid4().hex
    stored_filename = f"{file_id}_{safe_filename}"
    relative_path = _relative_upload_path("ip_recognition_pending", current_user.id, _ip_store_key(), stored_filename)
    filepath = _upload_abs_path(relative_path)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    file.save(filepath)
    persist_file(filepath, relative_path)
    file_hash = _file_sha256(filepath)

    from modules.parser.ip_analyzer import extract_text, parse_patent, evaluate_ip

    keep_staged_file = False
    try:
        text = extract_text(filepath)
        if not text.strip():
            return jsonify({"success": False, "error": "未识别到 PDF 文本，请确认文件不是空白或加密 PDF"}), 422

        parsed = parse_patent(text)
        details = parsed.get("details") or {}
        if not details and not parsed.get("patent_type"):
            return jsonify({"success": False, "error": "未识别到专利证书关键信息，请确认上传的是专利证书 PDF"}), 422

        cert = _ensure_ip_cert_identity({
            "filename": safe_filename,
            "parsed": parsed,
            "source_pdf": {
                "id": file_id,
                "original_filename": original_filename,
                "stored_filename": stored_filename,
                "relative_path": relative_path,
                "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "sha256": file_hash,
                "sync_status": "staged",
            },
        })
        certs = [_ensure_ip_cert_identity(c) for c in _get_ip_certs()]
        duplicate_cert = _find_duplicate_ip_cert(certs, cert)
        duplicate = duplicate_cert is not None
        patent_name = (details.get("name") or "").strip()
        if duplicate_cert:
            if not duplicate_cert.get("source_pdf"):
                duplicate_cert["source_pdf"] = cert["source_pdf"]
                keep_staged_file = True
                _set_ip_certs(certs)
            else:
                keep_staged_file = False
        else:
            certs.append(cert)
            keep_staged_file = True
            _set_ip_certs(certs)

        company_id = request.form.get("company_id")
        if company_id and certs:
            try:
                from models import Company, db
                from modules.docgen.routes import sync_ip_cert_pdfs_to_attachments
                company = Company.query.filter_by(id=int(company_id), user_id=current_user.id).first()
                if company:
                    certs = sync_ip_cert_pdfs_to_attachments(company, certs)
                    company.ip_certs_json = json.dumps(certs, ensure_ascii=False)
                    db.session.commit()
                    _set_ip_certs(certs)
                    keep_staged_file = True
            except (TypeError, ValueError):
                pass

        evaluation = evaluate_ip(certs)
        return jsonify({
            "success": True,
            "duplicate": duplicate,
            "message": f"专利「{patent_name}」已存在，未重复添加" if duplicate else "识别成功",
            "certificate": cert,
            "certificates": certs,
            "evaluation": evaluation,
            "count": len(certs),
            "uncertain": ["对主要产品核心支持作用需要结合企业主营产品人工确认，系统默认按较强填入。"],
        })
    finally:
        if not keep_staged_file:
            try:
                os.remove(filepath)
            except OSError:
                pass


@parser_bp.route("/upload_staff_list", methods=["POST"])
@login_required
def upload_staff_list():
    """识别评分阶段上传的人员清单，并暂存供评分提交时统一入库。"""
    upload_file = request.files.get("file")
    if not upload_file or not upload_file.filename:
        return jsonify({"success": False, "error": "请先选择人员清单 Excel 文件"}), 400
    if not upload_file.filename.lower().endswith((".xlsx", ".xlsm")):
        return jsonify({"success": False, "error": "人员清单仅支持 .xlsx 或 .xlsm 文件"}), 400

    try:
        from modules.docgen.routes import _import_hr_staff_excel, _summarize_hr_staff_rows

        rows = _import_hr_staff_excel(upload_file)
        if not rows:
            return jsonify({"success": False, "error": "未识别到有效人员数据"}), 422
        summary = _summarize_hr_staff_rows(rows)
    except Exception as exc:
        return jsonify({"success": False, "error": f"人员清单识别失败：{exc}"}), 400

    materials = _get_required_materials()
    materials["staff"] = {
        "id": uuid.uuid4().hex,
        "filename": _safe_upload_filename(upload_file.filename),
        "rows": rows,
        "summary": summary,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    return jsonify({
        "success": True,
        "filename": materials["staff"]["filename"],
        "rows": rows,
        "summary": summary,
        "count": len(rows),
        "file": {
            "id": materials["staff"]["id"],
            "filename": materials["staff"]["filename"],
            "uploaded_at": materials["staff"]["uploaded_at"],
        },
    })


@parser_bp.route("/staff_list_template", methods=["GET"])
@login_required
def staff_list_template():
    """下载与附件企业人员表字段完全一致的评分阶段人员清单模板。"""
    from modules.docgen.routes import _create_hr_staff_template

    return send_file(
        _create_hr_staff_template(),
        mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        as_attachment=True,
        download_name="企业人员表模板.xlsx",
    )


def _sales_contract_year_count(materials, contract_year):
    return sum(
        1 for item in materials["sales_contracts"]
        if str(item.get("year") or "") == contract_year
    )


def _same_name_sales_contract(materials, original_filename, contract_year):
    return next(
        (
            item for item in materials["sales_contracts"]
            if str(item.get("original_filename") or "") == original_filename
            and str(item.get("year") or "") == contract_year
        ),
        None,
    )


def _sales_contract_relative_path(original_filename, contract_year, file_id):
    safe_filename = (
        secure_filename(original_filename)
        or f"sales_contract_{file_id}.pdf"
    )
    stored_filename = f"{file_id}_{safe_filename}"
    relative_path = _relative_upload_path(
        "required_materials_pending",
        current_user.id,
        _material_store_key(),
        "sales_contracts",
        contract_year,
        stored_filename,
    )
    return stored_filename, relative_path


def _sales_contract_duplicate_response(contract, materials, contract_year):
    return jsonify({
        "success": True,
        "duplicate": True,
        "file": contract,
        "summary": contract.get("summary", ""),
        "keywords": contract.get("keywords", ""),
        "count": _sales_contract_year_count(materials, contract_year),
        "total_count": len(materials["sales_contracts"]),
    })


@parser_bp.route("/sales_contract_upload_ticket", methods=["POST"])
@login_required
def sales_contract_upload_ticket():
    """签发销售合同浏览器直传 Blob 所需的短期凭证。"""
    payload = request.get_json(silent=True) or {}
    original_filename = _safe_upload_filename(str(payload.get("filename") or ""))
    contract_year = str(payload.get("year") or "").strip()

    if not original_filename:
        return jsonify({"success": False, "error": "请先选择销售合同 PDF 文件"}), 400
    if not original_filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "销售合同仅支持 PDF 文件"}), 400
    if contract_year not in SALES_CONTRACT_YEARS:
        return jsonify({"success": False, "error": "请选择 2023、2024 或 2025 年销售合同"}), 400

    materials = _get_required_materials()
    same_name_contract = _same_name_sales_contract(
        materials,
        original_filename,
        contract_year,
    )
    if same_name_contract:
        return _sales_contract_duplicate_response(
            same_name_contract,
            materials,
            contract_year,
        )

    if not blob_enabled():
        return jsonify({"success": True, "direct_upload": False})

    file_id = uuid.uuid4().hex
    stored_filename, relative_path = _sales_contract_relative_path(
        original_filename,
        contract_year,
        file_id,
    )
    try:
        ticket = generate_client_upload_token(relative_path)
    except Exception as exc:
        return jsonify({"success": False, "error": f"无法创建上传凭证：{exc}"}), 500

    return jsonify({
        "success": True,
        "direct_upload": True,
        "upload": {
            "id": file_id,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "relative_path": relative_path,
            **ticket,
        },
    })


@parser_bp.route("/register_sales_contract", methods=["POST"])
@login_required
def register_sales_contract():
    """登记已由浏览器直接保存到 Vercel Blob 的销售合同。"""
    payload = request.get_json(silent=True) or {}
    file_id = str(payload.get("id") or "").strip().lower()
    original_filename = _safe_upload_filename(str(payload.get("filename") or ""))
    contract_year = str(payload.get("year") or "").strip()
    relative_path = str(payload.get("relative_path") or "").replace("\\", "/").strip("/")

    if not re.fullmatch(r"[0-9a-f]{32}", file_id):
        return jsonify({"success": False, "error": "销售合同文件标识无效"}), 400
    if not original_filename or not original_filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "销售合同仅支持 PDF 文件"}), 400
    if contract_year not in SALES_CONTRACT_YEARS:
        return jsonify({"success": False, "error": "请选择 2023、2024 或 2025 年销售合同"}), 400

    stored_filename, expected_relative_path = _sales_contract_relative_path(
        original_filename,
        contract_year,
        file_id,
    )
    expected_relative_path = expected_relative_path.replace("\\", "/").strip("/")
    if relative_path != expected_relative_path:
        return jsonify({"success": False, "error": "销售合同保存路径无效"}), 400

    blob = blob_metadata(relative_path)
    if not blob:
        return jsonify({
            "success": False,
            "error": "未找到已上传的销售合同，请重新上传",
        }), 409

    materials = _get_required_materials()
    same_name_contract = _same_name_sales_contract(
        materials,
        original_filename,
        contract_year,
    )
    if same_name_contract:
        delete_file(relative_path)
        return _sales_contract_duplicate_response(
            same_name_contract,
            materials,
            contract_year,
        )

    year_count = _sales_contract_year_count(materials, contract_year)
    contract_sequence, contract_code = next_sales_contract_identity(
        materials["sales_contracts"],
        contract_year,
    )
    if not contract_sequence:
        delete_file(relative_path)
        return jsonify({
            "success": False,
            "error": f"{contract_year} 年销售合同编号已用完",
        }), 422

    file_meta = {
        "id": file_id,
        "original_filename": original_filename,
        "stored_filename": stored_filename,
        "relative_path": relative_path,
        "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": "score_required_material",
        "year": contract_year,
        "contract_sequence": contract_sequence,
        "contract_code": contract_code,
        "summary": "",
        "keywords": "",
        "sha256": "",
        "blob_url": blob.get("url", ""),
        "blob_download_url": blob.get("downloadUrl", ""),
        "blob_etag": blob.get("etag", ""),
        "size": blob.get("size", payload.get("size", 0)),
    }
    materials["sales_contracts"].append(file_meta)
    return jsonify({
        "success": True,
        "duplicate": False,
        "file": file_meta,
        "summary": "",
        "keywords": "",
        "count": year_count + 1,
        "total_count": len(materials["sales_contracts"]),
    })


@parser_bp.route("/upload_sales_contract", methods=["POST"])
@login_required
def upload_sales_contract():
    """保存评分阶段上传的销售合同，不在申请表阶段解析合同内容。"""
    upload_file = request.files.get("file")
    if not upload_file or not upload_file.filename:
        return jsonify({"success": False, "error": "请先选择销售合同 PDF 文件"}), 400
    if not upload_file.filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "销售合同仅支持 PDF 文件"}), 400

    contract_year = str(request.form.get("year") or "").strip()
    if contract_year not in SALES_CONTRACT_YEARS:
        return jsonify({"success": False, "error": "请选择 2023、2024 或 2025 年销售合同"}), 400

    materials = _get_required_materials()
    original_filename = _safe_upload_filename(upload_file.filename)
    same_name_contract = _same_name_sales_contract(
        materials,
        original_filename,
        contract_year,
    )
    if same_name_contract:
        return _sales_contract_duplicate_response(
            same_name_contract,
            materials,
            contract_year,
        )

    year_count = _sales_contract_year_count(materials, contract_year)
    contract_sequence, contract_code = next_sales_contract_identity(
        materials["sales_contracts"],
        contract_year,
    )
    if not contract_sequence:
        return jsonify({
            "success": False,
            "error": f"{contract_year} 年销售合同编号已用完",
        }), 422

    file_id = uuid.uuid4().hex
    stored_filename, relative_path = _sales_contract_relative_path(
        original_filename,
        contract_year,
        file_id,
    )
    filepath = _upload_abs_path(relative_path)
    os.makedirs(os.path.dirname(filepath), exist_ok=True)
    upload_file.save(filepath)

    try:
        file_hash = _file_sha256(filepath)
        duplicate_by_hash = next(
            (
                item for item in materials["sales_contracts"]
                if str(item.get("sha256") or "") == file_hash
            ),
            None,
        )
        if duplicate_by_hash:
            _delete_relative_upload(relative_path)
            existing_year = str(duplicate_by_hash.get("year") or "")
            if existing_year != contract_year:
                return jsonify({
                    "success": False,
                    "error": f"该合同已上传到 {existing_year or '其他'} 年，请勿跨年度重复上传",
                }), 409
            return jsonify({
                "success": True,
                "duplicate": True,
                "file": duplicate_by_hash,
                "summary": duplicate_by_hash.get("summary", ""),
                "keywords": duplicate_by_hash.get("keywords", ""),
                "count": year_count,
                "total_count": len(materials["sales_contracts"]),
            })

        persist_file(filepath, relative_path)
        file_meta = {
            "id": file_id,
            "original_filename": original_filename,
            "stored_filename": stored_filename,
            "relative_path": relative_path,
            "uploaded_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "source": "score_required_material",
            "year": contract_year,
            "contract_sequence": contract_sequence,
            "contract_code": contract_code,
            "summary": "",
            "keywords": "",
            "sha256": file_hash,
        }
        materials["sales_contracts"].append(file_meta)
        return jsonify({
            "success": True,
            "duplicate": False,
            "file": file_meta,
            "summary": file_meta.get("summary", ""),
            "keywords": file_meta.get("keywords", ""),
            "count": year_count + 1,
            "total_count": len(materials["sales_contracts"]),
        })
    except Exception as exc:
        _delete_relative_upload(relative_path)
        return jsonify({"success": False, "error": f"销售合同保存失败：{exc}"}), 500


@parser_bp.route("/sales_contracts", methods=["GET"])
@login_required
def sales_contracts():
    """Return the current scoring session's saved, numbered sales contracts."""
    materials = _get_required_materials()
    contracts = sorted(
        materials["sales_contracts"],
        key=lambda item: (
            str(item.get("year") or ""),
            int(item.get("contract_sequence") or 0),
            str(item.get("original_filename") or ""),
        ),
    )
    return jsonify({"success": True, "contracts": contracts})


def _merged_finance_material_data(materials):
    merged = {}
    for item in materials.get("finance") or []:
        if isinstance(item, dict) and isinstance(item.get("data"), dict):
            merged.update(item["data"])
    return merged


@parser_bp.route("/required_materials/<section>/<file_id>", methods=["DELETE", "POST"])
@login_required
def delete_required_material(section, file_id):
    """删除评分申请表中指定的临时上传资料。"""
    materials = _get_required_materials()

    if section == "finance":
        finance_files = materials.get("finance") or []
        removed = next(
            (item for item in finance_files if str(item.get("id") or "") == file_id),
            None,
        )
        if not removed:
            return jsonify({"success": False, "error": "财务报表不存在或已删除"}), 404
        materials["finance"] = [item for item in finance_files if item is not removed]
        merged_data = _merged_finance_material_data(materials)
        if merged_data:
            session["last_finance_data"] = merged_data
            latest = materials["finance"][-1]
            session["last_finance_validation"] = latest.get("validation") or {}
        else:
            session.pop("last_finance_data", None)
            session.pop("last_finance_validation", None)
        return jsonify({
            "success": True,
            "remaining": len(materials["finance"]),
            "data": merged_data,
        })

    if section == "staff":
        staff = materials.get("staff") or {}
        if not staff or str(staff.get("id") or "") != file_id:
            return jsonify({"success": False, "error": "人员清单不存在或已删除"}), 404
        materials["staff"] = {}
        return jsonify({"success": True, "remaining": 0})

    if section == "sales_contracts":
        contracts = materials.get("sales_contracts") or []
        removed = next(
            (item for item in contracts if str(item.get("id") or "") == file_id),
            None,
        )
        if not removed:
            return jsonify({"success": False, "error": "销售合同不存在或已删除"}), 404
        materials["sales_contracts"] = [item for item in contracts if item is not removed]
        _delete_relative_upload(removed.get("relative_path"))
        return jsonify({
            "success": True,
            "remaining": len(materials["sales_contracts"]),
            "year": removed.get("year", ""),
        })

    return jsonify({"success": False, "error": "不支持的资料板块"}), 400


@parser_bp.route("/required_materials/<section>", methods=["DELETE"])
@login_required
def clear_required_material_section(section):
    """清空评分申请表中的指定资料板块。"""
    materials = _get_required_materials()
    if section == "finance":
        materials["finance"] = []
        session.pop("last_finance_data", None)
        session.pop("last_finance_validation", None)
        return jsonify({"success": True, "remaining": 0, "data": {}})
    if section == "staff":
        materials["staff"] = {}
        return jsonify({"success": True, "remaining": 0})
    if section == "sales_contracts":
        for item in materials.get("sales_contracts") or []:
            if isinstance(item, dict):
                _delete_relative_upload(item.get("relative_path"))
        materials["sales_contracts"] = []
        return jsonify({"success": True, "remaining": 0})
    return jsonify({"success": False, "error": "不支持的资料板块"}), 400


# 内存临时存储 IP 分析结果 {token: result}
_ip_results = {}


@parser_bp.route("/analyze_ip", methods=["POST"])
@login_required
def analyze_ip():
    """综合评估所有已上传的 IP 文件"""
    data = request.get_json() or {}
    certificates = data.get("certificates", [])
    if certificates:
        _set_ip_certs(certificates)

    from modules.parser.ip_analyzer import evaluate_ip

    result = evaluate_ip(certificates)
    token = uuid.uuid4().hex[:12]
    _ip_results[token] = result
    return jsonify({"success": True, "evaluation": result, "token": token})


@parser_bp.route("/sort_ip_certs", methods=["POST"])
@login_required
def sort_ip_certs():
    """按申请日期重排本次评分的专利证书 session。"""
    data = request.get_json() or {}
    direction = data.get("direction", "asc")
    certs = data.get("certificates") or _get_ip_certs()

    def app_date_key(cert):
        details = cert.get("parsed", {}).get("details", {}) if isinstance(cert, dict) else {}
        value = (details.get("app_date") or "").strip()
        return value or "9999-99-99"

    certs = sorted(certs, key=app_date_key, reverse=(direction == "desc"))
    _set_ip_certs(certs)

    from modules.parser.ip_analyzer import evaluate_ip

    return jsonify({"success": True, "certificates": certs, "evaluation": evaluate_ip(certs), "count": len(certs)})


@parser_bp.route("/delete_ip", methods=["POST"])
@login_required
def delete_ip():
    """按稳定 ID 或索引删除知识产权证书及其暂存文件。"""
    data = request.get_json() or {}
    cert_id = str(data.get("id") or "").strip()
    idx = data.get("index")
    certs = _get_ip_certs()
    if cert_id:
        idx = next(
            (
                index for index, cert in enumerate(certs)
                if str((cert or {}).get("id") or "") == cert_id
            ),
            -1,
        )
    else:
        if idx is None:
            return jsonify({"success": False, "error": "缺少 id 或 index 参数"}), 400
        try:
            idx = int(idx)
        except (ValueError, TypeError):
            return jsonify({"success": False, "error": "index 必须是整数"}), 400

    if not 0 <= idx < len(certs):
        return jsonify({"success": False, "error": "知识产权证书不存在或已删除"}), 404

    removed = certs.pop(idx)
    source_pdf = removed.get("source_pdf") if isinstance(removed, dict) else {}
    if isinstance(source_pdf, dict):
        _delete_relative_upload(source_pdf.get("relative_path"))
        _delete_relative_upload(source_pdf.get("attachment_relative_path"))
    _set_ip_certs(certs)

    company_id = data.get("company_id")
    if company_id:
        try:
            from models import Company, db
            from modules.docgen.routes import sync_ip_cert_pdfs_to_attachments

            company = Company.query.filter_by(
                id=int(company_id),
                user_id=current_user.id,
            ).first()
            if company:
                certs = sync_ip_cert_pdfs_to_attachments(company, certs)
                company.ip_certs_json = json.dumps(certs, ensure_ascii=False)
                db.session.commit()
                _set_ip_certs(certs)
        except (TypeError, ValueError):
            pass

    from modules.parser.ip_analyzer import evaluate_ip

    return jsonify({
        "success": True,
        "removed": removed.get("filename", ""),
        "remaining": len(certs),
        "certificates": certs,
        "evaluation": evaluate_ip(certs),
    })


@parser_bp.route("/save_ip_certs/<int:company_id>", methods=["POST"])
@login_required
def save_ip_certs(company_id):
    """将 session 中的 IP 证书持久化到公司记录"""
    from models import Company, db
    from modules.docgen.routes import sync_ip_cert_pdfs_to_attachments
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    certs = [_ensure_ip_cert_identity(cert) for cert in _get_ip_certs()]
    certs = sync_ip_cert_pdfs_to_attachments(company, certs)
    _set_ip_certs(certs)
    company.ip_certs_json = json.dumps(certs, ensure_ascii=False)
    db.session.commit()
    return jsonify({"success": True, "count": len(certs)})


@parser_bp.route("/load_ip_certs/<int:company_id>")
@login_required
def load_ip_certs(company_id):
    """从 DB 加载 IP 证书到 session"""
    from models import Company
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    try:
        certs = json.loads(company.ip_certs_json or "[]")
        _set_ip_certs(certs)
        return jsonify({"success": True, "count": len(certs)})
    except (json.JSONDecodeError, TypeError):
        return jsonify({"success": True, "count": 0})



    import requests as req
    import re

    results = []
    seen = set()

    # ===== 多源搜索 =====
    search_queries = [
        f"{company_name} 专利 知识产权",
        f"{company_name} 发明专利 实用新型",
    ]

    for query in search_queries[:2]:
        try:
            url = f"https://www.google.com/search?q={req.utils.quote(query)}&hl=zh-CN"
            headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"}
            resp = req.get(url, headers=headers, timeout=10)
            html = resp.text

            # 提取专利号模式: CN 数字 [A-Z]
            patent_patterns = [
                r'CN\s*\d{6,12}\s*[A-Z]',           # CN 217123456 U
                r'ZL\s*\d{6,12}\s*[A-Z]',           # ZL 217123456 U
            ]
            patents_found = []
            for pat in patent_patterns:
                patents_found.extend(re.findall(pat, html, re.IGNORECASE))

            # 提取名称片段（专利号附近的文字）
            for pn in patents_found:
                pn_clean = re.sub(r'\s+', ' ', pn).strip()
                if pn_clean in seen:
                    continue
                seen.add(pn_clean)

                # 尝试在上下文中提取名称
                idx = html.find(pn.replace(' ', ''))
                if idx > 0:
                    context = html[max(0,idx-300):idx+100]
                    # 查找可能的名称
                    name_match = re.search(r'[\u4e00-\u9fff][\u4e00-\u9fff\w]{8,60}', context)
                    name = name_match.group(0) if name_match else company_name + "专利"
                else:
                    name = company_name + "专利"

                results.append({
                    "name": name,
                    "type": "发明" if "发明" in html[max(0,idx-500):idx+200] else "实用新型",
                    "auth_no": pn_clean,
                    "date": "",
                    "acq": "自主研发"
                })

        except Exception:
            continue

    if results:
        return jsonify({"success": True, "data": results, "count": len(results),
                       "source": "web-parse"})
    else:
        return jsonify({"success": False, "error": "未搜索到知识产权信息，请手动上传"}), 500


@parser_bp.route("/ai_polish_intro", methods=["POST"])
@login_required
def ai_polish_intro():
    """企业简介润色：接收用户初稿并按目标字数优化。"""
    data = request.get_json() or {}
    company_name = (data.get("company_name") or "本公司").strip()
    draft = (data.get("draft") or "").strip()
    try:
        target_words = int(data.get("target_words") or 500)
    except (TypeError, ValueError):
        target_words = 500
    target_words = max(100, min(2000, target_words))

    if not draft:
        return jsonify({"success": False, "error": "请先填写企业简介草稿"}), 400

    prompt = f"""你是高新技术企业认定申报书撰写专家。请将下面的企业简介草稿润色成适合《高新技术企业认定申请书》使用的正式文本。

要求：
1. 保留用户已写入的信息，不要编造不存在的事实。
2. 语言正式、客观、申报书风格。
3. 目标长度为 {target_words} 字，尽量贴近该字数，允许上下浮动 10%。
4. 内容不足时可以扩展表达、补充逻辑衔接和申报书常用表述，但不得编造具体数据、资质、客户、专利、荣誉或财务信息。
5. 段落之间只保留一个换行，不要输出连续空行。
6. 直接输出润色后的正文。

企业名称：{company_name}
用户草稿：
{draft}"""

    try:
        from modules.ai.llm_client import call_llm
    except Exception:
        return jsonify({"success": False, "error": "AI 客户端不可用"}), 500

    max_tokens = max(800, min(4000, int(target_words * 2.2)))
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报书撰写专家，只输出最终正文。"},
        {"role": "user", "content": prompt},
    ], temperature=0.4, max_tokens=max_tokens, timeout=45, max_attempts=2)

    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "AI 调用失败")}), 500

    content = _normalize_ai_text_spacing(result.get("content") or "")
    if content.startswith("```"):
        content = _normalize_ai_text_spacing("\n".join(content.splitlines()[1:-1]))

    actual_words = len("".join(content.split()))
    return jsonify({"success": True, "text": content, "target_words": target_words, "actual_words": actual_words})


@parser_bp.route("/ai_polish_business_scope", methods=["POST"])
@login_required
def ai_polish_business_scope():
    """经营范围润色：接收用户草稿并优化为申报书可用表述。"""
    data = request.get_json() or {}
    company_name = (data.get("company_name") or "本公司").strip()
    draft = (data.get("draft") or "").strip()
    try:
        target_words = int(data.get("target_words") or 180)
    except (TypeError, ValueError):
        target_words = 180
    target_words = max(50, min(800, target_words))

    if not draft:
        return jsonify({"success": False, "error": "请先填写经营范围草稿"}), 400

    prompt = f"""你是高新技术企业认定申报书撰写专家。请将下面的经营范围草稿润色为适合《高新技术企业认定申请书》使用的正式经营范围描述。

要求：
1. 必须保留草稿中的业务事实、产品/服务方向和许可项目，不得新增不存在的经营资质、许可、产品、客户、收入、专利或荣誉。
2. 表述要正式、客观、清晰，可按“一般项目/许可项目”或连续句式整理。
3. 不要写成企业简介，不要加入成立时间、规模、团队、行业地位等非经营范围信息。
4. 目标长度为 {target_words} 字，尽量贴近该字数，允许上下浮动 10%。
5. 只输出润色后的经营范围正文，不要加标题、说明或 Markdown。

企业名称：{company_name}
经营范围草稿：
{draft}"""

    try:
        from modules.ai.llm_client import call_llm
    except Exception:
        return jsonify({"success": False, "error": "AI 客户端不可用"}), 500

    max_tokens = max(500, min(1800, int(target_words * 2.5)))
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报书撰写专家，只输出最终经营范围正文。"},
        {"role": "user", "content": prompt},
    ], temperature=0.3, max_tokens=max_tokens, timeout=35, max_attempts=2)

    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "AI 调用失败")}), 500

    content = _normalize_ai_text_spacing(result.get("content") or "")
    if content.startswith("```"):
        content = _normalize_ai_text_spacing("\n".join(content.splitlines()[1:-1]))

    actual_words = len("".join(content.split()))
    return jsonify({"success": True, "text": content, "target_words": target_words, "actual_words": actual_words})


@parser_bp.route("/ai_write", methods=["POST"])
@login_required
def ai_write():
    """AI 辅助撰写 RD 活动描述（调用 MiMo）"""
    data = request.get_json() or {}
    field = data.get("field", "")       # purpose / innovation / result
    context = data.get("context", {})   # {rd_name, rd_field, rd_source, company_name}
    user_template = (data.get("template") or "").strip()
    try:
        target_words = int(data.get("target_words") or 400)
    except (TypeError, ValueError):
        target_words = 400
    target_words = max(
        100,
        min(3200 if field == "rd_application" else 2000, target_words),
    )

    if field not in ("purpose", "innovation", "result", "rd_application", "hitech_product_summary", "ps_statement", "ps_tech", "ps_advantage", "ps_support",
                      "innovation_ip", "innovation_transform", "innovation_rd_mgmt", "innovation_staff", "cv_desc", "achievement_test_report", "achievement_user_report"):
        return jsonify({"success": False, "error": "field 不在允许范围内"}), 400

    rd_name = context.get("rd_name", "未命名项目")
    rd_period = context.get("rd_period", "")
    rd_year = context.get("rd_year", "")
    rd_code = context.get("rd_code", "")
    rd_field = context.get("rd_field", "未指定领域")
    rd_source = context.get("rd_source", "自主研发")
    rd_ip = context.get("rd_ip", "")
    rd_ps = context.get("rd_ps", "")
    rd_purpose = context.get("rd_purpose", "")
    rd_innovation = context.get("rd_innovation", "")
    rd_result = context.get("rd_result", "")
    rd_budget = context.get("rd_budget", "")
    rd_technologies = context.get("rd_technologies", "")
    rd_result_names = context.get("rd_result_names", "")
    ps_name = (
        context.get("ps_name")
        or context.get("cv_ps")
        or context.get("achievement_ps")
        or "未命名产品"
    )
    ps_field = context.get("ps_field", "未指定领域")
    ps_source = context.get("ps_source", "")
    ps_revenue = context.get("ps_revenue", "")
    ps_revenue_year = context.get("ps_revenue_year", "")
    ps_ip = context.get("ps_ip", "")
    ps_code = context.get("ps_code", "")
    ps_rds = context.get("ps_rds", "")
    ps_technologies = context.get("ps_technologies", "")
    ps_results = context.get("ps_results", "")
    ps_tech = context.get("ps_tech", "")
    ps_advantage = context.get("ps_advantage", "")
    ps_support = context.get("ps_support", "")
    ps_kind = infer_ps_kind(ps_name, context.get("ps_kind"))
    ps_label = ps_type_label(ps_name, ps_kind)
    if field == "ps_statement":
        user_template = ps_statement_case_template(ps_label)
    products_context = context.get("products_context", "")
    company = context.get("company_name", "本公司")

    field_labels = {
        "purpose": "目的及组织实施方式",
        "innovation": "核心技术及创新点",
        "result": "取得的阶段性成果",
        "rd_application": "研发项目申报书",
        "hitech_product_summary": f"高新技术{ps_label}汇总表",
        "ps_statement": "PS情况说明",
        "ps_tech": "关键技术及主要技术指标",
        "ps_advantage": "与同类产品的竞争优势",
        "ps_support": "知识产权对产品的支持作用",
        "innovation_ip": "知识产权对企业竞争力的作用",
        "innovation_transform": "科技成果转化情况",
        "innovation_rd_mgmt": "研究开发与技术创新组织管理情况",
        "innovation_staff": "管理与科技人员情况",
        "cv_desc": "成果转化描述",
        "achievement_test_report": "成果转化检测报告",
        "achievement_user_report": "用户使用报告",
    }
    field_guides = {
        "purpose": "说明该研发项目要解决什么技术问题、采用什么组织方式（独立研发/产学研合作等）、研发阶段划分",
        "innovation": "从技术原理、实现方法、性能指标等角度描述创新点，突出与现有技术的区别和优势",
        "result": "根据项目周期和已有证明材料描述预期成果、阶段成果或已取得成果；不得仅因项目到期就编造专利、样机、应用或销售收入",
        "rd_application": (
            "撰写完整、可归档的科研项目书，必须依次使用以下标题：立项背景与必要性、拟解决的技术问题、"
            "研发目标与考核指标、研发内容、技术路线、创新点、项目组织与任务分工、经费预算、计划进度、"
            "过程记录与质量控制、预期成果与阶段成果、RD-IP-PS关联、验收指标对照、验收意见、验收结论。"
            "每部分必须结合项目实际，明确研究对象、应用场景和技术路线，使名称相近的项目在技术问题、路线、"
            "成果和应用对象上能够区分。考核指标采用已提供指标；未提供量化值时写明指标类别和核验方式，"
            "不得编造数值。人员姓名、费用明细、检测数据和日期缺失时写“待补充”。不得输出 Markdown 符号。"
        ),
        "hitech_product_summary": f"按照模板生成高新技术{ps_label}汇总表内容，逐项列出PS编号、{ps_label}名称、技术领域、上年度销售收入、知识产权获得情况和证明材料；必须使用上下文已提供的{ps_label}、收入和知识产权信息，不得编造销售额、专利号或证明材料；可使用自然分段，不要输出 Markdown 表格符号",
        "ps_statement": (
            f"按照已提供的三个案例所归纳出的固定写法，撰写单个高新技术{ps_label}的 PS 情况说明。"
            f"正文保持 4 至 6 个自然段，不使用“一、二、三”等章节标题：首句点明 PS 编号、{ps_label}名称及"
            "“具有以下优势”；中间用 2 至 4 个自然段归纳核心技术、技术原理和实际优势；随后单独用一段准确统计"
            "关联知识产权数量，并逐项或按技术族说明支撑关系；最后一段说明实施与销售成效。"
            f"必须结合{ps_label}编号、名称、领域、关联知识产权、核心技术、研发成果和销售收入。"
            "知识产权数量必须根据已提供列表计算；没有明确技术对应关系时不得强行配对。"
            "不得编造客户、合同、资质、年度、金额、利润、批量实施状态或其他未提供事实。"
        ),
        "ps_tech": f"说明该{ps_label}采用的关键技术、技术指标（性能参数、精度、效率等），突出技术先进性和独特性",
        "ps_advantage": f"与市场上同类{ps_label}对比，从技术指标、成本、性能、服务等角度分析竞争优势",
        "ps_support": f"说明该{ps_label}拥有的知识产权如何支撑{ps_label}的核心技术，阐述IP与{ps_label}的对应关系及保护范围",
        "innovation_ip": "优先依据 RD-IP-PS-成果关联详情表中的知识产权名称、对应 RD 和 PS，写明知识产权如何支撑具体研发方向和产品（服务）；仅使用已提供的数量和名称",
        "innovation_transform": "优先依据 RD-IP-PS-成果关联详情表中的成果名称、RD、知识产权和 PS 对应关系，写明成果转化链路与形式；仅使用已提供的数量和名称",
        "innovation_rd_mgmt": "结合 RD-IP-PS-成果关联详情表中研发活动、知识产权、成果和 PS 的对应关系，说明企业如何按项目组织研发和成果转化；制度、合作等未提供事实不得编造",
        "cv_desc": (
            "按科技成果名称、项目时间状态、关联RD、计划周期、关联知识产权、关联PS、"
            "技术关联说明、核验说明和核验材料的顺序撰写。没有真实转化记录时，不得写成"
            "已经完成转化、投入应用或产生效益，只能说明资料对应关系和待核验事项"
        ),
        "achievement_test_report": f"撰写本公司内部检测报告：这是本公司对本技术及其{ps_label}开展的内部检测，不是第三方检测报告。必须严格按检测单位、检测对象、被检测技术、检测目的、检测依据、检测方法、检测项目1、检测结果1、检测项目2、检测结果2、检测项目3、检测结果3、检测结论、检测单位（盖章）、日期的顺序逐行填写，供系统自动排版为表格。检测项目和结果均限一句简短文字，重点说明技术功能、运行表现或适配情况；不得写成长篇过程说明、Markdown 表格或额外标题。不得虚构第三方机构、报告编号、检测人员、具体日期、量化指标或实际检测结果；缺少内部检测事实、依据或数据时必须留空。",
        "achievement_user_report": f"撰写用户使用报告：这是客户或使用单位对本公司{ps_label}实际使用情况和服务体验的评价，不是技术说明或检测报告。必须严格按使用单位、被评价单位、{ps_label}名称、使用时间、应用场景、功能适用性、运行稳定性、操作便利性、服务响应、综合评价、客户意见及建议、使用单位（盖章）、日期的顺序逐行填写，供系统自动排版为表格。应用场景写实际使用情况；功能、稳定性、便利性、服务响应和综合评价均以客户评价口吻简短填写。每项限一句，使用简洁、客观的措辞，不得写成长篇段落、Markdown 表格或额外标题。未提供的客户名称、日期、联系人、使用事实或具体数据必须留空，不得编造或用通用事实替代。",
        "innovation_staff": "描述企业科技人员结构（学历/职称/年龄分布）、人才引进培养机制、绩效考核制度等",
    }

    # 构建项目描述
    if field == "hitech_product_summary":
        project_desc = f"""高新技术产品（服务）基础信息：
{products_context or '未提供产品服务明细'}"""
    elif field == "ps_statement":
        project_desc = f"""PS类型：{ps_label}
高新技术{ps_label}：{ps_code} {ps_name}
技术领域：{ps_field}
技术来源：{ps_source}
上年度销售收入：{ps_revenue}万元
销售收入年度：{ps_revenue_year or '未提供'}
关联RD项目：{ps_rds}
关联知识产权：{ps_ip}
核心技术：{ps_technologies}
成果名称：{ps_results}
申请书关键技术描述：{ps_tech}
申请书竞争优势描述：{ps_advantage}
申请书知识产权支撑描述：{ps_support}"""
    elif field.startswith("ps_"):
        project_desc = f"""PS类型：{ps_label}
高新技术{ps_label}：{ps_name}
技术领域：{ps_field}
技术来源：{ps_source}
上年度销售收入：{ps_revenue}万元
关联知识产权：{ps_ip}"""
    elif field == "cv_desc":
        cv_rd = context.get("cv_rd", "")
        cv_ip = context.get("cv_ip", "")
        cv_ps = context.get("cv_ps", "")
        cv_result_name = context.get("cv_result_name", "")
        cv_period = context.get("cv_period", "")
        cv_technology = context.get("cv_technology", "")
        project_desc = f"""成果名称：{cv_result_name}
RD项目：{cv_rd}
起止时间：{cv_period}
知识产权：{cv_ip}
PS产品：{cv_ps}
核心技术：{cv_technology}"""
    elif field in {"achievement_test_report", "achievement_user_report"}:
        achievement_no = context.get("achievement_no", "")
        achievement_name = context.get("achievement_name", "")
        achievement_rd = context.get("achievement_rd", "")
        achievement_ps = context.get("achievement_ps", "")
        achievement_period = context.get("achievement_period", "")
        achievement_ip = context.get("achievement_ip", "")
        achievement_technology = context.get("achievement_technology", "")
        project_desc = f"""成果序号：{achievement_no}
成果名称：{achievement_name}
RD项目：{achievement_rd}
知识产权：{achievement_ip}
PS产品：{achievement_ps}
核心技术：{achievement_technology}"""
        if field == "achievement_test_report":
            project_desc = f"{project_desc}\n起止时间：{achievement_period}"
        else:
            project_desc = f"{project_desc}\n客户实际使用时间：未提供，必须留空"
    elif field.startswith("innovation_"):
        tech_field = context.get("tech_field", "")
        staff_total = context.get("staff_total", "")
        tech_staff = context.get("tech_staff", "")
        relation_summary = str(context.get("relation_summary", "")).strip()
        project_desc = f"""技术领域：{tech_field}
职工总数：{staff_total}人
科技人员：{tech_staff}人
RD-IP-PS-成果关联详情表摘要：
{relation_summary or '未提供关系表明细'}"""
    else:
        project_desc = f"""研发项目：{rd_name}
RD序号：{rd_code}
年份：{rd_year}
起止时间：{rd_period}
技术领域：{rd_field}
技术来源：{rd_source}
已有立项目的：{rd_purpose}
已有创新点：{rd_innovation}
已有阶段成果：{rd_result}
研发预算：{rd_budget}
关联技术：{rd_technologies}
成果名称：{rd_result_names}
对应知识产权：{rd_ip}
对应高新技术产品（服务）：{rd_ps}"""

    template_instruction = ""
    if user_template:
        template_instruction = f"""

用户给定模板：
{user_template}

模板改写规则：
1. 必须优先保留用户模板的标题层级、段落顺序、项目符号、冒号、分号和整体结构，不要重组为新的文章结构。
2. 如果模板包含【】占位，主要任务是充分扩写、具体化和专业化【】内的占位内容；可删除【】符号，但不要删除括号外的固定文字。
3. 如果模板没有【】占位，请以模板为版式样例，替换企业名称、项目编号、年份、项目名称、项目时间等已知字段，并按该结构补写项目背景、研发内容及目标、计划进度和验收意见。
4. 如果模板要求填写数据、专利号、客户、资质、荣誉、金额、负责人姓名等，而上下文没有提供，保留为空白、占位表达或泛化表述，不得编造具体事实。
5. 输出结果应看起来像在用户模板上完成填空和专业扩写，而不是重新写一版。
6. 不要输出 Markdown 格式，不要包含 #、|、---、``` 等符号；如需分段，使用中文标题和自然换行。"""

    ps_term_rule = ""
    if field.startswith("ps_") or field in {
        "hitech_product_summary",
        "cv_desc",
        "achievement_test_report",
        "achievement_user_report",
    }:
        ps_term_rule = (
            f'\n7. 当前 PS 类型为“{ps_label}”。涉及该 PS 时必须统一写“本{ps_label}”'
            f'“该{ps_label}”“高新技术{ps_label}”，不得出现“产品（服务）”“产品/服务”'
            f'“产品或服务”等不确定称呼'
        )

    temporal_period = (
        context.get("rd_period")
        or context.get("cv_period")
        or context.get("achievement_period")
        or ""
    )
    temporal = project_temporal_context(temporal_period)
    global_time_rule = f"""
统一时间规则：
1. 本次生成的系统基准日期为 {temporal['as_of_display']}。
2. 有项目周期时必须严格按周期判断状态；当前状态为“{temporal['status_display']}”，项目开始时间为“{temporal['start_display']}”，结束时间为“{temporal['end_display']}”。
3. {temporal['tense_instruction']}
4. 立项、审批、实施、测试、成果形成和验收等事件必须按时间先后排列，不得把未来事件写成已经发生。
5. 未提供的日期必须留空或写“待补充”，不得自行编造年月日；财务申报年度等固定业务年度不因系统当前年份而自动改动。
"""

    prompt = f"""你是高新技术企业认定申报专家。请撰写「{field_labels[field]}」段落。

企业：{company}
上下文信息：
{project_desc}

撰写要点：{field_guides[field]}{template_instruction}{global_time_rule}

要求：
1. 语言专业、简洁，符合高新技术企业申报书风格
2. 结合给定信息，避免空洞套话
3. 目标长度为 {target_words} 字，尽量贴近该字数，允许上下浮动 10%，不得超过 {int(target_words * 1.1)} 字
4. 不得编造具体数据、专利、客户、资质、荣誉或财务信息
5. 段落之间只保留一个换行，不要输出连续空行
6. 直接输出正文，不要加任何前缀说明{ps_term_rule}"""

    try:
        from modules.ai.llm_client import call_llm
    except Exception:
        return jsonify({"success": False, "error": "AI 客户端不可用"}), 500

    if field == "rd_application":
        result = _generate_rd_application_sections(
            call_llm,
            project_desc,
            field_guides[field],
            template_instruction,
            global_time_rule,
            target_words,
        )
    else:
        max_tokens = max(800, min(4000, int(target_words * 2.2)))
        result = call_llm(
            [
                {"role": "system", "content": "你是高新技术企业认定申报材料撰写专家。若用户提供模板，必须以模板为主，只扩写占位符并保留原结构；只输出最终正文。"},
                {"role": "user", "content": prompt},
            ],
            temperature=0.35 if field == "ps_statement" else 0.7,
            max_tokens=max_tokens,
            timeout=45,
            max_attempts=2,
        )

    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "AI 调用失败")}), 500

    content_text = _normalize_ai_text_spacing(result.get("content") or "")
    if content_text.startswith("```"):
        content_text = _normalize_ai_text_spacing("\n".join(content_text.splitlines()[1:-1]))
    if field == "cv_desc":
        content_text = enforce_transformation_wording(content_text, temporal)
    elif field in {
        "result",
        "rd_application",
        "achievement_test_report",
        "achievement_user_report",
        "innovation_ip",
        "innovation_transform",
        "innovation_rd_mgmt",
    }:
        content_text = enforce_temporal_wording(content_text, temporal)
    if field.startswith("ps_") or field in {
        "hitech_product_summary",
        "cv_desc",
        "achievement_test_report",
        "achievement_user_report",
    }:
        content_text = normalize_ps_reference_text(content_text, ps_name, ps_kind)
    actual_words = len("".join(content_text.split()))
    return jsonify({"success": True, "text": content_text, "target_words": target_words, "actual_words": actual_words})
