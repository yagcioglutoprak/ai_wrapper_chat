# RovoDev — AI-Powered Proxy & Web Dashboard

> **University Project** — Cloud Application Development  
> A full-stack Python application demonstrating cloud deployment, database management, file storage, and API proxy design using Microsoft Azure services.

---

## Table of Contents

- [Project Overview](#project-overview)
- [Architecture](#architecture)
- [Technology Stack](#technology-stack)
- [Project Structure](#project-structure)
- [Flask Web Dashboard](#flask-web-dashboard)
  - [Features](#features)
  - [Database Models](#database-models)
  - [CRUD Operations](#crud-operations)
  - [File Storage](#file-storage)
  - [REST API Endpoints](#rest-api-endpoints)
- [Proxy Server (FastAPI)](#proxy-server-fastapi)
  - [How It Works](#how-it-works)
  - [Rate Limiting](#rate-limiting)
  - [SSE Streaming](#sse-streaming)
- [Mitmproxy Interceptors](#mitmproxy-interceptors)
  - [intercept.py — Core Request Modifier](#interceptpy--core-request-modifier)
  - [amp_intercept.py — Amp CLI Bridge](#amp_interceptpy--amp-cli-bridge)
- [Setup & Installation](#setup--installation)
  - [Local Development](#local-development)
  - [Running All Services](#running-all-services)
- [Environment Variables](#environment-variables)
- [Deployment to Azure](#deployment-to-azure)
  - [Azure App Service](#azure-app-service)
  - [Azure PostgreSQL](#azure-postgresql)
  - [Azure Blob Storage](#azure-blob-storage)
  - [CI/CD with GitHub Actions](#cicd-with-github-actions)
- [Testing](#testing)
- [API Usage Examples](#api-usage-examples)

---

## Project Overview

RovoDev is a multi-component system that:

1. **Flask Web Dashboard** — A CRUD web application for managing AI conversations, system prompts, application settings, and file uploads. Uses SQLAlchemy ORM with SQLite (local) / PostgreSQL (Azure) and Azure Blob Storage for file management.
2. **FastAPI Proxy Server** — An Anthropic Messages API-compatible server that accepts requests and routes them through the Atlassian Bedrock AI gateway, handling authentication, streaming, rate limiting, and response capture.
3. **Mitmproxy Interceptors** — Man-in-the-middle proxy scripts that intercept, modify, and relay HTTP traffic between AI clients and upstream APIs.

---

## Architecture

```
┌──────────────────┐     ┌───────────────────┐     ┌──────────────────┐     ┌──────────────────┐
│  Client           │────▶│  rovodev_server    │────▶│  mitmproxy       │────▶│  Atlassian       │
│  (Cline / Amp)    │     │  (FastAPI :8000)   │     │  intercept.py    │     │  Bedrock API     │
└──────────────────┘     └───────────────────┘     │  (:8080)         │     └──────────────────┘
                                                    └──────────────────┘
┌──────────────────┐                                ┌──────────────────┐
│  Amp CLI          │────▶ amp_intercept.py ────────▶│  rovodev_server  │
│                   │     (mitmproxy :8899)          │  (FastAPI :8000) │
└──────────────────┘                                └──────────────────┘

┌──────────────────┐
│  Flask Dashboard  │──── SQLAlchemy ORM ──── SQLite (local) / PostgreSQL (Azure)
│  (:5001)          │──── Azure Blob Storage / Local filesystem
└──────────────────┘
```

### Request Flow

1. A client sends an Anthropic-compatible API request to `rovodev_server.py` on port 8000.
2. The server validates the request, normalizes messages, truncates long conversations, and writes a relay queue file.
3. The server spawns the Atlassian CLI (`acli rovodev run`), routing it through `intercept.py` (mitmproxy on port 8080).
4. `intercept.py` detects the queued relay file, swaps in the custom request body, injects the system prompt, merges tools, and forwards the request to the Atlassian Bedrock API.
5. The raw SSE response is captured and streamed back to the client with proper SSE event formatting.
6. Conversations are automatically logged to the Flask dashboard database.

---

## Technology Stack

| Layer | Technology |
|-------|-----------|
| **Web Framework** | Flask 3.1, Jinja2 templates |
| **API Server** | FastAPI, Uvicorn, Pydantic |
| **ORM** | Flask-SQLAlchemy / SQLAlchemy 2.0 |
| **Database** | SQLite (local dev) / Azure PostgreSQL (production) |
| **File Storage** | Local filesystem (dev) / Azure Blob Storage (production) |
| **Proxy** | mitmproxy (mitmdump) |
| **Deployment** | Azure App Service, Gunicorn |
| **CI/CD** | GitHub Actions |
| **Language** | Python 3.11 |

---

## Project Structure

```
.rovodev/
├── flask_app/                  # Flask web dashboard
│   ├── app.py                  # Main Flask application (routes, CRUD, API)
│   ├── models.py               # SQLAlchemy database models
│   ├── config.py               # Configuration from environment variables
│   ├── storage.py              # File storage abstraction (Azure Blob / local)
│   ├── requirements.txt        # Python dependencies
│   ├── startup.sh              # Azure App Service startup command
│   ├── .env.example            # Example environment variables
│   ├── templates/              # Jinja2 HTML templates
│   │   ├── base.html
│   │   ├── index.html          # Dashboard home
│   │   ├── conversations.html  # Conversation list
│   │   ├── conversation_detail.html
│   │   ├── conversation_form.html
│   │   ├── prompts.html        # System prompts management
│   │   ├── prompt_form.html
│   │   ├── settings.html       # App settings
│   │   └── files.html          # File upload/download
│   ├── static/
│   │   └── style.css           # Custom styles
│   └── uploads/                # Local file storage directory
├── rovodev_server.py           # FastAPI API proxy server (:8000)
├── intercept.py                # mitmproxy script — core request interceptor (:8080)
├── amp_intercept.py            # mitmproxy script — Amp CLI bridge (:8899)
├── start.sh                    # Service management script (start/stop/restart/status)
├── config.yml                  # Agent configuration
├── .github/
│   └── workflows/
│       └── deploy.yml          # GitHub Actions → Azure deployment
├── queue/                      # IPC: relay queue files between server and proxy
├── raw_responses/              # Captured API responses
├── logs/                       # Application logs
└── test_*.py                   # Test files
```

---

## Flask Web Dashboard

### Features

- **Dashboard Home** — Overview statistics (conversation count, message count, prompt count, file count) and recent conversations.
- **Conversations** — Full CRUD: create, view, edit, delete conversations. Each conversation tracks title, model, status, and timestamps.
- **Messages** — Nested CRUD within conversations: add user/assistant messages, view message history, delete individual messages.
- **System Prompts** — Manage multiple system prompts with active/inactive toggle. Only one prompt can be active at a time.
- **Settings** — Key-value configuration store with descriptions. Default settings seeded on first run (model, max_tokens, proxy_url, rate_limit_cooldown).
- **File Management** — Upload, download, and delete files. Uses Azure Blob Storage in production, local filesystem in development.
- **Health Check** — `/health` endpoint reports database connectivity and storage backend status.
- **REST API** — JSON endpoints for programmatic access to conversations, messages, prompts, and settings.

### Database Models

Defined in `flask_app/models.py` using SQLAlchemy ORM:

| Model | Table | Fields |
|-------|-------|--------|
| **Conversation** | `conversations` | id, title, model, status, created_at, updated_at |
| **Message** | `messages` | id, conversation_id (FK), role, content, token_count, created_at |
| **SystemPrompt** | `system_prompts` | id, name (unique), content, is_active, created_at, updated_at |
| **Setting** | `settings` | id, key (unique), value, description, updated_at |
| **FileUpload** | `file_uploads` | id, filename, blob_url, content_type, size_bytes, uploaded_at |

**Relationships:** Conversation → Messages (one-to-many, cascade delete)

### CRUD Operations

| Entity | Create | Read | Update | Delete |
|--------|--------|------|--------|--------|
| Conversations | `POST /conversations/new` | `GET /conversations`, `GET /conversations/<id>` | `POST /conversations/<id>/edit` | `POST /conversations/<id>/delete` |
| Messages | `POST /conversations/<id>/messages` | Displayed in conversation detail | — | `POST /messages/<id>/delete` |
| System Prompts | `POST /prompts/new` | `GET /prompts` | `POST /prompts/<id>/edit` | `POST /prompts/<id>/delete` |
| Settings | `POST /settings/new` | `GET /settings` | `POST /settings` (bulk) | `POST /settings/<id>/delete` |
| Files | `POST /files/upload` | `GET /files`, `GET /files/<id>/download` | — | `POST /files/<id>/delete` |

### File Storage

The `storage.py` module implements a **storage abstraction layer** with two backends:

- **`LocalStorage`** — Saves files to `flask_app/uploads/` with UUID-prefixed filenames. Used in local development.
- **`AzureBlobStorage`** — Uploads files to Azure Blob Storage containers. Used in production when `AZURE_STORAGE_CONNECTION_STRING` is set.

Both implement the same interface: `upload()`, `download()`, `delete()`, `get_url()`.

### REST API Endpoints

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/api/conversations` | List all conversations |
| POST | `/api/conversations` | Create a conversation |
| GET | `/api/conversations/<id>` | Get conversation with messages |
| POST | `/api/conversations/<id>/messages` | Add a message |
| GET | `/api/prompts` | List all system prompts |
| GET | `/api/settings` | List all settings |
| GET | `/health` | Health check (DB + storage status) |

---

## Proxy Server (FastAPI)

`rovodev_server.py` — A FastAPI application that exposes an **Anthropic Messages API-compatible endpoint** on port 8000.

### How It Works

1. **Request Validation** — Pydantic models validate incoming requests (`MessagesRequest`, `Message`, etc.).
2. **Message Processing** — Normalizes content blocks, truncates long conversations (>130 messages → 100), validates alternating user/assistant roles, removes orphaned tool results.
3. **CLI Orchestration** — Spawns `acli rovodev run` through the mitmproxy interceptor, using file-based IPC (queue files) to inject the request body.
4. **Response Capture** — Reads the raw SSE response captured by `intercept.py` and streams it back with proper `event:` line injection for Anthropic SDK compatibility.
5. **Conversation Logging** — Automatically logs each request/response pair to the Flask dashboard via its REST API.

### Rate Limiting

- Detects 429 responses from the upstream API.
- Enters a configurable cooldown period (default: 60 seconds).
- Purges all queued requests during rate limit.
- Returns `Retry-After` headers to clients.

### SSE Streaming

- **Incremental streaming** via file-based tail-reading (`SSEStreamFixer` class).
- **Keepalive injection** — Sends SSE comment lines (`: keepalive`) every 5 seconds to prevent client timeouts during long AI "thinking" periods.
- **Proper SSE formatting** — Injects `event: <type>` lines before each `data:` line, as required by the Anthropic Python SDK.

---

## Mitmproxy Interceptors

### intercept.py — Core Request Modifier

Runs on port 8080. Intercepts traffic between the CLI and the Atlassian Bedrock API:

- **System Prompt Injection** — Replaces the original system prompt with a custom one from `SYSTEM_PROMPT_CLAUDE.md`.
- **Request Relay** — Detects queued relay files from `rovodev_server.py` and swaps in the custom request body.
- **Tool Merging** — Merges client tools with required platform tools.
- **Prompt Caching** — Manages `cache_control` breakpoints for optimized API usage.
- **Response Capture** — Writes raw responses to disk with atomic file operations for the API server to read.
- **Streaming Support** — `StreamingCapture` callback captures response chunks while passing them through, with `KeepaliveInjector` background thread.
- **Rate Limit Handling** — Detects 429 responses and enters cooldown state.
- **Credits Interception** — Modifies credit balance responses.
- **Telemetry Modification** — Zeros token usage in telemetry events.

### amp_intercept.py — Amp CLI Bridge

Runs on port 8899. Bridges the Amp CLI to the local proxy infrastructure:

- **Request Rewriting** — Intercepts requests to `ampcode.com/api/provider/anthropic/v1/messages` and rewrites them to `localhost:8000/v1/messages`.
- **Token/Cost Zeroing** — `SSEUsageZeroer` class zeroes all token and cost fields in streaming responses.
- **Thinking Mode** — Converts thinking configuration to `adaptive` mode.
- **Prompt Caching** — Adds cache_control breakpoints to tools, system prompt, and conversation history.
- **Tool Injection** — Adds required Atlassian MCP tool definitions.
- **Haiku Pass-through** — Haiku model requests are logged but passed through to the original destination.

---

## Setup & Installation

### Prerequisites

- Python 3.11+
- mitmproxy (`pip install mitmproxy`)
- Atlassian CLI (`acli`) authenticated (`acli rovodev auth login`)

### Local Development

```bash
# 1. Clone the repository
git clone <repository-url>
cd .rovodev

# 2. Install Flask dashboard dependencies
cd flask_app
pip install -r requirements.txt

# 3. Run the Flask dashboard
python app.py
# Open http://localhost:5001
```

No environment variables needed locally — uses SQLite and local filesystem by default.

### Running All Services

Use the included service management script:

```bash
# Start all services (intercept.py, rovodev_server.py, Flask dashboard, amp_intercept.py)
./start.sh

# Check service status
./start.sh status

# Stop all services
./start.sh stop

# Restart all services
./start.sh restart
```

Services and their ports:

| Service | Port | Description |
|---------|------|-------------|
| `intercept.py` | 8080 | Core mitmproxy interceptor |
| `rovodev_server.py` | 8000 | FastAPI proxy server |
| Flask Dashboard | 5001 | Web UI |
| `amp_intercept.py` | 8899 | Amp CLI bridge |

---

## Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `DATABASE_URL` | No | `sqlite:///rovodev.db` | Database connection string (PostgreSQL for Azure) |
| `AZURE_STORAGE_CONNECTION_STRING` | No | _(empty)_ | Azure Blob Storage connection string |
| `AZURE_STORAGE_CONTAINER` | No | `rovodev-files` | Azure Blob container name |
| `SECRET_KEY` | No | `dev-secret-...` | Flask session secret key |
| `FLASK_DEBUG` | No | `0` | Enable Flask debug mode |
| `PORT` | No | `5001` | Flask app port |
| `ROVODEV_PROXY` | No | `http://127.0.0.1:8080` | Mitmproxy URL for the API server |
| `FLASK_API_URL` | No | `http://127.0.0.1:5001` | Flask dashboard URL for conversation logging |

---

## Deployment to Azure

### Azure App Service

1. Create an **Azure App Service** (Python 3.11 runtime).
2. Set the startup command:
   ```bash
   gunicorn --bind=0.0.0.0:8000 --timeout 600 --workers 2 app:app
   ```

### Azure PostgreSQL

1. Create an **Azure Database for PostgreSQL — Flexible Server**.
2. Set the `DATABASE_URL` environment variable in App Service Configuration:
   ```
   postgresql://user:password@yourserver.postgres.database.azure.com:5432/rovodev
   ```

### Azure Blob Storage

1. Create an **Azure Storage Account**.
2. Set `AZURE_STORAGE_CONNECTION_STRING` in App Service Configuration.
3. Optionally set `AZURE_STORAGE_CONTAINER` (defaults to `rovodev-files`).

### CI/CD with GitHub Actions

The project includes a GitHub Actions workflow (`.github/workflows/deploy.yml`) that:

1. Triggers on push to `main` branch (when `flask_app/` files change) or manual dispatch.
2. Sets up Python 3.11 and installs dependencies.
3. Zips the `flask_app/` directory (excluding `.pyc`, `__pycache__`, `uploads/`, `.db` files).
4. Deploys to Azure App Service using `azure/webapps-deploy@v3`.

**Required GitHub Secrets/Variables:**
- `AZURE_WEBAPP_PUBLISH_PROFILE` — Azure publish profile (secret)
- `AZURE_WEBAPP_NAME` — Azure App Service name (variable)

---

## Testing

```bash
python -m pytest test_*.py -v
```

---

## API Usage Examples

### Create a conversation via API

```bash
curl http://localhost:5001/api/conversations \
  -H "Content-Type: application/json" \
  -d '{"title": "My Conversation", "model": "claude-opus-4-6"}'
```

### Send a message to the proxy server

```bash
curl http://localhost:8000/v1/messages \
  -H "Content-Type: application/json" \
  -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
```

### Health checks

```bash
# Flask dashboard health
curl http://localhost:5001/health

# Proxy server health
curl http://localhost:8000/health
```
