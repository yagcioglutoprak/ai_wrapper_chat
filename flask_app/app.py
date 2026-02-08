"""RovoDev Dashboard — Flask application.

A web dashboard for managing conversations, system prompts, settings,
and files. Uses SQLAlchemy (SQLite locally, PostgreSQL on Azure) and
Azure Blob Storage (local filesystem fallback).

Run locally:
    python app.py

Environment variables (see .env.example):
    DATABASE_URL                      — DB connection string
    AZURE_STORAGE_CONNECTION_STRING   — Azure Blob Storage
    SECRET_KEY                        — Flask secret key
"""

from datetime import datetime, timezone

from flask import (
    Flask,
    abort,
    flash,
    jsonify,
    redirect,
    render_template,
    request,
    send_file,
    url_for,
)

from config import Config
from models import Conversation, FileUpload, Message, Setting, SystemPrompt, db
from storage import get_storage

import io


def create_app():
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)

    with app.app_context():
        db.create_all()
        _seed_defaults()

    return app


def _seed_defaults():
    """Insert default settings if the table is empty."""
    if Setting.query.count() == 0:
        defaults = [
            ("model", "claude-opus-4-6", "Default AI model"),
            ("max_tokens", "8192", "Maximum tokens per response"),
            ("proxy_url", "http://127.0.0.1:8080", "Mitmproxy URL"),
            ("rate_limit_cooldown", "60", "Rate limit cooldown in seconds"),
        ]
        for key, value, desc in defaults:
            db.session.add(Setting(key=key, value=value, description=desc))
        db.session.commit()

    if SystemPrompt.query.count() == 0:
        db.session.add(
            SystemPrompt(
                name="Default",
                content="You are a helpful coding assistant.",
                is_active=True,
            )
        )
        db.session.commit()


app = create_app()
file_storage = get_storage(app)


# ── Dashboard ────────────────────────────────────────────────────────────────


@app.route("/")
def index():
    stats = {
        "conversations": Conversation.query.count(),
        "messages": Message.query.count(),
        "prompts": SystemPrompt.query.count(),
        "files": FileUpload.query.count(),
    }
    recent = Conversation.query.order_by(Conversation.updated_at.desc()).limit(5).all()
    return render_template("index.html", stats=stats, recent=recent)


# ── Conversations CRUD ───────────────────────────────────────────────────────


@app.route("/conversations")
def conversations_list():
    convos = Conversation.query.order_by(Conversation.updated_at.desc()).all()
    return render_template("conversations.html", conversations=convos)


@app.route("/conversations/new", methods=["GET", "POST"])
def conversation_create():
    if request.method == "POST":
        conv = Conversation(
            title=request.form.get("title", "New Conversation"),
            model=request.form.get("model", "claude-opus-4-6"),
        )
        db.session.add(conv)
        db.session.commit()
        flash("Conversation created.", "success")
        return redirect(url_for("conversation_detail", conv_id=conv.id))
    return render_template("conversation_form.html", conversation=None)


@app.route("/conversations/<int:conv_id>")
def conversation_detail(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    messages = (
        Message.query.filter_by(conversation_id=conv_id)
        .order_by(Message.created_at)
        .all()
    )
    return render_template(
        "conversation_detail.html", conversation=conv, messages=messages
    )


@app.route("/conversations/<int:conv_id>/edit", methods=["GET", "POST"])
def conversation_edit(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    if request.method == "POST":
        conv.title = request.form.get("title", conv.title)
        conv.model = request.form.get("model", conv.model)
        conv.status = request.form.get("status", conv.status)
        db.session.commit()
        flash("Conversation updated.", "success")
        return redirect(url_for("conversation_detail", conv_id=conv.id))
    return render_template("conversation_form.html", conversation=conv)


@app.route("/conversations/<int:conv_id>/delete", methods=["POST"])
def conversation_delete(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    db.session.delete(conv)
    db.session.commit()
    flash("Conversation deleted.", "success")
    return redirect(url_for("conversations_list"))


@app.route("/conversations/<int:conv_id>/messages", methods=["POST"])
def message_create(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    msg = Message(
        conversation_id=conv.id,
        role=request.form.get("role", "user"),
        content=request.form.get("content", ""),
        token_count=len(request.form.get("content", "")) // 4,
    )
    db.session.add(msg)
    conv.updated_at = datetime.now(timezone.utc)
    db.session.commit()
    flash("Message added.", "success")
    return redirect(url_for("conversation_detail", conv_id=conv.id))


@app.route("/messages/<int:msg_id>/delete", methods=["POST"])
def message_delete(msg_id):
    msg = Message.query.get_or_404(msg_id)
    conv_id = msg.conversation_id
    db.session.delete(msg)
    db.session.commit()
    flash("Message deleted.", "success")
    return redirect(url_for("conversation_detail", conv_id=conv_id))


# ── System Prompts CRUD ──────────────────────────────────────────────────────


@app.route("/prompts")
def prompts_list():
    prompts = SystemPrompt.query.order_by(SystemPrompt.updated_at.desc()).all()
    return render_template("prompts.html", prompts=prompts)


@app.route("/prompts/new", methods=["GET", "POST"])
def prompt_create():
    if request.method == "POST":
        prompt = SystemPrompt(
            name=request.form.get("name", ""),
            content=request.form.get("content", ""),
            is_active=request.form.get("is_active") == "on",
        )
        if prompt.is_active:
            SystemPrompt.query.update({"is_active": False})
        db.session.add(prompt)
        db.session.commit()
        flash("Prompt created.", "success")
        return redirect(url_for("prompts_list"))
    return render_template("prompt_form.html", prompt=None)


@app.route("/prompts/<int:prompt_id>/edit", methods=["GET", "POST"])
def prompt_edit(prompt_id):
    prompt = SystemPrompt.query.get_or_404(prompt_id)
    if request.method == "POST":
        prompt.name = request.form.get("name", prompt.name)
        prompt.content = request.form.get("content", prompt.content)
        is_active = request.form.get("is_active") == "on"
        if is_active and not prompt.is_active:
            SystemPrompt.query.update({"is_active": False})
        prompt.is_active = is_active
        db.session.commit()
        flash("Prompt updated.", "success")
        return redirect(url_for("prompts_list"))
    return render_template("prompt_form.html", prompt=prompt)


@app.route("/prompts/<int:prompt_id>/delete", methods=["POST"])
def prompt_delete(prompt_id):
    prompt = SystemPrompt.query.get_or_404(prompt_id)
    db.session.delete(prompt)
    db.session.commit()
    flash("Prompt deleted.", "success")
    return redirect(url_for("prompts_list"))


# ── Settings CRUD ────────────────────────────────────────────────────────────


@app.route("/settings", methods=["GET", "POST"])
def settings_page():
    if request.method == "POST":
        for s in Setting.query.all():
            new_val = request.form.get(f"setting_{s.id}", s.value)
            if new_val != s.value:
                s.value = new_val
        db.session.commit()
        flash("Settings saved.", "success")
        return redirect(url_for("settings_page"))
    settings = Setting.query.order_by(Setting.key).all()
    return render_template("settings.html", settings=settings)


@app.route("/settings/new", methods=["POST"])
def setting_create():
    key = request.form.get("key", "").strip()
    value = request.form.get("value", "")
    desc = request.form.get("description", "")
    if key:
        existing = Setting.query.filter_by(key=key).first()
        if existing:
            flash(f"Setting '{key}' already exists.", "error")
        else:
            db.session.add(Setting(key=key, value=value, description=desc))
            db.session.commit()
            flash(f"Setting '{key}' created.", "success")
    return redirect(url_for("settings_page"))


@app.route("/settings/<int:setting_id>/delete", methods=["POST"])
def setting_delete(setting_id):
    s = Setting.query.get_or_404(setting_id)
    db.session.delete(s)
    db.session.commit()
    flash("Setting deleted.", "success")
    return redirect(url_for("settings_page"))


# ── Files (Azure Blob Storage / Local) ───────────────────────────────────────


@app.route("/files")
def files_list():
    files = FileUpload.query.order_by(FileUpload.uploaded_at.desc()).all()
    return render_template("files.html", files=files)


@app.route("/files/upload", methods=["POST"])
def file_upload():
    f = request.files.get("file")
    if not f or not f.filename:
        flash("No file selected.", "error")
        return redirect(url_for("files_list"))

    data = f.read()
    blob_name, size = file_storage.upload(data, f.filename, f.content_type)
    url = file_storage.get_url(blob_name)

    upload = FileUpload(
        filename=f.filename,
        blob_url=blob_name,
        content_type=f.content_type or "application/octet-stream",
        size_bytes=size,
    )
    db.session.add(upload)
    db.session.commit()
    flash("File uploaded.", "success")
    return redirect(url_for("files_list"))


@app.route("/files/<int:file_id>/download")
def file_download(file_id):
    fu = FileUpload.query.get_or_404(file_id)
    data = file_storage.download(fu.blob_url)
    if data is None:
        abort(404, "File not found in storage")
    return send_file(
        io.BytesIO(data),
        download_name=fu.filename,
        mimetype=fu.content_type,
    )


@app.route("/files/serve/<path:blob_name>")
def file_serve(blob_name):
    """Serve local files (used when Azure Blob is not configured)."""
    data = file_storage.download(blob_name)
    if data is None:
        abort(404)
    fu = FileUpload.query.filter_by(blob_url=blob_name).first()
    mimetype = fu.content_type if fu else "application/octet-stream"
    return send_file(io.BytesIO(data), mimetype=mimetype)


@app.route("/files/<int:file_id>/delete", methods=["POST"])
def file_delete(file_id):
    fu = FileUpload.query.get_or_404(file_id)
    file_storage.delete(fu.blob_url)
    db.session.delete(fu)
    db.session.commit()
    flash("File deleted.", "success")
    return redirect(url_for("files_list"))


# ── API Endpoints (JSON) ─────────────────────────────────────────────────────


@app.route("/api/conversations", methods=["GET"])
def api_conversations():
    convos = Conversation.query.order_by(Conversation.updated_at.desc()).all()
    return jsonify([c.to_dict() for c in convos])


@app.route("/api/conversations/<int:conv_id>", methods=["GET"])
def api_conversation(conv_id):
    conv = Conversation.query.get_or_404(conv_id)
    data = conv.to_dict()
    data["messages"] = [m.to_dict() for m in conv.messages]
    return jsonify(data)


@app.route("/api/prompts", methods=["GET"])
def api_prompts():
    prompts = SystemPrompt.query.all()
    return jsonify([p.to_dict() for p in prompts])


@app.route("/api/settings", methods=["GET"])
def api_settings():
    settings = Setting.query.all()
    return jsonify([s.to_dict() for s in settings])


# ── Health Check ─────────────────────────────────────────────────────────────


@app.route("/health")
def health():
    try:
        db.session.execute(db.text("SELECT 1"))
        db_status = "ok"
    except Exception as e:
        db_status = f"error: {e}"

    storage_type = (
        "azure_blob"
        if app.config.get("AZURE_STORAGE_CONNECTION_STRING")
        else "local"
    )

    return jsonify(
        {
            "status": "ok" if db_status == "ok" else "degraded",
            "database": db_status,
            "storage": storage_type,
            "database_url_set": bool(
                app.config.get("SQLALCHEMY_DATABASE_URI", "").startswith("postgresql")
            ),
        }
    )


# ── Main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=True)
