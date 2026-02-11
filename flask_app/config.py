"""Configuration from environment variables.

Locally: no env vars needed → SQLite + local filesystem.
On Azure: set DATABASE_URL, AZURE_STORAGE_CONNECTION_STRING, etc.
"""

import os


class Config:
    # Flask
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-change-in-production")
    DEBUG = os.environ.get("FLASK_DEBUG", "0") == "1"

    # Database — SQLite locally, PostgreSQL on Azure
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", "sqlite:///dashboard.db"
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Azure Blob Storage (optional — falls back to local filesystem)
    AZURE_STORAGE_CONNECTION_STRING = os.environ.get(
        "AZURE_STORAGE_CONNECTION_STRING", ""
    )
    AZURE_STORAGE_CONTAINER = os.environ.get(
        "AZURE_STORAGE_CONTAINER", "app-files"
    )

    # AI API server (Anthropic-compatible API)
    API_SERVER_URL = os.environ.get("API_SERVER_URL", "http://127.0.0.1:8000")

    # Local file upload fallback
    UPLOAD_FOLDER = os.environ.get(
        "UPLOAD_FOLDER",
        os.path.join(os.path.dirname(os.path.abspath(__file__)), "uploads"),
    )
    MAX_CONTENT_LENGTH = 16 * 1024 * 1024  # 16 MB max upload
