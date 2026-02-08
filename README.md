# RovoDev — AI Proxy & Dashboard

An Anthropic-compatible API proxy server with a Flask web dashboard for managing
conversations, system prompts, settings, and files.

## Architecture

```
┌──────────────┐     ┌──────────────────┐     ┌──────────────┐     ┌──────────────┐
│  Client      │────▶│  rovodev_server  │────▶│  mitmproxy   │────▶│  Atlassian   │
│  (Cline/Amp) │     │  (FastAPI:8000)  │     │  intercept.py│     │  Bedrock API │
└──────────────┘     └──────────────────┘     │  (:8080)     │     └──────────────┘
                                              └──────────────┘
┌──────────────┐
│  Flask App   │──── SQLAlchemy ──── SQLite (local) / PostgreSQL (Azure)
│  (:5000)     │──── Azure Blob Storage / Local filesystem
└──────────────┘
```

### Components

| File | Description |
|------|-------------|
| `rovodev_server.py` | FastAPI server — Anthropic Messages API on port 8000 |
| `intercept.py` | mitmproxy script — intercepts/modifies CLI traffic |
| `amp_intercept.py` | AMP-specific interceptor variant |
| `rovodev_api.py` | Direct API client for Atlassian/Bedrock |
| `rovodev_opus.py` | CLI wrapper for Opus 4.6 |
| `flask_app/` | Flask web dashboard (see below) |

## Flask Dashboard

A web application for managing the proxy configuration.

### Requirements Met

- **Flask framework** — `flask_app/app.py`
- **Azure AppService** — `startup.sh` + GitHub Actions deploy
- **Azure database via env vars** — `DATABASE_URL` → PostgreSQL (SQLite fallback)
- **Full CRUD** — Conversations, Messages, System Prompts, Settings, Files
- **Azure Blob Storage** — `AZURE_STORAGE_CONNECTION_STRING` (local fallback)
- **GitHub auto-deploy** — `.github/workflows/deploy.yml`

### Run Locally

```bash
cd flask_app
pip install -r requirements.txt
python app.py
# Open http://localhost:5000
```

No environment variables needed locally — uses SQLite and local filesystem.

### Environment Variables (Azure)

| Variable | Description |
|----------|-------------|
| `DATABASE_URL` | PostgreSQL connection string |
| `AZURE_STORAGE_CONNECTION_STRING` | Azure Blob Storage connection |
| `AZURE_STORAGE_CONTAINER` | Blob container name (default: `rovodev-files`) |
| `SECRET_KEY` | Flask session secret key |

### Deploy to Azure

1. Create Azure App Service (Python 3.11)
2. Create Azure PostgreSQL Flexible Server
3. Set environment variables in App Service Configuration
4. Add `AZURE_WEBAPP_PUBLISH_PROFILE` secret to GitHub repo
5. Set `AZURE_WEBAPP_NAME` variable in GitHub repo
6. Push to `main` — auto-deploys via GitHub Actions

## Proxy Server

### Prerequisites

```bash
# 1. Install and start mitmproxy
mitmdump -s intercept.py -p 8080

# 2. Start the API server
python rovodev_server.py
```

### Usage

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

## Tests

```bash
python -m pytest test_*.py -v
```
