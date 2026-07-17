import os
from dotenv import load_dotenv

load_dotenv()


def _database_url():
    default_url = "sqlite:////tmp/declare-assistant/declare.db" if os.getenv("VERCEL") else "sqlite:///declare.db"
    url = os.getenv("DATABASE_URL", default_url)
    if url.startswith("postgres://"):
        url = "postgresql://" + url[len("postgres://"):]
    return url


def _runtime_folder(name):
    if os.getenv("VERCEL"):
        return os.path.join("/tmp", "declare-assistant", name)
    return os.path.join(os.path.dirname(__file__), name)


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "declare-assistant-dev-key-change-me")
    SQLALCHEMY_DATABASE_URI = _database_url()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    UPLOAD_FOLDER = os.getenv("UPLOAD_FOLDER", _runtime_folder("uploads"))
    OUTPUT_FOLDER = os.getenv("OUTPUT_FOLDER", _runtime_folder("outputs"))
    MAX_CONTENT_LENGTH = int(os.getenv("MAX_CONTENT_LENGTH", 50 * 1024 * 1024))  # default 50MB
    MAX_FORM_MEMORY_SIZE = int(os.getenv("MAX_FORM_MEMORY_SIZE", MAX_CONTENT_LENGTH))

    # AI / LLM 配置
    LLM_API_BASE = os.getenv("LLM_API_BASE", "https://api.psydo.top/v1")
    LLM_API_KEY = os.getenv("LLM_API_KEY", "")
    LLM_MODEL = os.getenv("LLM_MODEL", "gpt-5.4-mini")
