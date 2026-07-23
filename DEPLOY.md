# 部署说明

本项目是 Flask 动态网站，不能部署到 GitHub Pages。仓库已经支持 Vercel
Python Functions，也保留了 Render/Docker 部署方式。

## 必需环境变量

线上平台需要配置：

```env
SECRET_KEY=一串很长的随机字符串
DATABASE_URL=PostgreSQL 连接串
LLM_API_BASE=https://api.psydo.top/v1
LLM_API_KEY=你的 LLM API Key
LLM_MODEL=gpt-5.6-sol
BLOB_READ_WRITE_TOKEN=Vercel Blob 读写令牌
MAX_CONTENT_LENGTH=4194304
MAX_FORM_MEMORY_SIZE=4194304
```

可选环境变量：

```env
MAX_CONTENT_LENGTH=52428800
UPLOAD_FOLDER=/app/uploads
OUTPUT_FOLDER=/app/outputs
```

## Vercel 部署

1. 将代码推送到 GitHub。
2. 在 Vercel 导入仓库，Framework 选择 Flask 或 Other。
3. 连接一个 PostgreSQL 数据库，并确保项目中存在 `DATABASE_URL`。
4. 创建 Vercel Blob Store，并确保项目中存在 `BLOB_READ_WRITE_TOKEN`。
5. 配置 `SECRET_KEY` 和 LLM 环境变量。
6. 执行 `vercel --prod`。

Vercel 环境中的临时上传和导出目录位于 `/tmp/declare-assistant`。该目录不是
持久磁盘，长期保留的上传材料必须写入 Blob。Vercel Function 的请求体大小
有限，当前应用限制单次请求为 4 MB；大文件应改为浏览器直传 Blob。OCR 依赖
的 Tesseract 系统命令也不保证
在 Vercel Python Function 中可用。

## Render + Docker + PostgreSQL

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
