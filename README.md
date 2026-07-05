# Declare Assistant

Flask-based declaration assistant for scoring, material parsing, AI-assisted writing, and Word/PDF application document generation.

## Local Development

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
python app.py
```

Open `http://127.0.0.1:8081`.

## Production Start Command

```bash
gunicorn "app:create_app()" --bind 0.0.0.0:$PORT --timeout 180 --workers 2
```

## Environment Variables

```env
SECRET_KEY=replace-with-a-long-random-secret
DATABASE_URL=postgresql://user:password@host:5432/dbname
LLM_API_BASE=https://api.deepseek.com
LLM_API_KEY=replace-with-your-llm-api-key
LLM_MODEL=deepseek-chat
```

## Deployment

This is a dynamic Flask application. Do not deploy it to GitHub Pages. Use Render, Railway, Fly.io, or a server that supports Python web services.

Recommended setup:

- GitHub for source code
- Render Web Service using Docker
- Render PostgreSQL for database
- Environment variables configured in the hosting dashboard

See [DEPLOY.md](DEPLOY.md) for detailed deployment steps and environment notes.
