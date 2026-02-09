#!/usr/bin/env python3
"""
Anthropic-compatible API server for Opus 4.6 via rovodev CLI.

Exposes POST /v1/messages matching the Anthropic Messages API.
Internally spawns `acli rovodev run` through mitmproxy (intercept.py)
which swaps the caller's messages/system/tools into the CLI request
and captures the raw SSE response.

Prerequisites:
  - mitmdump -s intercept.py -p 8080 (already running)
  - acli rovodev auth login

Run:
  python rovodev_server.py

Usage (drop-in replacement for Anthropic SDK):
  curl http://localhost:8000/v1/messages -H "Content-Type: application/json" -d '{
    "model": "claude-opus-4-6",
    "max_tokens": 4096,
    "messages": [{"role": "user", "content": "Hello"}]
  }'
"""

import asyncio
import fcntl
import json
import os
import signal
import subprocess
import tempfile
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from typing import Any, Dict, List, Optional, Union

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response, StreamingResponse
from pydantic import BaseModel, Field

app = FastAPI(title="RovoDev Anthropic API", version="7.0.0")

# ---------------------------------------------------------------------------
# CORS – allow browser-based clients (Cline, web UIs, etc.)
# ---------------------------------------------------------------------------
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
    expose_headers=["x-request-id"],
)


# ---------------------------------------------------------------------------
# Anthropic-style error responses
# ---------------------------------------------------------------------------

_ERROR_TYPE_MAP = {
    400: "invalid_request_error",
    401: "authentication_error",
    403: "permission_error",
    404: "not_found_error",
    408: "timeout_error",
    422: "invalid_request_error",
    429: "rate_limit_error",
    500: "api_error",
    502: "api_error",
    503: "overloaded_error",
    504: "timeout_error",
    529: "overloaded_error",
}


def _anthropic_error(status: int, message: str) -> JSONResponse:
    error_type = _ERROR_TYPE_MAP.get(status, "api_error")
    return JSONResponse(
        status_code=status,
        content={"type": "error", "error": {"type": error_type, "message": message}},
    )


@app.exception_handler(HTTPException)
async def anthropic_error_handler(request: Request, exc: HTTPException):
    return _anthropic_error(exc.status_code, str(exc.detail))


from fastapi.exceptions import RequestValidationError


@app.exception_handler(RequestValidationError)
async def validation_error_handler(request: Request, exc: RequestValidationError):
    msgs = "; ".join(
        f"{'.'.join(str(x) for x in e['loc'])}: {e['msg']}" for e in exc.errors()
    )
    return _anthropic_error(400, msgs)


# ---------------------------------------------------------------------------
# Concurrency
# ---------------------------------------------------------------------------

# Each request blocks a thread for the full duration (up to 5 min),
# so we size the pool accordingly.  8 workers means 8 concurrent requests.
_executor = ThreadPoolExecutor(max_workers=8)


# ---------------------------------------------------------------------------
# File-based CLI lock using fcntl (atomic, no stale-PID races)
# ---------------------------------------------------------------------------

CLI_LOCK_FILE = os.path.expanduser("~/.rovodev/cli.lock")


@contextmanager
def _cli_lock(timeout: float = 120.0):
    """Acquire an exclusive flock on CLI_LOCK_FILE.

    Uses POSIX flock which is automatically released when the fd is closed
    (including on process crash), eliminating stale-lock issues.
    """
    os.makedirs(os.path.dirname(CLI_LOCK_FILE), exist_ok=True)
    fd = os.open(CLI_LOCK_FILE, os.O_CREAT | os.O_RDWR)
    deadline = time.monotonic() + timeout
    acquired = False
    try:
        while time.monotonic() < deadline:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                acquired = True
                # Write our PID for debugging (not used for lock logic)
                os.ftruncate(fd, 0)
                os.lseek(fd, 0, os.SEEK_SET)
                os.write(fd, f"{os.getpid()}\n".encode())
                break
            except BlockingIOError:
                time.sleep(0.05)
        if not acquired:
            raise TimeoutError(
                "Could not acquire CLI lock – another request may be stuck"
            )
        yield
    finally:
        try:
            fcntl.flock(fd, fcntl.LOCK_UN)
        except OSError:
            pass
        os.close(fd)


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

PROXY_URL = os.environ.get("ROVODEV_PROXY", "http://127.0.0.1:8080")
DEFAULT_TIMEOUT = 600  # seconds (10 minutes)
POLL_INTERVAL = 0.05  # seconds

CAPTURE_DIR = os.path.expanduser("~/.rovodev/raw_responses")
QUEUE_DIR = os.path.expanduser("~/.rovodev/queue")
MITMPROXY_CERT = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
RATE_LIMIT_FILE = os.path.expanduser("~/.rovodev/queue/rate_limit.json")

# Maximum messages to keep.  We try to preserve tool_use/tool_result pairs
# at the truncation boundary.
MAX_MESSAGES = 100  # threshold is 130, cut 30 → keep 100

RATE_LIMIT_DEFAULT_COOLDOWN = 60  # seconds

FLASK_API_URL = os.environ.get("FLASK_API_URL", "http://127.0.0.1:5001")

import threading
import urllib.request
import urllib.error


def _log_conversation(req: "MessagesRequest", response_text: str, response_tool_uses: list = None):
    """Log the conversation (user message + assistant response) to the Flask dashboard DB."""
    try:
        user_preview = ""
        for msg in reversed(req.messages):
            role = msg.role if hasattr(msg, "role") else msg.get("role", "")
            if role == "user":
                content = msg.content if hasattr(msg, "content") else msg.get("content", "")
                if isinstance(content, str):
                    user_preview = content[:200]
                elif isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "text":
                            user_preview = block.get("text", "")[:200]
                            break
                break

        title = user_preview[:80] or "API Conversation"
        if len(user_preview) > 80:
            title += "..."

        conv_data = json.dumps({
            "title": title,
            "model": req.model,
        }).encode("utf-8")
        conv_req = urllib.request.Request(
            f"{FLASK_API_URL}/api/conversations",
            data=conv_data,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        conv_resp = urllib.request.urlopen(conv_req, timeout=5)
        conv = json.loads(conv_resp.read())
        conv_id = conv["id"]

        if user_preview:
            msg_data = json.dumps({
                "role": "user",
                "content": user_preview,
                "token_count": len(user_preview) // 4,
            }).encode("utf-8")
            msg_req = urllib.request.Request(
                f"{FLASK_API_URL}/api/conversations/{conv_id}/messages",
                data=msg_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(msg_req, timeout=5)

        if response_text:
            assistant_content = response_text[:5000]
            msg_data = json.dumps({
                "role": "assistant",
                "content": assistant_content,
                "token_count": len(assistant_content) // 4,
            }).encode("utf-8")
            msg_req = urllib.request.Request(
                f"{FLASK_API_URL}/api/conversations/{conv_id}/messages",
                data=msg_data,
                headers={"Content-Type": "application/json"},
                method="POST",
            )
            urllib.request.urlopen(msg_req, timeout=5)

        if response_tool_uses:
            for tu in response_tool_uses:
                tool_content = f"[Tool: {tu.get('name', '?')}] {json.dumps(tu.get('input', {}))[:500]}"
                msg_data = json.dumps({
                    "role": "assistant",
                    "content": tool_content,
                    "token_count": len(tool_content) // 4,
                }).encode("utf-8")
                msg_req = urllib.request.Request(
                    f"{FLASK_API_URL}/api/conversations/{conv_id}/messages",
                    data=msg_data,
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                urllib.request.urlopen(msg_req, timeout=5)

        print(f"[DASHBOARD] Logged conversation {conv_id}: {title[:50]}")
    except Exception as e:
        print(f"[DASHBOARD] Could not log conversation: {e}")

_rate_limit_lock = threading.Lock()
_rate_limit_state = {"active": False, "until": 0.0}


def _enter_rate_limit_server(retry_after: float = None):
    cooldown = retry_after if retry_after else RATE_LIMIT_DEFAULT_COOLDOWN
    with _rate_limit_lock:
        _rate_limit_state["active"] = True
        _rate_limit_state["until"] = time.time() + cooldown
    print(f"[RATE_LIMIT] 429 detected — cooldown for {cooldown:.0f}s, purging queues")
    _purge_all_queues()


def _is_rate_limited_server() -> bool:
    with _rate_limit_lock:
        if not _rate_limit_state["active"]:
            return False
        if time.time() >= _rate_limit_state["until"]:
            _rate_limit_state["active"] = False
            print("[RATE_LIMIT] Cooldown expired — accepting requests again")
            return False
        return True


def _rate_limit_remaining_server() -> float:
    with _rate_limit_lock:
        if not _rate_limit_state["active"]:
            return 0.0
        return max(0.0, _rate_limit_state["until"] - time.time())


def _purge_all_queues():
    try:
        if not os.path.isdir(QUEUE_DIR):
            return
        removed = 0
        for fname in os.listdir(QUEUE_DIR):
            if fname.startswith("relay_queue.") or fname.startswith("capture_next."):
                try:
                    os.remove(os.path.join(QUEUE_DIR, fname))
                    removed += 1
                except OSError:
                    pass
        if removed:
            print(f"[RATE_LIMIT] Purged {removed} queued file(s)")
    except Exception as e:
        print(f"[RATE_LIMIT] Error purging queues: {e}")


# ---------------------------------------------------------------------------
# Anthropic Messages API request models
# ---------------------------------------------------------------------------


class ContentBlockText(BaseModel):
    type: str = "text"
    text: str


class ContentBlockImage(BaseModel):
    type: str = "image"
    source: Dict[str, Any]


class ToolUseBlock(BaseModel):
    type: str = "tool_use"
    id: str
    name: str
    input: Any


class ToolResultBlock(BaseModel):
    type: str = "tool_result"
    tool_use_id: str
    content: Any = None
    is_error: bool = False


class Message(BaseModel):
    model_config = {"extra": "allow"}
    role: str
    content: Union[str, List[Any]]


class MessagesRequest(BaseModel):
    model_config = {"extra": "allow"}
    model: str = Field(default="claude-opus-4-6")
    messages: List[Message]
    max_tokens: int = Field(default=8192)
    system: Optional[Union[str, List[Any]]] = None
    temperature: Optional[float] = None
    top_p: Optional[float] = None
    top_k: Optional[int] = None
    stream: Optional[bool] = Field(default=True)
    tools: Optional[List[Any]] = None
    tool_choice: Optional[Any] = None
    stop_sequences: Optional[List[str]] = None
    metadata: Optional[Dict[str, Any]] = None


# ---------------------------------------------------------------------------
# Helpers: content normalisation
# ---------------------------------------------------------------------------


def _normalize_content(content: Any) -> list:
    """Ensure message content is always an array of content blocks."""
    if isinstance(content, str):
        return [{"type": "text", "text": content}]
    if isinstance(content, list):
        return content
    return [{"type": "text", "text": str(content)}]


def _normalize_system(system: Any) -> Optional[list]:
    """Ensure system prompt is always an array of content blocks."""
    if system is None:
        return None
    if isinstance(system, str):
        return [{"type": "text", "text": system}]
    if isinstance(system, list):
        return system
    return [{"type": "text", "text": str(system)}]


# ---------------------------------------------------------------------------
# Smart message truncation
# ---------------------------------------------------------------------------


def _truncate_messages(messages: list, limit: int, threshold: int = 130) -> list:
    """Truncate to the last *limit* messages when total exceeds *threshold*.

    Strategy: only truncate when messages exceed threshold (130). When cutting,
    keep the last `limit` (100) messages, then walk the start forward
    until we land on a user message that has NO tool_result blocks (a clean
    conversational boundary). This guarantees we never start mid-tool-exchange.
    """
    if len(messages) <= threshold:
        return messages

    start_idx = max(0, len(messages) - limit)

    # Walk forward to find a safe cut point: a user message with no tool_results
    while start_idx < len(messages):
        msg = messages[start_idx]
        role = msg.get("role", "")

        if role == "user":
            content = msg.get("content", [])
            has_tool_results = False
            if isinstance(content, list):
                has_tool_results = any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
            if not has_tool_results:
                # Clean user message — safe to start here
                break

        # Skip this message (assistant, or user with tool_results)
        start_idx += 1

    if start_idx >= len(messages):
        # Fallback: walk backward from original start to find a clean user message
        start_idx = max(0, len(messages) - limit)
        while start_idx > 0:
            start_idx -= 1
            msg = messages[start_idx]
            if msg.get("role") == "user":
                content = msg.get("content", [])
                has_tool_results = isinstance(content, list) and any(
                    isinstance(b, dict) and b.get("type") == "tool_result"
                    for b in content
                )
                if not has_tool_results:
                    break
        # If still no clean boundary, just use the last few messages
        if start_idx <= 0:
            start_idx = max(0, len(messages) - 4)

    result = messages[start_idx:]

    if len(result) < len(messages):
        print(
            f"[DEBUG] Truncated from {len(messages)} to {len(result)} messages "
            f"(cut at index {start_idx})"
        )

    return result


# ---------------------------------------------------------------------------
# Validate & fix message sequence
# ---------------------------------------------------------------------------


def _validate_messages(messages: list) -> list:
    """Ensure messages follow Anthropic API rules:
    - Must start with 'user'
    - Roles must alternate (merge consecutive same-role messages)
    - No empty content blocks
    - Each tool_result must have a matching tool_use in the IMMEDIATELY previous message
    """
    if not messages:
        raise ValueError("messages list is empty")

    validated = []
    for i, msg in enumerate(messages):
        role = msg.get("role", "")
        content = msg.get("content")

        # Skip messages with empty/null content
        if content is None:
            continue
        if isinstance(content, str) and not content.strip():
            continue
        if isinstance(content, list) and len(content) == 0:
            continue

        # Filter out orphaned tool_result blocks by checking the immediately
        # preceding assistant message (not the entire history)
        if role == "user" and isinstance(content, list):
            # Get tool_use IDs from the immediately preceding assistant message
            prev_tool_use_ids = set()
            if validated and validated[-1].get("role") == "assistant":
                prev_content = validated[-1].get("content", [])
                if isinstance(prev_content, list):
                    for block in prev_content:
                        if isinstance(block, dict) and block.get("type") == "tool_use":
                            prev_tool_use_ids.add(block.get("id"))

            filtered_content = []
            removed_count = 0
            for block in content:
                if isinstance(block, dict) and block.get("type") == "tool_result":
                    tool_use_id = block.get("tool_use_id")
                    if tool_use_id not in prev_tool_use_ids:
                        # Orphaned tool_result - no matching tool_use in previous message
                        removed_count += 1
                        continue
                filtered_content.append(block)

            if removed_count > 0:
                print(
                    f"[DEBUG] Removed {removed_count} orphaned tool_result block(s) from message {i} "
                    f"(prev assistant had {len(prev_tool_use_ids)} tool_use IDs)"
                )

            if not filtered_content:
                # Message only had orphaned tool_results - skip entire message
                continue

            content = filtered_content
            msg = {**msg, "content": content}

        # Merge consecutive same-role messages
        if validated and validated[-1].get("role") == role:
            prev_content = validated[-1].get("content", [])
            if isinstance(prev_content, str):
                prev_content = [{"type": "text", "text": prev_content}]
            curr_content = (
                content
                if isinstance(content, list)
                else [{"type": "text", "text": content}]
            )
            validated[-1] = {**validated[-1], "content": prev_content + curr_content}
        else:
            validated.append(msg)

    if not validated:
        raise ValueError("No valid messages remaining after filtering")

    # Must start with user
    while validated and validated[0].get("role") != "user":
        validated.pop(0)

    if not validated:
        raise ValueError("No user message found")

    return validated


# ---------------------------------------------------------------------------
# Build the body for the Atlassian/Vertex gateway
# ---------------------------------------------------------------------------

# Fields the Atlassian gateway accepts (temperature EXCLUDED — causes 403)
_GATEWAY_FIELDS = {
    "messages",
    "system",
    "max_tokens",
    "top_p",
    "top_k",
    "tools",
    "stop_sequences",
    "anthropic_version",
    "thinking",
}


def _build_anthropic_body(req: MessagesRequest) -> dict:
    """Convert incoming request to the body the Atlassian gateway expects."""
    body = req.model_dump(exclude_none=True)

    # Remove server-side / unsupported fields
    body.pop("stream", None)
    body.pop("model", None)
    body.pop("metadata", None)
    body.pop("temperature", None)  # causes 403

    unsupported = [k for k in body if k not in _GATEWAY_FIELDS]
    for k in unsupported:
        body.pop(k)

    # ------------------------------------------------------------------
    # System prompt handling
    # ------------------------------------------------------------------
    # KEEP system prompt - intercept.py will replace it with custom one
    # The gateway accepts system prompts (they don't cause 403)
    if "system" in body:
        print(f"[DEBUG] Keeping system prompt ({len(body['system'])} chars)")

    # ------------------------------------------------------------------
    # Tools handling
    # ------------------------------------------------------------------
    # KEEP tools - intercept.py will replace them with custom ones
    # The gateway accepts tools (they don't cause 403)
    if "tools" in body:
        print(f"[DEBUG] Keeping {len(body['tools'])} tools")

    # ------------------------------------------------------------------
    # Messages
    # ------------------------------------------------------------------
    messages = body.get("messages", [])

    # Vertex AI doesn't support assistant-message prefill — remove trailing
    while messages and messages[-1].get("role") == "assistant":
        messages.pop()
        print(
            "[DEBUG] Removed trailing assistant message (Vertex doesn't support prefill)"
        )

    if not messages:
        raise ValueError("No valid messages remaining after filtering")

    # Smart truncation that preserves tool pairs
    messages = _truncate_messages(messages, MAX_MESSAGES)

    # Validate alternating roles, merge duplicates
    messages_as_dicts = []
    for m in messages:
        if isinstance(m, dict):
            messages_as_dicts.append(m)
        else:
            messages_as_dicts.append(
                m.model_dump() if hasattr(m, "model_dump") else dict(m)
            )
    messages_as_dicts = _validate_messages(messages_as_dicts)

    # Normalize content to array-of-blocks (Vertex requirement)
    body["messages"] = [
        {
            "role": m["role"],
            "content": _normalize_content(m["content"]),
            **{k: v for k, v in m.items() if k not in ("role", "content")},
        }
        for m in messages_as_dicts
    ]

    # Remove tool_choice — Bedrock gateway returns 403 with it
    body.pop("tool_choice", None)

    # Pass through adaptive thinking if present, remove other thinking configs
    if isinstance(body.get("thinking"), dict) and body["thinking"].get("type") == "adaptive":
        pass  # Keep adaptive thinking
    else:
        body.pop("thinking", None)

    # Strip thinking/redacted_thinking content blocks from assistant messages
    for msg in body.get("messages", []):
        content = msg.get("content")
        if isinstance(content, list):
            msg["content"] = [
                b for b in content
                if not (isinstance(b, dict) and b.get("type") in ("thinking", "redacted_thinking"))
            ]

    # Keep cache_control — Bedrock supports prompt caching
    print(f"[DEBUG] Preserving cache_control for prompt caching")

    # Add required anthropic_version for Bedrock
    body["anthropic_version"] = "bedrock-2023-05-31"

    # Debug: log final message structure
    print(
        f"[DEBUG] Final body: {len(body.get('messages', []))} messages, "
        f"roles={[m.get('role') for m in body.get('messages', [])]}"
    )

    return body


# ---------------------------------------------------------------------------
# Cleanup helpers
# ---------------------------------------------------------------------------


def _cleanup_stale_files(
    directory: str, max_age: float = 300.0, extensions: tuple = ()
):
    """Remove files older than *max_age* seconds from *directory*."""
    try:
        now = time.time()
        for name in os.listdir(directory):
            if extensions and not any(name.endswith(ext) for ext in extensions):
                continue
            path = os.path.join(directory, name)
            try:
                if now - os.path.getmtime(path) > max_age:
                    os.unlink(path)
                    print(f"[CLEANUP] Removed stale file: {name}")
            except OSError:
                pass
    except FileNotFoundError:
        pass


# ---------------------------------------------------------------------------
# Core: spawn CLI → inject body via intercept.py → capture raw SSE response
# ---------------------------------------------------------------------------


def _run_and_capture(
    req: MessagesRequest,
    capture_id: str = None,
    skip_stream_cleanup: bool = False,
) -> tuple:
    """Returns (status_code: int, content_type: str, raw_bytes: bytes)."""
    os.makedirs(CAPTURE_DIR, exist_ok=True)
    os.makedirs(QUEUE_DIR, exist_ok=True)

    # Periodic cleanup
    _cleanup_stale_files(QUEUE_DIR, max_age=300.0, extensions=(".json",))
    _cleanup_stale_files(
        CAPTURE_DIR,
        max_age=300.0,
        extensions=(".raw", ".meta", ".tmp", ".stream", ".stream.done"),
    )

    if capture_id is None:
        capture_id = str(uuid.uuid4())
    raw_path = os.path.join(CAPTURE_DIR, f"{capture_id}.raw")
    meta_path = os.path.join(CAPTURE_DIR, f"{capture_id}.meta")
    stream_path = raw_path + ".stream"
    done_path = stream_path + ".done"
    relay_queue_file = os.path.join(QUEUE_DIR, f"relay_queue.{capture_id}.json")
    capture_next_file = os.path.join(QUEUE_DIR, f"capture_next.{capture_id}.json")

    # Pre-clean
    for p in (raw_path, meta_path, stream_path, done_path, relay_queue_file, capture_next_file):
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    # 1) Write the relay queue so intercept.py swaps our body into the CLI request
    relay_body = _build_anthropic_body(req)
    with open(relay_queue_file, "w") as f:
        json.dump(relay_body, f)
        f.flush()
        os.fsync(f.fileno())

    print(
        f"[DEBUG] Request {capture_id[:8]}: "
        f"keys={list(relay_body.keys())}, "
        f"tools={len(relay_body.get('tools', []))}, "
        f"msgs={len(relay_body.get('messages', []))}"
    )

    # 2) Tell intercept.py where to write the captured response
    with open(capture_next_file, "w") as f:
        json.dump(
            {"raw_path": raw_path, "meta_path": meta_path, "request_id": capture_id},
            f,
        )
        f.flush()
        os.fsync(f.fileno())

    # 3) Spawn CLI
    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="rovodev_api_")
    os.close(fd)
    try:
        os.unlink(output_path)
    except OSError:
        pass

    cmd = ["acli", "rovodev", "run", ".", "--output-file", output_path, "--yolo"]

    env = os.environ.copy()
    env["HTTPS_PROXY"] = PROXY_URL
    env["HTTP_PROXY"] = PROXY_URL
    env["SSL_CERT_FILE"] = MITMPROXY_CERT
    env["REQUESTS_CA_BUNDLE"] = MITMPROXY_CERT
    env["ROVODEV_REQUEST_ID"] = capture_id
    # Increase HTTP timeouts to prevent acli from closing long streaming connections
    env["HTTP_TIMEOUT"] = "600"
    env["REQUESTS_TIMEOUT"] = "600"
    env["AIOHTTP_TIMEOUT"] = "600"
    env["URLLIB3_TIMEOUT"] = "600"

    proc = None
    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=os.getcwd(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        deadline = time.monotonic() + DEFAULT_TIMEOUT
        while time.monotonic() < deadline:
            if os.path.exists(meta_path):
                break

            rc = proc.poll()
            if rc is not None:
                for _ in range(12000):  # up to 10 minutes
                    if os.path.exists(meta_path):
                        break
                    time.sleep(0.05)
                if os.path.exists(meta_path):
                    break
                stdout = (proc.stdout.read() or b"").decode(errors="replace")[:500]
                stderr = (proc.stderr.read() or b"").decode(errors="replace")[:500]
                raise RuntimeError(
                    f"CLI exited (code {rc}) before response was captured. "
                    f"stdout: {stdout}. stderr: {stderr}"
                )

            time.sleep(POLL_INTERVAL)

        if not os.path.exists(meta_path):
            raise TimeoutError(f"No response captured after {DEFAULT_TIMEOUT}s")

        # Read captured response
        with open(meta_path, "r") as f:
            meta = json.load(f)
        with open(raw_path, "rb") as f:
            raw = f.read()

        status_code = int(meta.get("status_code", 200))
        headers = meta.get("headers", {}) or {}
        content_type = (
            headers.get("Content-Type")
            or headers.get("content-type")
            or "text/event-stream"
        )

        return status_code, content_type, raw

    except TimeoutError:
        raise
    except RuntimeError:
        raise
    except Exception as e:
        raise RuntimeError(f"Unexpected error: {e}") from e
    finally:
        # Kill lingering CLI process
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
                proc.wait(timeout=5)
            except Exception:
                pass

        # Clean up temp files (stream files left for caller in streaming mode)
        cleanup_paths = [
            output_path,
            raw_path,
            meta_path,
            relay_queue_file,
            capture_next_file,
        ]
        if not skip_stream_cleanup:
            cleanup_paths.extend([stream_path, done_path])
        for p in cleanup_paths:
            try:
                os.unlink(p)
            except (OSError, FileNotFoundError):
                pass


# ---------------------------------------------------------------------------
# SSE processing
# ---------------------------------------------------------------------------


def _fix_sse_events(raw: bytes) -> bytes:
    """Inject missing `event:` lines, zero tokens, ensure usage, and
    apply proper SSE formatting.

    The raw Vertex response only has `data:` lines.  The Anthropic Python SDK
    requires `event: <type>\\n` before each `data:` line and a blank line
    after each event block.
    """
    lines = raw.decode("utf-8", errors="replace").splitlines()
    out: list[str] = []
    for line in lines:
        stripped = line.strip()

        if not stripped:
            out.append("")
            continue

        if stripped.startswith("data: "):
            data_str = stripped[6:].strip()
            try:
                evt = json.loads(data_str)
                evt_type = evt.get("type")
                if evt_type:
                    out.append(f"event: {evt_type}")
                _zero_tokens(evt)
                _ensure_usage(evt)
                out.append(f"data: {json.dumps(evt)}")
            except (json.JSONDecodeError, AttributeError, TypeError):
                out.append(stripped)
            out.append("")  # Blank line terminates SSE event
        else:
            out.append(line)

    return "\n".join(out).encode("utf-8")


def _sse_to_messages_response(raw: bytes) -> bytes:
    """Assemble a single Anthropic Messages JSON response from an SSE stream."""
    text_parts: list[str] = []
    tool_uses: list[dict] = []
    model = "claude-opus-4-6"
    msg_id = None
    stop_reason = None
    input_tokens = 0
    output_tokens = 0

    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.strip()
        if not line.startswith("data: "):
            continue
        data_str = line[6:].strip()
        if not data_str:
            continue
        try:
            evt = json.loads(data_str)
        except json.JSONDecodeError:
            continue

        evt_type = evt.get("type")

        if evt_type == "message_start":
            msg = evt.get("message", {})
            model = msg.get("model", model)
            msg_id = msg.get("id")
            usage = msg.get("usage", {})
            input_tokens = (
                usage.get("input_tokens", 0)
                + usage.get("cache_read_input_tokens", 0)
                + usage.get("cache_creation_input_tokens", 0)
            )

        elif evt_type == "content_block_start":
            block = evt.get("content_block", {})
            if block.get("type") == "text":
                text_parts.append("")  # placeholder
            elif block.get("type") == "tool_use":
                tool_uses.append(
                    {
                        "type": "tool_use",
                        "id": block.get("id", ""),
                        "name": block.get("name", ""),
                        "input": "",
                    }
                )

        elif evt_type == "content_block_delta":
            delta = evt.get("delta", {})
            if delta.get("type") == "text_delta":
                text_parts.append(delta.get("text", ""))
            elif delta.get("type") == "input_json_delta":
                if tool_uses:
                    tool_uses[-1]["input"] += delta.get("partial_json", "")

        elif evt_type == "message_delta":
            d = evt.get("delta", {})
            stop_reason = d.get("stop_reason") or stop_reason
            output_tokens = evt.get("usage", {}).get("output_tokens", output_tokens)

    # Parse accumulated tool input JSON strings
    for tu in tool_uses:
        if isinstance(tu["input"], str):
            try:
                tu["input"] = json.loads(tu["input"])
            except (json.JSONDecodeError, TypeError):
                tu["input"] = {}

    content: list[dict] = []
    full_text = "".join(text_parts)
    if full_text:
        content.append({"type": "text", "text": full_text})
    content.extend(tool_uses)

    if not content:
        content.append({"type": "text", "text": ""})

    response = {
        "id": msg_id or f"msg_{uuid.uuid4().hex[:24]}",
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": content,
        "stop_reason": stop_reason or "end_turn",
        "stop_sequence": None,
        "usage": {
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
        },
    }
    return json.dumps(response).encode("utf-8")


# ---------------------------------------------------------------------------
# Anthropic-compatible endpoints
# ---------------------------------------------------------------------------


KEEPALIVE_INTERVAL = 5  # seconds between SSE keepalive comments
STREAM_POLL_INTERVAL = 0.1  # seconds between stream file checks


_ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


def _ensure_usage(evt: dict) -> None:
    """Guarantee that message_start and message_delta SSE events always
    carry a ``usage`` object so the downstream client never crashes on
    ``D.usage.input_tokens`` being undefined."""
    evt_type = evt.get("type")
    if evt_type == "message_start":
        msg = evt.get("message")
        if isinstance(msg, dict):
            if "usage" not in msg or not isinstance(msg.get("usage"), dict):
                msg["usage"] = dict(_ZERO_USAGE)
            else:
                for k, v in _ZERO_USAGE.items():
                    msg["usage"].setdefault(k, v)
    elif evt_type == "message_delta":
        if "usage" not in evt or not isinstance(evt.get("usage"), dict):
            evt["usage"] = {"output_tokens": 0}
        else:
            evt["usage"].setdefault("output_tokens", 0)


def _zero_tokens(evt: dict, depth: int = 0) -> None:
    """Recursively zero all token/cost fields in an SSE event."""
    if depth > 20:
        return
    _TOKEN_KEYS = {
        "input_tokens", "output_tokens", "total_tokens",
        "cache_creation_input_tokens", "cache_read_input_tokens",
        "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    }
    if isinstance(evt, dict):
        for k in list(evt.keys()):
            if k in _TOKEN_KEYS:
                evt[k] = 0
            elif isinstance(evt[k], dict):
                _zero_tokens(evt[k], depth + 1)
            elif isinstance(evt[k], list):
                for item in evt[k]:
                    if isinstance(item, dict):
                        _zero_tokens(item, depth + 1)


class SSEStreamFixer:
    """Buffer partial SSE lines and emit properly formatted events.

    The raw upstream stream only has ``data:`` lines.  The Anthropic SDK
    requires ``event: <type>`` before each ``data:`` line and a blank line
    after.  This class processes data incrementally (line-buffered) so it
    works with streaming chunks that may split across line boundaries.

    Also zeroes all token counts and ensures ``usage`` is always present
    on ``message_start`` and ``message_delta`` events.
    """

    def __init__(self):
        self._buffer = ""

    def feed(self, data: bytes) -> bytes:
        """Feed raw bytes, return fixed SSE bytes."""
        self._buffer += data.decode("utf-8", errors="replace")
        output: list[str] = []
        while "\n" in self._buffer:
            line, self._buffer = self._buffer.split("\n", 1)
            stripped = line.strip()
            if not stripped:
                output.append("\n")
                continue
            if stripped.startswith("data: "):
                data_str = stripped[6:].strip()
                try:
                    evt = json.loads(data_str)
                    evt_type = evt.get("type")
                    if evt_type:
                        output.append(f"event: {evt_type}\n")
                    _zero_tokens(evt)
                    _ensure_usage(evt)
                    output.append(f"data: {json.dumps(evt)}\n")
                except (json.JSONDecodeError, AttributeError, TypeError):
                    output.append(stripped + "\n")
                output.append("\n")  # Blank line terminates SSE event
            else:
                output.append(line + "\n")
        return "".join(output).encode("utf-8") if output else b""

    def flush(self) -> bytes:
        """Flush remaining partial line."""
        if self._buffer.strip():
            return self.feed(b"\n")
        self._buffer = ""
        return b""


@app.post("/v1/messages")
@app.post("/v1/messages/")
async def messages(req: MessagesRequest):
    """Anthropic Messages API compatible endpoint."""
    if _is_rate_limited_server():
        remaining = _rate_limit_remaining_server()
        return JSONResponse(
            status_code=429,
            content={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": f"Rate limited. Retry after {remaining:.0f}s",
                },
            },
            headers={"Retry-After": str(int(remaining) + 1)},
        )

    if req.stream is not False:
        request_id = uuid.uuid4().hex[:16]

        async def _keepalive_stream():
            # Pre-generate capture_id so we know the stream file path
            capture_id = str(uuid.uuid4())
            stream_path = os.path.join(
                CAPTURE_DIR, f"{capture_id}.raw.stream"
            )
            done_path = stream_path + ".done"

            loop = asyncio.get_event_loop()
            future = loop.run_in_executor(
                _executor,
                lambda: _run_and_capture(
                    req,
                    capture_id=capture_id,
                    skip_stream_cleanup=True,
                ),
            )
            task = asyncio.ensure_future(future)

            # Incremental SSE streaming: tail-read the stream file
            fixer = SSEStreamFixer()
            read_pos = 0
            stream_started = False
            last_yield_time = time.monotonic()

            while not task.done():
                # Check stream file for new data
                new_data = b""
                if os.path.exists(stream_path):
                    try:
                        fsize = os.path.getsize(stream_path)
                        if fsize > read_pos:
                            with open(stream_path, "rb") as sf:
                                sf.seek(read_pos)
                                new_data = sf.read()
                            read_pos += len(new_data)
                    except (OSError, IOError):
                        pass

                if new_data:
                    stream_started = True
                    fixed = fixer.feed(new_data)
                    if fixed:
                        yield fixed
                        last_yield_time = time.monotonic()
                    continue  # Check for more data immediately

                # No new data — send keepalive or sleep briefly
                now = time.monotonic()
                if now - last_yield_time > KEEPALIVE_INTERVAL:
                    yield b": keepalive\n\n"
                    last_yield_time = now

                try:
                    await asyncio.wait_for(
                        asyncio.shield(task),
                        timeout=STREAM_POLL_INTERVAL,
                    )
                except asyncio.TimeoutError:
                    continue
                except asyncio.CancelledError:
                    return

            # Task finished — get result for error handling
            try:
                status_code, content_type, raw = task.result()
            except TimeoutError as e:
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'timeout_error', 'message': str(e)}})}\n\n".encode()
                return
            except (RuntimeError, ValueError, Exception) as e:
                etype = (
                    "api_error"
                    if isinstance(e, RuntimeError)
                    else "invalid_request_error"
                    if isinstance(e, ValueError)
                    else "api_error"
                )
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': etype, 'message': str(e)}})}\n\n".encode()
                return

            if status_code == 429:
                retry_after = None
                try:
                    body = json.loads(raw)
                    ra = body.get("retry_after") or body.get("Retry-After")
                    if ra:
                        retry_after = float(ra)
                except Exception:
                    pass
                _enter_rate_limit_server(retry_after)
                yield f"event: error\ndata: {json.dumps({'type': 'error', 'error': {'type': 'rate_limit_error', 'message': 'Rate limited by upstream gateway.'}})}\n\n".encode()
                return

            # Read any remaining data from stream file
            if os.path.exists(stream_path):
                try:
                    fsize = os.path.getsize(stream_path)
                    if fsize > read_pos:
                        with open(stream_path, "rb") as sf:
                            sf.seek(read_pos)
                            remaining = sf.read()
                        if remaining:
                            stream_started = True
                            fixed_remaining = fixer.feed(remaining)
                            if fixed_remaining:
                                yield fixed_remaining
                except (OSError, IOError):
                    pass

            # Flush any buffered partial line
            flushed = fixer.flush()
            if flushed:
                yield flushed

            # Fallback: if stream file never appeared, use complete response
            if not stream_started:
                fixed = _fix_sse_events(raw)
                for line in fixed.decode(
                    "utf-8", errors="replace"
                ).splitlines(keepends=True):
                    yield (
                        line.encode("utf-8")
                        if isinstance(line, str)
                        else line
                    )
            yield b"\n"

            # Log conversation to Flask dashboard (background, non-blocking)
            if status_code == 200:
                try:
                    log_raw = raw if not stream_started else b""
                    if stream_started and os.path.exists(stream_path.replace(".stream", ".raw")):
                        pass
                    parsed = json.loads(_sse_to_messages_response(raw))
                    resp_text = ""
                    resp_tools = []
                    for block in parsed.get("content", []):
                        if block.get("type") == "text":
                            resp_text += block.get("text", "")
                        elif block.get("type") == "tool_use":
                            resp_tools.append(block)
                    threading.Thread(
                        target=_log_conversation,
                        args=(req, resp_text, resp_tools),
                        daemon=True,
                    ).start()
                except Exception:
                    pass

            # Clean up stream files
            for p in (stream_path, done_path):
                try:
                    os.unlink(p)
                except (OSError, FileNotFoundError):
                    pass

        return StreamingResponse(
            _keepalive_stream(),
            status_code=200,
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-store",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
                "x-request-id": request_id,
            },
        )

    # Non-streaming path
    loop = asyncio.get_event_loop()
    try:
        status_code, content_type, raw = await loop.run_in_executor(
            _executor, _run_and_capture, req
        )
    except TimeoutError as e:
        return _anthropic_error(504, str(e))
    except RuntimeError as e:
        return _anthropic_error(502, str(e))
    except ValueError as e:
        return _anthropic_error(400, str(e))
    except Exception as e:
        return _anthropic_error(500, f"Internal error: {e}")

    if status_code == 429:
        retry_after = None
        try:
            body = json.loads(raw)
            ra = body.get("retry_after") or body.get("Retry-After")
            if ra:
                retry_after = float(ra)
        except Exception:
            pass
        _enter_rate_limit_server(retry_after)
        return JSONResponse(
            status_code=429,
            content={
                "type": "error",
                "error": {
                    "type": "rate_limit_error",
                    "message": "Rate limited by upstream gateway. All queued requests purged.",
                },
            },
            headers={
                "Retry-After": str(int(retry_after or RATE_LIMIT_DEFAULT_COOLDOWN))
            },
        )

    # Log conversation to Flask dashboard (background, non-blocking)
    if status_code == 200:
        try:
            parsed = json.loads(_sse_to_messages_response(raw))
            resp_text = ""
            resp_tools = []
            for block in parsed.get("content", []):
                if block.get("type") == "text":
                    resp_text += block.get("text", "")
                elif block.get("type") == "tool_use":
                    resp_tools.append(block)
            threading.Thread(
                target=_log_conversation,
                args=(req, resp_text, resp_tools),
                daemon=True,
            ).start()
        except Exception:
            pass

    request_id = uuid.uuid4().hex[:16]
    return Response(
        content=_sse_to_messages_response(raw),
        status_code=status_code,
        media_type="application/json",
        headers={"x-request-id": request_id},
    )


@app.post("/v1/messages/count_tokens")
async def count_tokens(request: Request):
    """Stub for count_tokens — return a reasonable estimate."""
    try:
        body = await request.json()
        text_len = len(json.dumps(body.get("messages", [])))
        estimated = max(1, text_len // 4)
    except Exception:
        estimated = 1000
    return {"input_tokens": estimated}


# ---------------------------------------------------------------------------
# Health / readiness
# ---------------------------------------------------------------------------


@app.get("/health")
async def health():
    proxy_up = False
    try:
        import socket

        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(1)
        s.connect(("127.0.0.1", 8080))
        s.close()
        proxy_up = True
    except OSError:
        pass

    rate_limited = _is_rate_limited_server()
    return {
        "status": "rate_limited"
        if rate_limited
        else ("ok" if proxy_up else "degraded"),
        "proxy": "up" if proxy_up else "down",
        "proxy_url": PROXY_URL,
        "capture_dir": CAPTURE_DIR,
        "version": app.version,
        "rate_limited": rate_limited,
        "rate_limit_remaining_s": round(_rate_limit_remaining_server(), 1),
    }


# ---------------------------------------------------------------------------
# Graceful shutdown
# ---------------------------------------------------------------------------


@app.on_event("shutdown")
async def shutdown():
    """Clean up lock file on graceful shutdown."""
    try:
        os.unlink(CLI_LOCK_FILE)
    except OSError:
        pass


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn

    uvicorn.run(
        app,
        host="0.0.0.0",
        port=8000,
        timeout_keep_alive=600,
        timeout_graceful_shutdown=30,
    )
