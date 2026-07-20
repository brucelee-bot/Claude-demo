"""Claude API client for first-pass financial statement extraction."""
import os


def _credentials():
    return {
        "api_key": (
            os.getenv("CLAUDE_API_KEY")
            or os.getenv("ANTHROPIC_API_KEY")
            or ""
        ).strip(),
        "base_url": (
            os.getenv("CLAUDE_API_BASE")
            or os.getenv("ANTHROPIC_BASE_URL")
            or ""
        ).strip(),
    }


def _model_name() -> str:
    return (os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL") or "mimo-v2.5").strip()


def _client(timeout: int = 60, max_retries: int = 1):
    import anthropic

    config = _credentials()
    kwargs = {"timeout": timeout, "max_retries": max(0, min(2, int(max_retries)))}
    if config["api_key"]:
        kwargs["api_key"] = config["api_key"]
    if config["base_url"]:
        kwargs["base_url"] = config["base_url"]
    return anthropic.Anthropic(**kwargs)


def _response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()


def _messages_url(base_url):
    base_url = str(base_url or "").rstrip("/")
    if base_url.endswith("/v1/messages"):
        return base_url
    if base_url.endswith("/v1"):
        return f"{base_url}/messages"
    return f"{base_url}/v1/messages"


def _call_claude_http(messages, system, max_tokens, timeout, temperature):
    config = _credentials()
    if not config["base_url"] or not config["api_key"]:
        return {"success": False, "error": "Claude 未配置 (base_url 或 api_key 缺失)"}

    payload = {
        "model": _model_name(),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        payload["system"] = system
    if temperature is not None:
        payload["temperature"] = temperature

    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        "Authorization": f"Bearer {config['api_key']}",
        "x-api-key": config["api_key"],
        "anthropic-version": "2023-06-01",
        "Connection": "close",
    }

    try:
        import requests

        session = requests.Session()
        session.trust_env = False
        try:
            response = session.post(
                _messages_url(config["base_url"]),
                json=payload,
                headers=headers,
                timeout=(min(10, timeout), timeout),
                verify=False,
            )
            raw = response.text or ""
            if response.status_code >= 400:
                return {
                    "success": False,
                    "error": f"HTTP {response.status_code}: {raw[:300]}",
                }
            data = response.json()
        finally:
            session.close()
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}

    content = "".join(
        str(block.get("text") or "")
        for block in data.get("content", [])
        if isinstance(block, dict) and block.get("type") == "text"
    ).strip()
    if not content:
        return {"success": False, "error": "Claude 返回内容为空"}
    return {
        "success": True,
        "content": content,
        "model": data.get("model", _model_name()),
        "usage": data.get("usage"),
    }


def call_claude(
    messages: list,
    system: str = None,
    max_tokens: int = 2048,
    timeout: int = 60,
    temperature: float = None,
    output_config: dict = None,
    max_retries: int = 1,
) -> dict:
    """Call Claude through the official Anthropic SDK."""
    try:
        client = _client(timeout=timeout, max_retries=max_retries)
    except ModuleNotFoundError:
        return _call_claude_http(
            messages,
            system,
            max_tokens,
            timeout,
            temperature,
        )
    except Exception as exc:
        return {"success": False, "error": f"Claude 客户端不可用: {str(exc)[:200]}"}

    kwargs = {
        "model": _model_name(),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
    if temperature is not None:
        kwargs["temperature"] = temperature
    if output_config:
        kwargs["output_config"] = output_config

    try:
        response = client.messages.create(**kwargs)
    except TypeError:
        kwargs.pop("output_config", None)
        try:
            response = client.messages.create(**kwargs)
        except Exception as exc:
            return {"success": False, "error": str(exc)[:300]}
    except Exception as exc:
        return {"success": False, "error": str(exc)[:300]}

    if getattr(response, "stop_reason", None) == "refusal":
        return {"success": False, "error": "Claude 拒绝处理该财务识别请求"}

    return {
        "success": True,
        "content": _response_text(response),
        "model": getattr(response, "model", _model_name()),
        "usage": getattr(response, "usage", None),
    }
