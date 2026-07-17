"""
LLM 客户端 — 通过 OpenAI 兼容 API 调用配置的模型
"""
import json, os, time, ssl, re, socket, threading
from urllib.request import Request, urlopen
from urllib.error import URLError, HTTPError


# SSL context for custom endpoints
_SSL_CONTEXT = ssl.create_default_context()
_SSL_CONTEXT.check_hostname = False
_SSL_CONTEXT.verify_mode = ssl.CERT_NONE


def _project_root():
    return os.path.abspath(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", ".."))


def _load_dotenv_file(project_root):
    try:
        from dotenv import load_dotenv
        load_dotenv(os.path.join(project_root, ".env"))
    except Exception:
        pass


def _env_config():
    return {
        "base_url": os.getenv("LLM_API_BASE", "").strip(),
        "api_key": os.getenv("LLM_API_KEY", "").strip(),
        "model": os.getenv("LLM_MODEL", "").strip(),
    }


# 优先从 .env 读取配置，缺失时回退到 .hermes/config.yaml
def _load_config():
    project_root = _project_root()
    _load_dotenv_file(project_root)

    env_cfg = _env_config()
    if env_cfg["base_url"] and env_cfg["api_key"]:
        return {
            "base_url": env_cfg["base_url"],
            "api_key": env_cfg["api_key"],
            "model": env_cfg["model"] or "gpt-5.6-sol",
        }

    try:
        import yaml
        config_path = os.path.join(project_root, ".hermes", "config.yaml")
        config_path = os.path.abspath(config_path)
        if os.path.exists(config_path):
            with open(config_path) as f:
                cfg = yaml.safe_load(f) or {}
            model_cfg = cfg.get("model", {})
            return {
                "base_url": model_cfg.get("base_url", "https://api.psydo.top/v1"),
                "api_key": model_cfg.get("api_key", ""),
                "model": model_cfg.get("default", "gpt-5.6-sol"),
            }
    except Exception:
        pass
    return {
        "base_url": env_cfg["base_url"],
        "api_key": env_cfg["api_key"],
        "model": env_cfg["model"] or "gpt-5.6-sol",
    }


_CONFIG = _load_config()
_WARMUP_STARTED = False
_WARMUP_LOCK = threading.Lock()


def _is_retryable_error_text(text):
    text = str(text or "").lower()
    return any(marker in text for marker in [
        "unexpected_eof_while_reading",
        "eof occurred in violation of protocol",
        "connection reset",
        "connection aborted",
        "remote end closed connection",
        "max retries exceeded",
        "ssl",
        "temporarily unavailable",
        "timed out",
        "timeout",
    ])


def _retry_delay(attempt):
    return min(2.0, 0.6 * (2 ** attempt))


def _read_http_error(e):
    try:
        return e.read().decode("utf-8", errors="ignore") if e.fp else str(e)
    except Exception:
        return str(e)


def _is_retryable_http_status(status_code):
    return status_code == 429 or 500 <= status_code < 600


def _is_retryable_http_error(status_code, error_text):
    if _is_retryable_http_status(status_code):
        return True
    text = str(error_text or "").lower()
    return status_code in (400, 408) and "upstream request failed" in text


def _request_timeout(timeout):
    timeout = max(1, int(timeout or 60))
    connect_timeout = min(10, max(3, timeout // 4))
    return (connect_timeout, timeout)


def _model_candidates(primary_model):
    configured = os.getenv("LLM_FALLBACK_MODELS", "").strip()
    if configured:
        fallbacks = [item.strip() for item in configured.split(",") if item.strip()]
    elif "api.psydo.top" in str(_CONFIG.get("base_url") or ""):
        fallbacks = ["gpt-5.4-mini", "gpt-5.4"]
    else:
        fallbacks = []

    candidates = []
    for candidate in [primary_model, *fallbacks]:
        if candidate and candidate not in candidates:
            candidates.append(candidate)
    return candidates or ["gpt-5.6-sol"]


def _extract_content_from_chunked_response(raw: str) -> dict:
    text_parts = []
    usage = {}
    lines = raw.splitlines()
    for line in lines:
        line = line.strip()
        if not line.startswith("data:"):
            continue
        payload = line[5:].strip()
        if not payload or payload == "[DONE]":
            continue
        try:
            obj = json.loads(payload)
        except Exception:
            continue
        if isinstance(obj, dict):
            if obj.get("usage"):
                usage = obj.get("usage") or usage
            choices = obj.get("choices") or []
            for choice in choices:
                delta = choice.get("delta") or {}
                content = delta.get("content")
                if content:
                    text_parts.append(content)
                msg = choice.get("message") or {}
                if msg.get("content") and not text_parts:
                    text_parts.append(msg.get("content"))
    if not usage:
        usage_match = re.search(r'"usage"\s*:\s*(\{.*?\})\s*[,}]', raw, re.S)
        if usage_match:
            try:
                usage = json.loads(usage_match.group(1))
            except Exception:
                usage = {}
    return {"content": "".join(text_parts).strip(), "usage": usage}


def _parse_llm_response(raw):
    try:
        data = json.loads(raw)
        return {
            "success": True,
            "content": data["choices"][0]["message"]["content"],
            "usage": data.get("usage", {}),
        }
    except json.JSONDecodeError:
        chunked = _extract_content_from_chunked_response(raw)
        if chunked.get("content"):
            return {
                "success": True,
                "content": chunked["content"],
                "usage": chunked.get("usage", {}),
            }
        usage = chunked.get("usage") or {}
        if usage.get("completion_tokens") == 0:
            return {"success": False, "error": "上游模型没有生成正文，请检查 LLM 模型配置或稍后重试"}
        return {"success": False, "error": f"上游返回非 JSON 内容: {raw[:300]}"}


def call_llm(
    messages: list,
    model: str = None,
    temperature: float = 0.3,
    max_tokens: int = 2048,
    timeout: int = 60,
    max_attempts: int = 2,
) -> dict:
    """
    调用 LLM
    
    Args:
        messages: [{"role": "system"|"user"|"assistant", "content": "..."}, ...]
        model: 模型名，默认用 config 中的
        temperature: 温度
        max_tokens: 最大输出
        timeout: 超时秒数
        max_attempts: 最多请求次数，包含首次请求
    
    Returns:
        {"success": True, "content": "...", "usage": {...}}
        或 {"success": False, "error": "..."}
    """
    base_url = _CONFIG.get("base_url", "").rstrip("/")
    api_key = _CONFIG.get("api_key", "")
    model = model or _CONFIG.get("model", "gpt-5.6-sol")

    if not base_url or not api_key:
        return {"success": False, "error": "LLM 未配置 (base_url 或 api_key 缺失)"}

    url = f"{base_url}/chat/completions"
    body = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "top_p": 0.95,
        "stream": False,
    }

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {api_key}",
        "api-key": api_key,
        "Connection": "close",
    }

    def request_with_requests():
        import requests
        session = requests.Session()
        session.trust_env = False
        try:
            payload = dict(body)
            resp = session.post(url, json=payload, headers=headers, timeout=_request_timeout(timeout), verify=False)
            raw = resp.text or ""
            if resp.status_code >= 400:
                raise RuntimeError(f"HTTP {resp.status_code}: {raw[:300]}")
            return raw
        finally:
            session.close()

    def request_with_urllib():
        payload = dict(body)
        req = Request(url, data=json.dumps(payload).encode("utf-8"))
        for key, value in headers.items():
            req.add_header(key, value)
        resp = urlopen(req, timeout=timeout, context=_SSL_CONTEXT)
        return resp.read().decode("utf-8", errors="ignore")

    last_error = ""
    try:
        max_attempts = max(1, min(4, int(max_attempts)))
    except (TypeError, ValueError):
        max_attempts = 2
    model_candidates = _model_candidates(model)
    for attempt in range(max_attempts):
        body["model"] = model_candidates[min(attempt, len(model_candidates) - 1)]
        try:
            try:
                raw = request_with_requests()
            except ImportError:
                raw = request_with_urllib()
            return _parse_llm_response(raw)
        except HTTPError as e:
            err_body = _read_http_error(e)
            last_error = f"HTTP {e.code}: {err_body[:300]}"
            if not _is_retryable_http_error(e.code, last_error) or attempt == max_attempts - 1:
                return {"success": False, "error": last_error}
        except RuntimeError as e:
            last_error = str(e)[:300]
            status_match = re.match(r"HTTP\s+(\d+)", last_error)
            status_code = int(status_match.group(1)) if status_match else 0
            if (
                status_code
                and not _is_retryable_http_error(status_code, last_error)
            ) or attempt == max_attempts - 1:
                return {"success": False, "error": last_error}
        except (URLError, ssl.SSLError, socket.timeout, TimeoutError, ConnectionResetError) as e:
            reason = getattr(e, "reason", e)
            last_error = f"连接失败: {str(reason)[:200]}"
            if not _is_retryable_error_text(reason) or attempt == max_attempts - 1:
                return {"success": False, "error": last_error}
        except Exception as e:
            try:
                import requests
                if isinstance(e, (requests.exceptions.ConnectionError, requests.exceptions.Timeout, requests.exceptions.SSLError)):
                    last_error = f"连接失败: {str(e)[:200]}"
                    if attempt == max_attempts - 1:
                        return {"success": False, "error": last_error}
                    time.sleep(_retry_delay(attempt))
                    continue
            except Exception:
                pass
            last_error = str(e)[:300]
            if not _is_retryable_error_text(last_error) or attempt == max_attempts - 1:
                return {"success": False, "error": last_error}

        time.sleep(_retry_delay(attempt))

    return {"success": False, "error": last_error or "AI 调用失败"}


def warmup_llm_async():
    """后台预热 LLM 连接，降低用户第一次点击 AI 撰写时的冷启动失败率。"""
    global _WARMUP_STARTED
    with _WARMUP_LOCK:
        if _WARMUP_STARTED:
            return
        _WARMUP_STARTED = True

    def _warmup():
        try:
            call_llm(
                [{"role": "user", "content": "请回复：OK"}],
                temperature=0,
                max_tokens=300,
                timeout=20,
                max_attempts=1,
            )
        except Exception:
            pass

    thread = threading.Thread(target=_warmup, name="llm-warmup", daemon=True)
    thread.start()


def analyze_scoring_result(result: dict, data: dict = None) -> dict:
    """
    用 LLM 分析评分结果，生成定性评估
    
    Args:
        result: 评分结果 {"total_score": 86, "pass_score": 71, "rule_type": "高新技术", "breakdown": [...]}
        data: 原始输入数据
    
    Returns:
        {"overall": "...", "strengths": [...], "weaknesses": [...], 
         "recommendations": [...], "priority": "...", "risk_level": "低/中/高"}
    """
    # 构建 prompt
    breakdown_text = ""
    for cat in result.get("breakdown", []):
        cat_rate = cat["score"] / cat["max_score"] if cat["max_score"] > 0 else 0
        breakdown_text += f"\n【{cat['name']}】{cat['score']}/{cat['max_score']}分 (得分率{cat_rate:.0%})"
        for sub in cat.get("sub_items", []):
            breakdown_text += f"\n  - {sub['name']}: {sub['score']}/{sub['max_score']}"

    rule_type = result.get("rule_type", "高新技术")
    total = result.get("total_score", 0)
    pass_line = result.get("pass_score", 71)
    passed = result.get("passed", False)
    
    user_prompt = f"""你是一位专业的政府项目申报顾问，精通{rule_type}企业认定评审标准。

以下是一家企业的评分结果，请进行定性分析：

总分：{total}/100，达标线：{pass_line}，状态：{'✅达标' if passed else '❌未达标'}

分项得分：
{breakdown_text}

请输出 JSON 格式（不要用 markdown 代码块包裹，只输出纯 JSON）：

{{
    "overall": "综合评估（100-200字，包含总分、得分率、达标判断、整体评价）",
    "strengths": ["优势1（包含得分率数据）", "优势2", ...],
    "weaknesses": ["短板1（包含得分率数据和距满分差距）", ...],
    "recommendations": ["具体改进建议1（针对短板，有可操作步骤）", ...],
    "priority": "行动路线（含emoji，一条清晰的优先级建议）",
    "risk_level": "低/中/高"
}}"""

    messages = [
        {"role": "system", "content": "你是一个政府项目申报AI顾问，输出精准、专业、有数据支撑的分析。只输出JSON，不要其他内容。"},
        {"role": "user", "content": user_prompt},
    ]

    result_llm = call_llm(
        messages,
        temperature=0.3,
        max_tokens=2048,
        timeout=35,
        max_attempts=1,
    )

    if not result_llm.get("success"):
        return None  # 调用失败，由调用方回退到规则引擎

    try:
        content = result_llm["content"].strip()
        # 处理可能的 markdown 代码块
        if content.startswith("```"):
            lines = content.split("\n")
            content = "\n".join(lines[1:-1])
        analysis = json.loads(content)
        # 验证必要字段
        for key in ["overall", "strengths", "weaknesses", "recommendations", "priority", "risk_level"]:
            if key not in analysis:
                analysis[key] = "" if key == "overall" else []
        return analysis
    except json.JSONDecodeError:
        return None  # JSON 解析失败
