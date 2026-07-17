import base64
import hashlib
import hmac
import json
import mimetypes
import os
import ssl
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import quote, urlencode
from urllib.request import Request, urlopen

import certifi


BLOB_API_URL = "https://blob.vercel-storage.com"
BLOB_PREFIX = "declare-assistant"
SSL_CONTEXT = ssl.create_default_context(cafile=certifi.where())


def blob_enabled():
    return bool(os.getenv("BLOB_READ_WRITE_TOKEN"))


def _token():
    return os.getenv("BLOB_READ_WRITE_TOKEN", "")


def _blob_path(relative_path):
    normalized = str(relative_path or "").replace("\\", "/").lstrip("/")
    return f"{BLOB_PREFIX}/{normalized}"


def blob_path(relative_path):
    return _blob_path(relative_path)


def _request_json(request, timeout=60):
    with urlopen(request, timeout=timeout, context=SSL_CONTEXT) as response:
        return json.loads(response.read().decode("utf-8"))


def _matching_blob(relative_path):
    if not blob_enabled() or not relative_path:
        return None
    pathname = _blob_path(relative_path)
    query = urlencode({"prefix": pathname, "limit": 10})
    request = Request(
        f"{BLOB_API_URL}?{query}",
        headers={
            "Authorization": f"Bearer {_token()}",
            "x-api-version": "10",
        },
    )
    try:
        payload = _request_json(request)
    except (HTTPError, URLError, TimeoutError, ValueError):
        return None
    return next(
        (item for item in payload.get("blobs", []) if item.get("pathname") == pathname),
        None,
    )


def blob_metadata(relative_path):
    return _matching_blob(relative_path)


def generate_client_upload_token(relative_path, valid_for_seconds=3600):
    """Create a short-lived Vercel Blob client token scoped to one pathname."""
    read_write_token = _token()
    token_parts = read_write_token.split("_")
    store_id = token_parts[3] if len(token_parts) > 3 else ""
    if not store_id:
        raise ValueError("BLOB_READ_WRITE_TOKEN 无效")

    pathname = _blob_path(relative_path)
    payload = {
        "pathname": pathname,
        "addRandomSuffix": False,
        "allowOverwrite": False,
        "validUntil": int((time.time() + valid_for_seconds) * 1000),
    }
    encoded_payload = base64.b64encode(
        json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    ).decode("ascii")
    signature = hmac.new(
        read_write_token.encode("utf-8"),
        encoded_payload.encode("ascii"),
        hashlib.sha256,
    ).hexdigest()
    secured_payload = base64.b64encode(
        f"{signature}.{encoded_payload}".encode("ascii")
    ).decode("ascii")
    return {
        "client_token": f"vercel_blob_client_{store_id}_{secured_payload}",
        "pathname": pathname,
        "store_id": store_id,
    }


def persist_file(local_path, relative_path):
    if not blob_enabled() or not os.path.isfile(local_path):
        return None
    pathname = _blob_path(relative_path)
    content_type = mimetypes.guess_type(local_path)[0] or "application/octet-stream"
    with open(local_path, "rb") as file_obj:
        request = Request(
            f"{BLOB_API_URL}/?pathname={quote(pathname, safe='/')}",
            data=file_obj.read(),
            method="PUT",
            headers={
                "Authorization": f"Bearer {_token()}",
                "x-api-version": "10",
                "x-vercel-blob-access": "private",
                "x-content-type": content_type,
                "x-allow-overwrite": "1",
            },
        )
    return _request_json(request, timeout=120)


def ensure_local_file(local_path, relative_path):
    if os.path.exists(local_path) or not blob_enabled():
        return local_path
    blob = _matching_blob(relative_path)
    if not blob:
        return local_path
    Path(local_path).parent.mkdir(parents=True, exist_ok=True)
    request = Request(
        blob["url"],
        headers={"Authorization": f"Bearer {_token()}"},
    )
    try:
        with urlopen(request, timeout=120, context=SSL_CONTEXT) as response:
            Path(local_path).write_bytes(response.read())
    except (HTTPError, URLError, TimeoutError, OSError):
        pass
    return local_path


def delete_file(relative_path):
    blob = _matching_blob(relative_path)
    if not blob:
        return
    request = Request(
        f"{BLOB_API_URL}/delete",
        data=json.dumps({"urls": [blob["url"]]}).encode("utf-8"),
        method="POST",
        headers={
            "Authorization": f"Bearer {_token()}",
            "Content-Type": "application/json",
            "x-api-version": "10",
        },
    )
    try:
        _request_json(request)
    except (HTTPError, URLError, TimeoutError, ValueError):
        pass
