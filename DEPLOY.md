# 部署说明

本项目是 Flask 动态网站，不能直接部署到 GitHub Pages。推荐使用 GitHub 托管代码，Render/Railway/Fly.io 等平台运行 Python 后端。

## 必需环境变量

线上平台需要配置：

```env
SECRET_KEY=一串很长的随机字符串
DATABASE_URL=PostgreSQL 连接串
LLM_API_BASE=https://api.deepseek.com
LLM_API_KEY=你的 LLM API Key
LLM_MODEL=deepseek-chat
```

可选环境变量：

```env
MAX_CONTENT_LENGTH=52428800
UPLOAD_FOLDER=/app/uploads
OUTPUT_FOLDER=/app/outputs
```

## 推荐部署方式：Render + Docker + PostgreSQL

1. 将代码推送到 GitHub。
2. 在 Render 创建 PostgreSQL 数据库，复制 Internal Database URL。
3. 在 Render 创建 Web Service，连接 GitHub 仓库。
4. Runtime 选择 Docker，Render 会读取仓库根目录的 `Dockerfile`。
5. 在 Web Service 的 Environment 中填入上面的必需环境变量。
6. 部署完成后访问 Render 分配的网址。

Dockerfile 已安装 `tesseract-ocr` 和中文 OCR 包，避免图片/PDF 识别功能在线上缺少系统依赖。

## 非 Docker 部署

如果使用普通 Python 环境：

Build Command:

```bash
pip install -r requirements.txt
```

Start Command:

```bash
gunicorn "app:create_app()" --bind 0.0.0.0:$PORT --timeout 180 --workers 2
```

注意：普通 Python 环境通常没有系统命令 `tesseract`，图片 OCR 可能不可用。

## 本地生产方式验证

```bash
source .venv/bin/activate
pip install -r requirements.txt
gunicorn "app:create_app()" --bind 127.0.0.1:8081 --timeout 180 --workers 2
```

打开 `http://127.0.0.1:8081` 验证。

## 不要提交到 GitHub 的内容

`.gitignore` 已忽略：

- `.env` 和本地密钥
- `.venv/`
- `uploads/`
- `outputs/`
- `modules/outputs/`
- SQLite 数据库文件

如果要保留用户上传文件，正式环境应改用对象存储或平台持久磁盘。
