"""文件上传 + 财务数据提取 路由"""
import os
import json
import re
import uuid
from flask import Blueprint, request, jsonify, session
from flask_login import login_required, current_user
from werkzeug.utils import secure_filename

from config import Config

parser_bp = Blueprint("parser", __name__)

ALLOWED_EXTENSIONS = {'xlsx', 'xls', 'pdf', 'docx'}
UPLOAD_DEBUG_VERSION = "finance-upload-v2"



_ip_cert_store = {}


def _normalize_ai_text_spacing(text):
    text = str(text or "").replace("\r\n", "\n").replace("\r", "\n").strip()
    text = "\n".join(line.rstrip() for line in text.split("\n"))
    return re.sub(r"\n[ \t　]*\n+", "\n", text)


def _ip_store_key() -> str:
    key = session.get("ip_cert_store_key")
    if not key:
        key = uuid.uuid4().hex
        session["ip_cert_store_key"] = key
    return key


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

    from modules.parser.finance_extractor import extract

    try:
        data = extract(filepath)
    except ValueError as e:
        msg = str(e)
        if "格式不支持" in msg:
            code = 415
            tip = "请将文件另存为 .xlsx 后重试"
        elif "空表" in msg or "表头" in msg:
            code = 422
            tip = "请确认财务报表中包含年份、科目和数值列"
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
    session["last_finance_data"] = data

    try:
        os.remove(filepath)
    except OSError:
        pass

    return _jsonify_upload({
        "success": True,
        "data": data,
        "count": len(data),
        "filename": filename,
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

    filename = _safe_upload_filename(file.filename)
    if not filename.lower().endswith(".pdf"):
        return jsonify({"success": False, "error": "专利上传仅支持 PDF 文件"}), 400

    filepath = os.path.join(Config.UPLOAD_FOLDER, filename)
    os.makedirs(Config.UPLOAD_FOLDER, exist_ok=True)
    file.save(filepath)

    from modules.parser.ip_analyzer import extract_text, parse_patent, evaluate_ip

    try:
        text = extract_text(filepath)
        if not text.strip():
            return jsonify({"success": False, "error": "未识别到 PDF 文本，请确认文件不是空白或加密 PDF"}), 422

        parsed = parse_patent(text)
        details = parsed.get("details") or {}
        if not details and not parsed.get("patent_type"):
            return jsonify({"success": False, "error": "未识别到专利证书关键信息，请确认上传的是专利证书 PDF"}), 422

        cert = {"filename": filename, "parsed": parsed}
        certs = _get_ip_certs()
        patent_name = (details.get("name") or "").strip()
        duplicate = False
        if patent_name:
            duplicate = any(
                ((c.get("parsed", {}).get("details", {}).get("name") or "").strip() == patent_name)
                for c in certs
            )
        if not duplicate:
            certs.append(cert)
            _set_ip_certs(certs)

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
        try:
            os.remove(filepath)
        except OSError:
            pass


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
    """删除指定索引的知识产权证书（从 session 中移除）"""
    data = request.get_json() or {}
    idx = data.get("index")
    if idx is None:
        return jsonify({"success": False, "error": "缺少 index 参数"}), 400

    certs = _get_ip_certs()
    try:
        idx = int(idx)
    except (ValueError, TypeError):
        return jsonify({"success": False, "error": "index 必须是整数"}), 400

    if 0 <= idx < len(certs):
        removed = certs.pop(idx)
        _set_ip_certs(certs)
        return jsonify({"success": True, "removed": removed["filename"], "remaining": len(certs)})
    else:
        return jsonify({"success": False, "error": f"索引 {idx} 超出范围 (0-{len(certs)-1})"}), 400


@parser_bp.route("/save_ip_certs/<int:company_id>", methods=["POST"])
@login_required
def save_ip_certs(company_id):
    """将 session 中的 IP 证书持久化到公司记录"""
    from models import Company, db
    company = Company.query.filter_by(id=company_id, user_id=current_user.id).first_or_404()
    certs = _get_ip_certs()
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


@parser_bp.route("/lookup_company", methods=["GET"])
@login_required
def lookup_company():
    """根据企业名称查询成立年份"""
    company_name = request.args.get("name", "").strip()
    if not company_name:
        return jsonify({"success": False, "error": "企业名称为空"}), 400

    from modules.parser.company_lookup import lookup

    result = lookup(company_name)
    return jsonify(result)


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
    ], temperature=0.4, max_tokens=max_tokens)

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
    """AI 辅助撰写 RD 活动描述（调用 DeepSeek）"""
    data = request.get_json() or {}
    field = data.get("field", "")       # purpose / innovation / result
    context = data.get("context", {})   # {rd_name, rd_field, rd_source, company_name}
    try:
        target_words = int(data.get("target_words") or 400)
    except (TypeError, ValueError):
        target_words = 400
    target_words = max(100, min(2000, target_words))

    if field not in ("purpose", "innovation", "result", "ps_tech", "ps_advantage", "ps_support",
                      "innovation_ip", "innovation_transform", "innovation_rd_mgmt", "innovation_staff", "cv_desc"):
        return jsonify({"success": False, "error": "field 不在允许范围内"}), 400

    rd_name = context.get("rd_name", "未命名项目")
    rd_field = context.get("rd_field", "未指定领域")
    rd_source = context.get("rd_source", "自主研发")
    ps_name = context.get("ps_name", "未命名产品")
    ps_field = context.get("ps_field", "未指定领域")
    ps_source = context.get("ps_source", "")
    ps_revenue = context.get("ps_revenue", "")
    ps_ip = context.get("ps_ip", "")
    company = context.get("company_name", "本公司")

    field_labels = {
        "purpose": "目的及组织实施方式",
        "innovation": "核心技术及创新点",
        "result": "取得的阶段性成果",
        "ps_tech": "关键技术及主要技术指标",
        "ps_advantage": "与同类产品的竞争优势",
        "ps_support": "知识产权对产品的支持作用",
        "innovation_ip": "知识产权对企业竞争力的作用",
        "innovation_transform": "科技成果转化情况",
        "innovation_rd_mgmt": "研究开发与技术创新组织管理情况",
        "innovation_staff": "管理与科技人员情况",
        "cv_desc": "成果转化描述",
    }
    field_guides = {
        "purpose": "说明该研发项目要解决什么技术问题、采用什么组织方式（独立研发/产学研合作等）、研发阶段划分",
        "innovation": "从技术原理、实现方法、性能指标等角度描述创新点，突出与现有技术的区别和优势",
        "result": "描述已取得的具体成果，如：已申请专利X项、已形成样机/产品原型、已实现销售收入X万元等",
        "ps_tech": "说明该产品采用的关键技术、技术指标（性能参数、精度、效率等），突出技术先进性和独特性",
        "ps_advantage": "与市场上同类产品对比，从技术指标、成本、性能、服务等角度分析竞争优势",
        "ps_support": "说明该产品拥有的知识产权如何支撑产品的核心技术，阐述IP与产品的对应关系及保护范围",
        "innovation_ip": "分析企业拥有的知识产权类型、数量、覆盖范围，阐述IP如何形成技术壁垒、提升市场竞争力",
        "innovation_transform": "描述近三年科技成果转化数量、转化形式（产品/服务/工艺等）、转化效果及产生的经济效益",
        "innovation_rd_mgmt": "描述企业研发组织架构、管理制度、研发流程、产学研合作情况、创新激励机制等",
        "cv_desc": "按标准格式描述成果转化：科技成果名称→转化方式→转化产品→关键技术及成效→证明材料",
        "innovation_staff": "描述企业科技人员结构（学历/职称/年龄分布）、人才引进培养机制、绩效考核制度等",
    }

    # 构建项目描述
    if field.startswith("ps_"):
        project_desc = f"""高新技术产品（服务）：{ps_name}
技术领域：{ps_field}
技术来源：{ps_source}
上年度销售收入：{ps_revenue}万元
关联知识产权：{ps_ip}"""
    elif field == "cv_desc":
        cv_rd = context.get("cv_rd", "")
        cv_ip = context.get("cv_ip", "")
        cv_ps = context.get("cv_ps", "")
        project_desc = f"""RD项目：{cv_rd}
知识产权：{cv_ip}
PS产品：{cv_ps}"""
    elif field.startswith("innovation_"):
        tech_field = context.get("tech_field", "")
        staff_total = context.get("staff_total", "")
        tech_staff = context.get("tech_staff", "")
        project_desc = f"""技术领域：{tech_field}
职工总数：{staff_total}人
科技人员：{tech_staff}人"""
    else:
        project_desc = f"""研发项目：{rd_name}
技术领域：{rd_field}
技术来源：{rd_source}"""

    prompt = f"""你是高新技术企业认定申报专家。请撰写「{field_labels[field]}」段落。

企业：{company}
上下文信息：
{project_desc}

撰写要点：{field_guides[field]}

要求：
1. 语言专业、简洁，符合高新技术企业申报书风格
2. 结合给定信息，避免空洞套话
3. 目标长度为 {target_words} 字，尽量贴近该字数，允许上下浮动 10%
4. 不得编造具体数据、专利、客户、资质、荣誉或财务信息
5. 段落之间只保留一个换行，不要输出连续空行
6. 直接输出正文，不要加任何前缀说明"""

    try:
        from modules.ai.llm_client import call_llm
    except Exception:
        return jsonify({"success": False, "error": "AI 客户端不可用"}), 500

    max_tokens = max(800, min(4000, int(target_words * 2.2)))
    result = call_llm([
        {"role": "system", "content": "你是高新技术企业认定申报材料撰写专家，输出专业、精准、符合官方申报规范的内容。"},
        {"role": "user", "content": prompt},
    ], temperature=0.7, max_tokens=max_tokens)

    if not result.get("success"):
        return jsonify({"success": False, "error": result.get("error", "AI 调用失败")}), 500

    content_text = _normalize_ai_text_spacing(result.get("content") or "")
    if content_text.startswith("```"):
        content_text = _normalize_ai_text_spacing("\n".join(content_text.splitlines()[1:-1]))
    actual_words = len("".join(content_text.split()))
    return jsonify({"success": True, "text": content_text, "target_words": target_words, "actual_words": actual_words})
