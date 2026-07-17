"""Claude API client for first-pass financial statement extraction."""
import os


def _model_name() -> str:
    return (os.getenv("CLAUDE_MODEL") or os.getenv("ANTHROPIC_MODEL") or "mimo-v2.5").strip()


def _client(timeout: int = 60, max_retries: int = 1):
    import anthropic

    api_key = (os.getenv("CLAUDE_API_KEY") or os.getenv("ANTHROPIC_API_KEY") or "").strip()
    base_url = (os.getenv("CLAUDE_API_BASE") or os.getenv("ANTHROPIC_BASE_URL") or "").strip()
    kwargs = {"timeout": timeout, "max_retries": max(0, min(2, int(max_retries)))}
    if api_key:
        kwargs["api_key"] = api_key
    if base_url:
        kwargs["base_url"] = base_url
    return anthropic.Anthropic(**kwargs)


def _response_text(response) -> str:
    parts = []
    for block in getattr(response, "content", []) or []:
        if getattr(block, "type", "") == "text":
            parts.append(getattr(block, "text", ""))
    return "".join(parts).strip()


def call_claude(
    messages: list,
    system: str = None,
    max_tokens: int = 2048,
    timeout: int = 60,
    output_config: dict = None,
    max_retries: int = 1,
) -> dict:
    """Call Claude through the official Anthropic SDK."""
    try:
        client = _client(timeout=timeout, max_retries=max_retries)
    except Exception as exc:
        return {"success": False, "error": f"Claude 客户端不可用: {str(exc)[:200]}"}

    kwargs = {
        "model": _model_name(),
        "max_tokens": max_tokens,
        "messages": messages,
    }
    if system:
        kwargs["system"] = system
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
