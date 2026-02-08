"""
mitmproxy script to intercept and modify rovodev credits check response.

Usage:
    mitmproxy -s intercept.py -p 8080

Then run acli with proxy:
    HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev
"""

import gzip
import json
import os
import time
import socket
import threading
from mitmproxy import http
from mitmproxy import ctx

# Set socket timeout to 10 minutes to prevent connection drops
socket.setdefaulttimeout(600)

# =============================================================================
# STREAMING MODE - Critical for long responses with keepalive support
# =============================================================================
# By default mitmproxy buffers the entire response before sending to client.
# For long streaming SSE responses, acli times out waiting.
# This enables streaming mode AND injects keepalive comments during long pauses.
#
# The callable receives bytes and must RETURN bytes (not yield!).
# It's called for each chunk, and once at the end with empty bytes b"".
#
# CRITICAL: When upstream (Claude) is "thinking" for minutes without sending
# data, we inject SSE comment lines (: keepalive\n\n) to keep the connection alive.

# Global dict to store streamed chunks per flow ID
_stream_buffers = {}
_stream_lock = threading.Lock()

# Track active streams for keepalive injection
_active_streams = {}  # flow_id -> {"queue": queue.Queue, "stop": threading.Event, "thread": Thread}


class KeepaliveInjector:
    """Background thread that injects keepalive messages when stream is idle."""

    KEEPALIVE_INTERVAL = 10  # Check every 10 seconds
    KEEPALIVE_MESSAGE = b": keepalive\n\n"  # SSE comment (ignored by parsers)

    def __init__(self, flow_id: str, capture: "StreamingCapture"):
        self.flow_id = flow_id
        self.capture = capture
        self.stop_event = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)

    def start(self):
        self.thread.start()
        print(f"[KEEPALIVE] Started keepalive thread for flow {self.flow_id[:12]}...")

    def stop(self):
        self.stop_event.set()

    def _run(self):
        """Background thread that monitors for idle periods."""
        while not self.stop_event.is_set():
            # Sleep in small increments to respond quickly to stop
            for _ in range(self.KEEPALIVE_INTERVAL * 10):  # 100ms increments
                if self.stop_event.is_set():
                    return
                time.sleep(0.1)

            # Check if we should inject keepalive
            if not self.stop_event.is_set():
                elapsed = time.time() - self.capture.last_chunk_time
                if elapsed > self.KEEPALIVE_INTERVAL and self.capture.total_bytes > 0:
                    # Mark that we need to inject keepalive on next chunk
                    self.capture.pending_keepalive = True
                    print(
                        f"[KEEPALIVE] Flagging keepalive needed (idle {elapsed:.1f}s)"
                    )


class StreamingCapture:
    """Callable that captures response chunks while passing them through.

    Also injects SSE keepalive comments if upstream is silent for too long,
    preventing the client (acli) from timing out.
    """

    KEEPALIVE_MESSAGE = b": keepalive\n\n"  # SSE comment (ignored by parsers)

    def __init__(self, flow_id: str, stream_path: str = None):
        self.flow_id = flow_id
        self.chunks = []
        self.last_chunk_time = time.time()
        self.keepalives_sent = 0
        self.total_bytes = 0
        self.pending_keepalive = False  # Set by background thread
        self.stream_path = stream_path
        self.injector = KeepaliveInjector(flow_id, self)
        self.injector.start()
        # Create/truncate stream file for incremental reading by rovodev_server.py
        if self.stream_path:
            os.makedirs(os.path.dirname(self.stream_path), exist_ok=True)
            open(self.stream_path, "wb").close()

    def __call__(self, chunk: bytes) -> bytes:
        """Called for each chunk of response data.

        Args:
            chunk: Response data bytes. Empty bytes b"" signals end of stream.

        Returns:
            The chunk bytes (possibly prefixed with keepalive) to pass to client.
        """
        now = time.time()
        output = b""

        # Check if background thread flagged us for keepalive injection
        if self.pending_keepalive:
            output = self.KEEPALIVE_MESSAGE
            self.keepalives_sent += 1
            self.pending_keepalive = False
            elapsed = now - self.last_chunk_time
            print(
                f"[STREAM] Injected keepalive after {elapsed:.1f}s silence (total: {self.keepalives_sent})"
            )

        if chunk:
            self.chunks.append(chunk)
            self.total_bytes += len(chunk)
            self.last_chunk_time = now
            # Log each chunk live so we can see streaming in action
            preview = chunk[:100].decode("utf-8", errors="replace").replace("\n", "\\n")
            print(
                f"[CHUNK] +{len(chunk)} bytes (total: {self.total_bytes}) | {preview}..."
            )
            # Append chunk to stream file for real-time reading by rovodev_server.py
            if self.stream_path:
                try:
                    with open(self.stream_path, "ab") as f:
                        f.write(chunk)
                        f.flush()
                        os.fsync(f.fileno())
                except Exception as e:
                    print(f"[STREAM] Error appending to stream file: {e}")
            output += chunk
        else:
            # End of stream - stop keepalive thread and store buffer
            self.injector.stop()
            complete = b"".join(self.chunks)
            with _stream_lock:
                _stream_buffers[self.flow_id] = complete
            # Write done sentinel so rovodev_server.py knows streaming is complete
            if self.stream_path:
                done_path = self.stream_path + ".done"
                try:
                    with open(done_path, "w") as f:
                        f.write(str(self.total_bytes))
                        f.flush()
                        os.fsync(f.fileno())
                    print(f"[STREAM] Wrote done sentinel: {done_path}")
                except Exception as e:
                    print(f"[STREAM] Error writing done sentinel: {e}")
            print(
                f"[STREAM] Captured {len(complete)} bytes for flow {self.flow_id[:12]}... (sent {self.keepalives_sent} keepalives)"
            )

        return output if output else chunk  # Pass through (empty chunk signals end)


def responseheaders(flow: http.HTTPFlow) -> None:
    """Enable streaming for Opus responses to prevent acli timeout.

    Called when response headers arrive, BEFORE body is received.
    Setting flow.response.stream to a callable enables streaming with capture.
    """
    is_opus = any(
        pattern in flow.request.pretty_url
        for pattern in [
            "anthropic.claude-opus",
            "models/claude-opus",
            "bedrock/model/anthropic.claude",
        ]
    )

    if is_opus:
        # Generate unique flow ID for this request
        flow_id = f"{id(flow)}_{time.time()}"
        flow._stream_flow_id = flow_id  # type: ignore[attr-defined]

        # Derive stream file path for incremental streaming to rovodev_server.py
        stream_path = None
        if getattr(flow, "_capture_raw", False):
            capture_next_file = getattr(flow, "_capture_next_file", None)
            if capture_next_file and os.path.exists(capture_next_file):
                try:
                    with open(capture_next_file, "r") as f:
                        capture_info = json.load(f)
                    raw_path = capture_info.get("raw_path", "")
                    if raw_path:
                        stream_path = raw_path + ".stream"
                        print(f"[STREAM] Incremental streaming to {stream_path}")
                except Exception as e:
                    print(f"[STREAM] Could not read capture_next for stream path: {e}")

        # Enable streaming with capture callback (includes keepalive support)
        if flow.response:
            flow.response.stream = StreamingCapture(flow_id, stream_path=stream_path)  # type: ignore[attr-defined]
        print(
            f"[STREAM] Enabled streaming for Opus response: {flow.request.pretty_url[:80]}..."
        )


# Log file for all captured requests (general)
LOG_FILE = "/Users/toprakyagcioglu/.rovodev/logs/mitmproxy.log"

BEDROCK_PROXY_LOG = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
BEDROCK_ENDPOINT = "api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-opus-4-6-v1/invoke-with-response-stream"


def _log_bedrock_pair(
    flow: http.HTTPFlow, original_body: dict, modified_body: dict
) -> None:
    """Log both unmodified and modified request bodies for the Bedrock Opus endpoint."""
    with open(BEDROCK_PROXY_LOG, "a") as f:
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        f.write(f"\n{'=' * 100}\n")
        f.write(f"BEDROCK PROXY REQUEST - {ts}\n")
        f.write(f"URL: {flow.request.pretty_url}\n")
        f.write(f"Method: {flow.request.method}\n")
        f.write(f"Headers:\n")
        for header, value in flow.request.headers.items():
            if header.lower() in ["authorization", "x-api-key"]:
                f.write(f"  {header}: [REDACTED]\n")
            else:
                f.write(f"  {header}: {value}\n")

        f.write(f"\n{'-' * 50}\n")
        f.write(f"UNMODIFIED REQUEST BODY:\n")
        f.write(f"{'-' * 50}\n")
        f.write(json.dumps(original_body, indent=2))
        f.write("\n")

        f.write(f"\n{'-' * 50}\n")
        f.write(f"MODIFIED REQUEST BODY:\n")
        f.write(f"{'-' * 50}\n")
        f.write(json.dumps(modified_body, indent=2))
        f.write("\n")
        f.write(f"{'=' * 100}\n")


TARGET_VERSION = "0.13.39"

# Path to system prompt file
SYSTEM_PROMPT_FILE = "/Users/toprakyagcioglu/.rovodev/SYSTEM_PROMPT_CLAUDE.md"

# Opus model URL patterns to intercept
OPUS_MODEL_URL = (
    "api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-opus-4-6"
)
OPUS_4_5_MODEL_ID = "anthropic.claude-opus-4-5-20251101-v1:0"
OPUS_4_6_MODEL_ID = "anthropic.claude-opus-4-6-v1:0"

# Opus 4.6 via Google/Vertex proxy URL pattern
OPUS_4_6_VERTEX_URL = "api.atlassian.com/rovodev/v2/proxy/ai/v1/google/v1/publishers/anthropic/models/claude-opus-4-6"

# Max tokens for Opus 4.6 requests
OPUS_4_6_MAX_TOKENS = 8192
REDIRECT_OPUS_4_5_TO_4_6 = False
# =============================================================================
# INTERCEPT MODE CONFIGURATION
# =============================================================================
# Set to "TEST" for simple test prompt, "FULL" for full system prompt from file
INTERCEPT_MODE = "FULL"  # Options: "TEST", "FULL"

# Test prompt used in TEST mode
TEST_PROMPT = "You are a helpful coding assistant. Help the user with their software engineering tasks."

# Set to True to redirect opus-4-5 requests to opus-4-6
HAIKU_TO_OPUS = True
# =============================================================================

# Cache for loaded prompt and tools
_cached_system_prompt = None
_cached_tools = None

# =============================================================================
# RELAY SERVER - Accept external requests and inject into CLI flow
# =============================================================================
import threading
from http.server import HTTPServer, BaseHTTPRequestHandler
import io

# Stores the last captured auth headers + body template from a successful CLI request
_captured_context = {
    "headers": None,
    "body_template": None,
    "ready": False,
}

# =============================================================================
# RATE LIMIT (429) STATE
# =============================================================================
_rate_limit_state = {
    "active": False,
    "until": 0.0,
}
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_DEFAULT_COOLDOWN = 60  # seconds fallback if no Retry-After header


def _enter_rate_limit(retry_after: float | None = None):
    """Enter rate-limited state: purge all queued requests and block new ones."""
    cooldown = retry_after if retry_after else RATE_LIMIT_DEFAULT_COOLDOWN
    with RATE_LIMIT_LOCK:
        _rate_limit_state["active"] = True
        _rate_limit_state["until"] = time.time() + cooldown
    print(f"[RATE_LIMIT] 429 received — entering cooldown for {cooldown:.0f}s")
    _purge_all_queues()


def _is_rate_limited() -> bool:
    """Check whether we are currently in a rate-limited cooldown period."""
    with RATE_LIMIT_LOCK:
        if not _rate_limit_state["active"]:
            return False
        if time.time() >= _rate_limit_state["until"]:
            _rate_limit_state["active"] = False
            remaining = 0
            print("[RATE_LIMIT] Cooldown expired — accepting requests again")
            return False
        return True


def _rate_limit_remaining() -> float:
    """Return seconds remaining in cooldown, or 0 if not rate-limited."""
    with RATE_LIMIT_LOCK:
        if not _rate_limit_state["active"]:
            return 0.0
        remaining = _rate_limit_state["until"] - time.time()
        return max(0.0, remaining)


def _purge_all_queues():
    """Delete every queue file (relay_queue.*, capture_next.*) to stop pending work."""
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


# Queue for external messages to inject
QUEUE_DIR = "/Users/toprakyagcioglu/.rovodev/queue"
RELAY_RESPONSE_FILE = "/Users/toprakyagcioglu/.rovodev/relay_response.jsonl"
RAW_RESPONSE_DIR = "/Users/toprakyagcioglu/.rovodev/raw_responses"
RELAY_PORT = 9999


def _get_queue_files(flow=None):
    """Get request-specific queue filenames.

    Looks for the request ID in:
      1. X-Rovodev-Request-Id header on the flow (set by rovodev_server.py via env -> CLI)
      2. Fallback: scan QUEUE_DIR for the newest relay_queue file
    """
    request_id = None

    # Prefer the header injected by the CLI subprocess
    if flow is not None:
        request_id = flow.request.headers.get("X-Rovodev-Request-Id")
        if request_id:
            print(f"[QUEUE] Found request ID from header: {request_id[:12]}...")

    if not request_id:
        request_id = os.environ.get("ROVODEV_REQUEST_ID")
        if request_id:
            print(f"[QUEUE] Found request ID from env: {request_id[:12]}...")

    if request_id:
        relay_queue = os.path.join(QUEUE_DIR, f"relay_queue.{request_id}.json")
        capture_next = os.path.join(QUEUE_DIR, f"capture_next.{request_id}.json")
        print(
            f"[QUEUE] relay_queue exists={os.path.exists(relay_queue)}, capture_next exists={os.path.exists(capture_next)}"
        )
        return relay_queue, capture_next

    # Fallback: scan for newest queue file
    try:
        files = os.listdir(QUEUE_DIR)
        queue_files = [
            f for f in files if f.startswith("relay_queue.") and f.endswith(".json")
        ]
        print(
            f"[QUEUE] Fallback scan: found {len(queue_files)} queue files in {QUEUE_DIR}"
        )
        if queue_files:
            queue_files.sort(
                key=lambda x: os.path.getmtime(os.path.join(QUEUE_DIR, x)), reverse=True
            )
            request_id = queue_files[0].replace("relay_queue.", "").replace(".json", "")
            relay_queue = os.path.join(QUEUE_DIR, queue_files[0])
            capture_next = os.path.join(QUEUE_DIR, f"capture_next.{request_id}.json")
            print(f"[QUEUE] Using newest queue file: {queue_files[0]}")
            return relay_queue, capture_next
    except Exception as e:
        print(f"[QUEUE] Fallback scan error: {e}")

    print(f"[QUEUE] No queue files found")
    return None, None


class RelayHandler(BaseHTTPRequestHandler):
    """Simple HTTP handler that accepts Opus requests and forwards them."""

    def log_message(self, format, *args):
        print(f"[RELAY] {format % args}")

    def do_POST(self):
        content_length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(content_length)

        try:
            data = json.loads(body)
        except json.JSONDecodeError:
            self.send_response(400)
            self.end_headers()
            self.wfile.write(b'{"error": "Invalid JSON"}')
            return

        if _is_rate_limited():
            remaining = _rate_limit_remaining()
            self.send_response(429)
            self.send_header("Retry-After", str(int(remaining) + 1))
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {"error": f"Rate limited. Retry after {remaining:.0f}s"}
                ).encode()
            )
            return

        if not _captured_context["ready"]:
            self.send_response(503)
            self.end_headers()
            self.wfile.write(
                b'{"error": "No CLI context captured yet. Make a request with the CLI first."}'
            )
            return

        # Write the request to a unique queue file for the proxy to pick up
        import uuid

        request_id = str(uuid.uuid4())
        relay_queue_file = os.path.join(QUEUE_DIR, f"relay_queue.{request_id}.json")
        capture_next_file = os.path.join(QUEUE_DIR, f"capture_next.{request_id}.json")
        os.makedirs(QUEUE_DIR, exist_ok=True)
        with open(relay_queue_file, "w") as f:
            json.dump(data, f)

        print(f"[RELAY] Queued message, waiting for CLI to make next request...")

        # Wait for response (poll the response file)
        # Clear old response
        if os.path.exists(RELAY_RESPONSE_FILE):
            os.remove(RELAY_RESPONSE_FILE)

        # Signal that we have a queued request
        _captured_context["has_queued"] = True

        # Wait up to 600s for response (10 minutes)
        for _ in range(12000):
            if os.path.exists(RELAY_RESPONSE_FILE):
                try:
                    with open(RELAY_RESPONSE_FILE, "r") as f:
                        response_data = f.read()
                    if response_data:  # Only proceed if file has content
                        self.send_response(200)
                        self.send_header("Content-Type", "text/event-stream")
                        self.end_headers()
                        self.wfile.write(response_data.encode())
                        os.remove(RELAY_RESPONSE_FILE)
                        _captured_context["has_queued"] = False
                        return
                except (IOError, OSError):
                    pass  # File might still be written, retry
            time.sleep(0.05)

        self.send_response(504)
        self.end_headers()
        self.wfile.write(b'{"error": "Timeout waiting for response"}')
        _captured_context["has_queued"] = False

    def do_GET(self):
        """Health check / status endpoint."""
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.end_headers()
        status = {
            "ready": _captured_context["ready"],
            "has_queued": _captured_context.get("has_queued", False),
            "rate_limited": _is_rate_limited(),
            "rate_limit_remaining_s": round(_rate_limit_remaining(), 1),
        }
        self.wfile.write(json.dumps(status).encode())


def _start_relay_server():
    server = HTTPServer(("127.0.0.1", RELAY_PORT), RelayHandler)
    print(f"[RELAY] Server listening on http://127.0.0.1:{RELAY_PORT}")
    server.serve_forever()


# Start relay server in background thread
_relay_thread = threading.Thread(target=_start_relay_server, daemon=True)
_relay_thread.start()
# =============================================================================

# Ensure raw response directory exists
os.makedirs(RAW_RESPONSE_DIR, exist_ok=True)


def redirect_opus_4_5_to_4_6(flow: http.HTTPFlow) -> bool:
    """
    Redirect opus-4-5 requests to opus-4-6.

    Returns True if the request was redirected, False otherwise.
    """
    if OPUS_4_5_MODEL_ID not in flow.request.pretty_url:
        return False

    # Replace model ID in URL
    new_url = flow.request.pretty_url.replace(OPUS_4_5_MODEL_ID, OPUS_4_6_MODEL_ID)
    flow.request.url = new_url
    print(f"[OPUS] Redirected {OPUS_4_5_MODEL_ID} -> {OPUS_4_6_MODEL_ID}")
    return True


def load_system_prompt_and_tools():
    """Load system prompt and tools from the markdown file."""
    global _cached_system_prompt, _cached_tools

    # Disable cache - always reload file
    # if _cached_system_prompt is not None:
    #     return _cached_system_prompt, _cached_tools

    try:
        with open(SYSTEM_PROMPT_FILE, "r") as f:
            content = f.read()

        # Split at the tools JSON section
        if "```json" in content:
            parts = content.split("```json")
            system_prompt = parts[0].strip()

            # Remove the "## Available Tools" header and trailing ---
            if "---" in system_prompt:
                system_prompt = system_prompt.split("---")[0].strip()

            # Extract tools JSON
            if len(parts) > 1:
                tools_json_str = parts[1].split("```")[0].strip()
                tools_data = json.loads(tools_json_str)
                _cached_tools = tools_data.get("tools", [])
        else:
            system_prompt = content.strip()
            _cached_tools = None

        _cached_system_prompt = system_prompt
        print(
            f"[CONFIG] Loaded system prompt ({len(system_prompt)} chars) and {len(_cached_tools) if _cached_tools else 0} tools"
        )
        return _cached_system_prompt, _cached_tools

    except Exception as e:
        print(f"[ERROR] Failed to load system prompt: {e}")
        return None, None


def log_request(flow: http.HTTPFlow, direction: str) -> None:
    """Log request/response details to file"""
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'=' * 80}\n")
        f.write(f"{direction} - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"URL: {flow.request.pretty_url}\n")
        f.write(f"Method: {flow.request.method}\n")
        f.write(f"Headers:\n")
        for header, value in flow.request.headers.items():
            # Mask sensitive headers
            if header.lower() in ["authorization", "x-api-key", "cookie"]:
                f.write(f"  {header}: {value}\n")
            else:
                f.write(f"  {header}: {value}\n")

        if flow.request.content:
            f.write(f"\nRequest Body:\n")
            try:
                body = json.loads(flow.request.content)
                f.write(json.dumps(body, indent=2))
                f.write("\n")
            except:
                f.write(f"  {flow.request.content.decode('utf-8', errors='replace')}\n")


def log_response(flow: http.HTTPFlow) -> None:
    """Log response details"""
    if not flow.response:
        return
    with open(LOG_FILE, "a") as f:
        f.write(f"\nResponse Status: {flow.response.status_code}\n")
        f.write(f"Response Headers:\n")
        for header, value in flow.response.headers.items():
            f.write(f"  {header}: {value}\n")

        if flow.response.content:
            f.write(f"\nResponse Body:\n")
            try:
                body = json.loads(flow.response.content.decode("utf-8"))
                f.write(json.dumps(body, indent=2))
                f.write("\n")
            except:
                content = (
                    flow.response.content.decode("utf-8", errors="replace")
                    if flow.response.content
                    else ""
                )
                f.write(f"  {content}\n")
        f.write(f"{'=' * 80}\n")


def strip_all_cache_control(data: dict) -> int:
    """
    Remove every ``cache_control`` key from the request body.

    Covers system prompt blocks, message content blocks, AND tool definitions
    (Anthropic clients like Claude Code add cache_control to tools too).

    Returns the total number of cache_control keys removed.
    """
    removed = 0
    locations = []

    # --- system prompt ---
    system = data.get("system")
    if isinstance(system, list):
        for i, block in enumerate(system):
            if isinstance(block, dict) and "cache_control" in block:
                del block["cache_control"]
                removed += 1
                locations.append(f"system[{i}]")
    elif isinstance(system, dict) and "cache_control" in system:
        del system["cache_control"]
        removed += 1
        locations.append("system")

    # --- messages ---
    for msg_idx, msg in enumerate(data.get("messages", [])):
        if not isinstance(msg, dict):
            continue
        content = msg.get("content")
        if isinstance(content, list):
            for block_idx, block in enumerate(content):
                if isinstance(block, dict) and "cache_control" in block:
                    del block["cache_control"]
                    removed += 1
                    locations.append(f"msg[{msg_idx}].content[{block_idx}]")
        elif isinstance(content, dict) and "cache_control" in content:
            del content["cache_control"]
            removed += 1
            locations.append(f"msg[{msg_idx}].content")

    # --- tool definitions ---
    for tool_idx, tool in enumerate(data.get("tools", [])):
        if not isinstance(tool, dict):
            continue
        if "cache_control" in tool:
            del tool["cache_control"]
            removed += 1
            locations.append(f"tool[{tool_idx}]")
        # Also check nested input_schema
        schema = tool.get("input_schema")
        if isinstance(schema, dict) and "cache_control" in schema:
            del schema["cache_control"]
            removed += 1
            locations.append(f"tool[{tool_idx}].input_schema")

    if removed > 0:
        print(
            f"[CACHE_DEBUG] Removed {removed} cache_control from: {', '.join(locations)}"
        )

    return removed


def modify_opus_request(flow: http.HTTPFlow, label: str) -> None:
    """
    Common logic to modify an Opus model request (4.5 Bedrock or 4.6 Vertex).
    Injects custom system prompt and sets max_tokens.
    Also handles relay: if a queued request exists, swaps the body entirely.
    """
    if not flow.request.content:
        return

    try:
        content = flow.request.content
        flow.request.pretty_url.replace(
            flow.request.pretty_url,
            "https://api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-opus-4-6-v1/invoke",
        )

        # Decompress if gzip
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)

        data = json.loads(content.decode("utf-8"))

        import copy

        _original_body = (
            copy.deepcopy(data) if BEDROCK_ENDPOINT in flow.request.pretty_url else None
        )

        # Capture auth context for relay
        _captured_context["headers"] = dict(flow.request.headers)
        _captured_context["body_template"] = {
            k: v for k, v in data.items() if k not in ("messages", "system")
        }
        _captured_context["ready"] = True

        # Check if there's a queued relay request (from relay server or API server)
        is_relay = False
        RELAY_QUEUE_FILE, CAPTURE_NEXT_FILE = _get_queue_files(flow)
        if RELAY_QUEUE_FILE and os.path.exists(RELAY_QUEUE_FILE):
            print(f"[{label}] Found queue file: {RELAY_QUEUE_FILE}")
            try:
                with open(RELAY_QUEUE_FILE, "r") as f:
                    relay_data = json.load(f)
                print(
                    f"[{label}] Loaded relay data with keys: {list(relay_data.keys())}"
                )
                # Only consume the queue file if we successfully process it
                # This prevents losing the relay data if the first request is not a model invocation

                # Use Amp's own system prompt, tools, and messages as-is
                for key, value in relay_data.items():
                    data[key] = value

                # --- Hard sanitize after relay merge ---
                # The CLI's original body may contain fields (e.g. thinking,
                # stream, temperature, model) that relay_data doesn't overwrite
                # because _build_anthropic_body already popped them.  The
                # Atlassian Bedrock gateway returns 403 for unknown fields.
                _ALLOWED_TOP_LEVEL = {
                    "anthropic_version",
                    "max_tokens",
                    "messages",
                    "system",
                    "tools",
                    "top_p",
                    "top_k",
                    "stop_sequences",
                }
                _removed_keys = []
                for k in list(data.keys()):
                    if k not in _ALLOWED_TOP_LEVEL:
                        _removed_keys.append(k)
                        del data[k]
                if _removed_keys:
                    print(
                        f"[{label}] RELAY: Stripped disallowed top-level keys: {_removed_keys}"
                    )

                # Deep-strip cache_control everywhere (Bedrock rejects it)
                def _deep_strip_cache_control(obj):
                    if isinstance(obj, dict):
                        obj.pop("cache_control", None)
                        for v in obj.values():
                            _deep_strip_cache_control(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            _deep_strip_cache_control(item)

                _deep_strip_cache_control(data)

                print(
                    f"[{label}] RELAY: Using client data — "
                    f"msgs={len(data.get('messages', []))}, "
                    f"tools={len(data.get('tools', []))}, "
                    f"system_blocks={len(data.get('system', [])) if isinstance(data.get('system'), list) else 'str'}"
                )

                # Keep all system blocks as-is (amp_intercept.py already prepared them)
                print(
                    f"[{label}] RELAY: Keeping {len(data.get('system', []))} system blocks as-is"
                )

                # Strip ttl from cache_control (Bedrock only accepts {"type": "ephemeral"})
                def _strip_ttl(obj):
                    if isinstance(obj, dict):
                        cc = obj.get("cache_control")
                        if isinstance(cc, dict) and "ttl" in cc:
                            del cc["ttl"]
                        for v in obj.values():
                            _strip_ttl(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            _strip_ttl(item)

                _strip_ttl(data)

                # Strip cache_control from system/messages (causes 400 on Bedrock)
                for block in data.get("system", []):
                    if isinstance(block, dict) and "cache_control" in block:
                        del block["cache_control"]
                for msg in data.get("messages", []):
                    content = msg.get("content", [])
                    if isinstance(content, list):
                        for block in content:
                            if isinstance(block, dict) and "cache_control" in block:
                                del block["cache_control"]
                print(f"[{label}] RELAY: Stripped cache_control/ttl")

                # Merge original RovoDev tools into relay tools.
                # The Atlassian gateway REQUIRES standard RovoDev tool names
                # (open_files, create_file, delete_file, etc.) to be present
                # in the request, otherwise it returns 403.
                # We keep AMP tools as-is and append RovoDev tools that aren't
                # already in the set.
                if _original_body:
                    original_tools = _original_body.get("tools", [])
                    if "tools" not in data or not data["tools"]:
                        # If relay has no tools, use original tools entirely
                        data["tools"] = copy.deepcopy(original_tools)
                        # Strip cache_control from copied tools
                        for tool in data["tools"]:
                            tool.pop("cache_control", None)
                        print(
                            f"[{label}] RELAY: Using original RovoDev tools ({len(data['tools'])})"
                        )
                    else:
                        # Merge: keep relay tools and add missing original tools
                        relay_tool_names = {
                            t.get("name") for t in data["tools"] if isinstance(t, dict)
                        }
                        added_names = []
                        for tool in original_tools:
                            if (
                                isinstance(tool, dict)
                                and tool.get("name") not in relay_tool_names
                            ):
                                tool_copy = copy.deepcopy(tool)
                                tool_copy.pop("cache_control", None)
                                data["tools"].append(tool_copy)
                                added_names.append(tool.get("name"))
                                relay_tool_names.add(tool.get("name"))
                        if added_names:
                            print(
                                f"[{label}] RELAY: Merged {len(added_names)} RovoDev tools: {added_names}"
                            )
                    print(
                        f"[{label}] RELAY: Total tools after merge: {len(data.get('tools', []))}"
                    )
                else:
                    print(f"[{label}] RELAY: No original body to merge tools from")

                # Mark this flow as a relay flow so we capture the response
                flow.relay_mode = True  # type: ignore[attr-defined]
                flow._capture_raw = True  # type: ignore[attr-defined]
                flow._capture_next_file = CAPTURE_NEXT_FILE  # type: ignore[attr-defined]
                os.remove(RELAY_QUEUE_FILE)
                print(f"[{label}] RELAY: Injected queued message into CLI request")
                print(
                    f"[{label}] RELAY: _capture_raw={flow._capture_raw}, _capture_next_file={CAPTURE_NEXT_FILE}"  # type: ignore[attr-defined]
                )
                is_relay = True

            except Exception as e:
                # Clean up queue file on error to prevent stale files
                try:
                    if RELAY_QUEUE_FILE and os.path.exists(RELAY_QUEUE_FILE):
                        os.remove(RELAY_QUEUE_FILE)
                        print(f"[{label}] RELAY: Cleaned up queue file after error")
                except Exception:
                    pass
                print(f"[{label}] RELAY: Failed to load queue: {e}")
        elif RELAY_QUEUE_FILE:
            print(f"[{label}] Queue file not found at: {RELAY_QUEUE_FILE}")
        else:
            print(f"[{label}] No queue file configured (ROVODEV_REQUEST_ID not set)")

        # Normal mode - apply system prompt modifications
        # SKIP for relay requests — they already have the right tools/system
        if not is_relay:
            custom_prompt, custom_tools = load_system_prompt_and_tools()

            if INTERCEPT_MODE == "TEST":
                active_prompt = TEST_PROMPT
                print(f"[{label}] Using TEST mode prompt")
            else:
                active_prompt = custom_prompt
                print(f"[{label}] Using FULL mode (custom system prompt)")

            if active_prompt and "system" in data:
                import re as _re

                active_prompt = _re.sub(
                    r'https?://[^\s\)\]\}\>"\'`,]+', "", active_prompt
                )
                active_prompt = active_prompt.replace("ampcode", "opencode").replace(
                    "Ampcode", "Opencode"
                )
                active_prompt = _re.sub(r"\bAmp\b", "opencode", active_prompt)
                active_prompt = _re.sub(r"\bamp\b", "opencode", active_prompt)
                if isinstance(data["system"], list):
                    data["system"] = [
                        {
                            "type": "text",
                            "text": active_prompt,
                            "cache_control": {"type": "ephemeral"},
                        }
                    ]
                else:
                    data["system"] = active_prompt
                print(
                    f"[{label}] Replaced system prompt ({len(active_prompt)} chars, URLs stripped)"
                )

            if custom_tools and "tools" in data:
                print(f"[{label}] Keeping original tools (count: {len(data['tools'])})")

            # Ensure cache_control breakpoints match the original request structure.
            # The Atlassian Bedrock gateway expects cache_control on:
            #   1. system[0]  (done above)
            #   2. Bash tool at index 8 (exact copy from unmodified request)
            #   3. The last tool definition
            #   4. The last content block of the last message
            _UNMOD_BASH_TOOL = {
                "name": "bash",
                "description": "Execute a bash command on the workspace.\n\nCommands are run in the workspace root directory. Typically used to reproduce bugs or verify features are\nworking as expected. Avoid making calls that will result in very large outputs, as they may be truncated. End\ncommands with `&` to run them in the background.\n\nInteractive commands should be used sparingly and should be run in the background with `&` to avoid blocking.\n\nExample commands:\n- `git log --oneline -n 50`: Show the git log for the last 50 commits.\n- `bash minimal_reproducible_example_script.sh`: Run a bash reproduction script in the workspace.\n- `long_running_command &`: Commands ending with `&` are automatically run in background.\n- `python3 -i &`: Start an interactive Python session in background.",
                "input_schema": {
                    "properties": {
                        "command": {
                            "anyOf": [{"type": "string"}, {"type": "integer"}],
                            "title": "Command",
                            "description": 'The command to execute. Can also be an integer PID, in which case the running logs from that command\nwill be returned (excluding any logs that have been previously retrieved). Commands that end with `&` are\nautomatically run in background and their logs can be retrieved later using the PID. For interactive\nprocesses, you can send input by using a string in the format: "PID:input", where input is what to send to\nstdin.',
                        },
                        "timeout": {
                            "anyOf": [{"type": "integer"}, {"type": "null"}],
                            "default": None,
                            "title": "Timeout",
                            "description": "Optional timeout in seconds for foreground command execution. If exceeded, the command is moved to\nbackground and a warning with the PID is returned. Explicitly backgrounded commands using `&` do not use\nthis timeout for the startup window and instead use the configured startup window constant. This option is\ncapped at 600 seconds.",
                        },
                    },
                    "required": ["command"],
                    "title": "bashArguments",
                    "type": "object",
                },
                "cache_control": {"type": "ephemeral"},
            }
            tools = data.get("tools", [])
            if tools:
                # Remove any existing Bash tool
                tools[:] = [
                    t
                    for t in tools
                    if not (isinstance(t, dict) and t.get("name", "").lower() == "bash")
                ]
                # Insert exact unmodified bash tool at index 8
                target_idx = min(8, len(tools))
                tools.insert(target_idx, _UNMOD_BASH_TOOL)
                tools[-1].setdefault("cache_control", {"type": "ephemeral"})
            msgs = data.get("messages", [])
            if msgs:
                last_msg = msgs[-1]
                content = last_msg.get("content")
                if isinstance(content, list) and content:
                    content[-1].setdefault("cache_control", {"type": "ephemeral"})
        else:
            print(
                f"[{label}] RELAY: Skipping system prompt/tools replacement (using client's own)"
            )

            # Keep client's tools (already sanitized above) — do NOT replace with _original_body tools
            tools = data.get("tools", [])
            if tools:
                tools[-1].setdefault("cache_control", {"type": "ephemeral"})
            print(f"[{label}] RELAY: Keeping client tools ({len(tools)})")

            # Keep full Amp system prompt as-is, just add cache_control
            sys_blocks = data.get("system")
            if isinstance(sys_blocks, list) and sys_blocks:
                sys_blocks[0].setdefault("cache_control", {"type": "ephemeral"})

            # last message content block cache_control
            msgs = data.get("messages", [])
            if msgs:
                last_msg = msgs[-1]
                content_blocks = last_msg.get("content")
                if isinstance(content_blocks, list) and content_blocks:
                    content_blocks[-1].setdefault(
                        "cache_control", {"type": "ephemeral"}
                    )

            print(
                f"[{label}] RELAY: Tools={len(data.get('tools', []))}, cache_control set"
            )

        # Enforce max_tokens
        # Always set to our configured value
        if label in ("OPUS-4.5", "OPUS-4.6"):
            requested = data.get("max_tokens", 0)
            data["max_tokens"] = OPUS_4_6_MAX_TOKENS
            if requested != OPUS_4_6_MAX_TOKENS:
                print(
                    f"[{label}] max_tokens: {requested} -> {OPUS_4_6_MAX_TOKENS} (forced)"
                )
            else:
                print(f"[{label}] Using max_tokens: {OPUS_4_6_MAX_TOKENS}")

        # CRITICAL: Remove temperature - causes 403 errors at Atlassian gateway
        if "temperature" in data:
            print(f"[{label}] Removing temperature={data['temperature']} (causes 403)")
            data.pop("temperature")

        # Ensure anthropic_version is set - REQUIRED by both Bedrock and Vertex
        # Bedrock uses bedrock-2023-05-31, Vertex uses vertex-2023-10-16
        if "anthropic_version" not in data:
            if "bedrock" in flow.request.pretty_url:
                data["anthropic_version"] = "bedrock-2023-05-31"
            else:
                data["anthropic_version"] = "vertex-2023-10-16"
            print(
                f"[{label}] Added missing anthropic_version: {data['anthropic_version']}"
            )
        elif (
            "bedrock" in flow.request.pretty_url
            and data["anthropic_version"] == "vertex-2023-10-16"
        ):
            data["anthropic_version"] = "bedrock-2023-05-31"
            print(f"[{label}] Fixed anthropic_version for Bedrock: bedrock-2023-05-31")
        else:
            print(f"[{label}] Keeping anthropic_version: {data['anthropic_version']}")

        # Cache control blocks are preserved (removed stripping logic)
        # Previously removed due to 400 errors, but now keeping them
        pass

        # Debug: Print all top-level keys and dump full body to file
        print(f"[{label}] FINAL BODY KEYS: {list(data.keys())}")
        print(
            f"[{label}] FINAL: tools={len(data.get('tools', []))}, "
            f"msgs={len(data.get('messages', []))}, "
            f"body_size={len(json.dumps(data))}"
        )

        # Debug: Log User-Agent and other key headers
        user_agent = flow.request.headers.get("User-Agent", "NOT SET")
        x_rovodev = flow.request.headers.get("X-RovoDev-Xid", "NOT SET")
        print(f"[{label}] User-Agent: {user_agent}")
        print(f"[{label}] X-RovoDev-Xid: {x_rovodev}")

        with open("/tmp/debug_request.json", "w") as f:
            json.dump(data, f, indent=2)
        # Also dump full request with headers for comparison
        full_debug = {
            "url": flow.request.pretty_url,
            "method": flow.request.method,
            "headers": dict(flow.request.headers),
            "body": data,
            "is_relay": is_relay,
            "body_size_bytes": len(json.dumps(data, separators=(",", ":"))),
        }
        ts = time.strftime("%Y%m%d_%H%M%S")
        debug_path = f"/tmp/debug_full_request_{ts}.json"
        with open(debug_path, "w") as f:
            json.dump(full_debug, f, indent=2)
        print(f"[{label}] Full request+headers dumped to {debug_path}")

        if _original_body is not None:
            _log_bedrock_pair(flow, _original_body, data)

        # Use compact JSON encoding
        new_content = json.dumps(data, separators=(",", ":")).encode("utf-8")

        # Use set_content() which properly updates content-length
        flow.request.set_content(new_content)

        # Remove content-encoding if we decompressed
        flow.request.headers.pop("content-encoding", None)

        # Tag relay flows for raw response capture (non-relay CLI
        # requests must NOT be captured — they are the CLI's own follow-up
        # calls and would overwrite or conflict with the relay response).
        if not getattr(flow, "_capture_raw", False):
            # Only set if not already set by relay path above
            if getattr(flow, "relay_mode", False):
                flow._capture_raw = True  # type: ignore[attr-defined]

        print(f"[{label}] Request modified, size: {len(content)} -> {len(new_content)}")

    except Exception as e:
        import traceback

        print(f"[ERROR] {label} modify failed: {e}")
        traceback.print_exc()


def request(flow: http.HTTPFlow) -> None:
    """Intercept all requests"""
    # Optionally redirect opus-4-5 to opus-4-6
    if REDIRECT_OPUS_4_5_TO_4_6:
        redirect_opus_4_5_to_4_6(flow)

    is_opus_bedrock = OPUS_MODEL_URL in flow.request.pretty_url
    is_opus_vertex = OPUS_4_6_VERTEX_URL in flow.request.pretty_url
    is_opus = is_opus_bedrock or is_opus_vertex
    is_bedrock = (
        "api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/"
        in flow.request.pretty_url
    )

    # Log API requests (only once)
    if any(
        domain in flow.request.pretty_url
        for domain in ["api.atlassian.com", "anthropic.com", "bedrock", "openai.com"]
    ):
        if is_opus_bedrock:
            log_request(flow, "REQUEST (OPUS-4.5)")
        elif is_opus_vertex:
            log_request(flow, "REQUEST (OPUS-4.6)")
        else:
            log_request(flow, "REQUEST")
        print(f"[PROXY] {flow.request.method} {flow.request.pretty_url}")

    # Intercept and modify Opus Bedrock requests (4.5)
    if is_opus_bedrock:
        modify_opus_request(flow, "OPUS-4.5")

    # Intercept and modify Opus Vertex requests (4.6)
    if is_opus_vertex:
        modify_opus_request(flow, "OPUS-4.6")

    # Intercept cli/events requests and set input/output tokens to 1
    if (
        "api.atlassian.com/rovodev/v2/cli/events" in flow.request.pretty_url
        and flow.request.content
    ):
        try:
            content = flow.request.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)

            data = json.loads(content.decode("utf-8"))

            # Set input_tokens and output_tokens to 1 in attributes
            if "attributes" in data:
                original_input = data["attributes"].get("input_tokens")
                original_output = data["attributes"].get("output_tokens")

                if original_input is not None:
                    data["attributes"]["input_tokens"] = 1
                if original_output is not None:
                    data["attributes"]["output_tokens"] = 1

                # Also update gen_ai.usage fields if present
                if "gen_ai.usage.input_tokens" in data["attributes"]:
                    data["attributes"]["gen_ai.usage.input_tokens"] = 1
                if "gen_ai.usage.output_tokens" in data["attributes"]:
                    data["attributes"]["gen_ai.usage.output_tokens"] = 1

                if original_input is not None or original_output is not None:
                    print(
                        f"[INTERCEPTED] Modified tokens: input={original_input}->1, output={original_output}->1"
                    )

            # Update total_tokens if present
            if "attributes" in data and "total_tokens" in data["attributes"]:
                data["attributes"]["total_tokens"] = 2

            # Remove "TRIAL" from rovoDevEntitlement
            if "attributes" in data and "rovoDevEntitlement" in data["attributes"]:
                original = data["attributes"]["rovoDevEntitlement"]
                if "TRIAL" in original:
                    data["attributes"]["rovoDevEntitlement"] = original.replace(
                        "_TRIAL", ""
                    )
                    print(
                        f"[INTERCEPTED] Modified rovoDevEntitlement: {original} -> {data['attributes']['rovoDevEntitlement']}"
                    )

            new_content = json.dumps(data, separators=(",", ":")).encode("utf-8")
            flow.request.set_content(new_content)
            flow.request.headers.pop("content-encoding", None)

        except Exception as e:
            print(f"[ERROR] Failed to modify cli/events request: {e}")

    # Intercept batch requests and remove TRIAL from rovoDevEntitlement
    if (
        "as.atlassian.com/api/v1/batch" in flow.request.pretty_url
        and flow.request.content
    ):
        try:
            content = flow.request.content
            if content[:2] == b"\x1f\x8b":
                content = gzip.decompress(content)

            data = json.loads(content.decode("utf-8"))
            modified = False

            if "batch" in data and isinstance(data["batch"], list):
                for event in data["batch"]:
                    if "properties" in event and "attributes" in event["properties"]:
                        attrs = event["properties"]["attributes"]
                        if "rovoDevEntitlement" in attrs:
                            original = attrs["rovoDevEntitlement"]
                            if "TRIAL" in original:
                                attrs["rovoDevEntitlement"] = original.replace(
                                    "_TRIAL", ""
                                )
                                modified = True

            if modified:
                new_content = json.dumps(data, separators=(",", ":")).encode("utf-8")
                flow.request.set_content(new_content)
                flow.request.headers.pop("content-encoding", None)
                print(
                    f"[INTERCEPTED] Modified rovoDevEntitlement in batch request (removed TRIAL)"
                )

        except Exception as e:
            print(f"[ERROR] Failed to modify batch request: {e}")


def response(flow: http.HTTPFlow) -> None:
    if BEDROCK_ENDPOINT in flow.request.pretty_url and flow.response:
        with open(BEDROCK_PROXY_LOG, "a") as f:
            f.write(f"\n{'-' * 50}\n")
            f.write(f"RESPONSE - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write(f"Status: {flow.response.status_code}\n")
            f.write(f"Response Headers:\n")
            for header, value in flow.response.headers.items():
                f.write(f"  {header}: {value}\n")
            raw = flow.response.content
            if raw:
                if raw[:2] == b"\x1f\x8b":
                    try:
                        raw = gzip.decompress(raw)
                    except Exception:
                        pass
                preview = raw[:2000].decode("utf-8", errors="replace")
                f.write(f"\nResponse Body (first 2000 chars):\n{preview}\n")
            f.write(f"{'=' * 100}\n")

    # --- 429 Rate Limit detection ---
    if (
        flow.response
        and flow.response.status_code == 429
        and "api.atlassian.com" in flow.request.pretty_url
    ):
        retry_after = None
        ra_header = flow.response.headers.get("Retry-After")
        if ra_header:
            try:
                retry_after = float(ra_header)
            except (ValueError, TypeError):
                pass
        _enter_rate_limit(retry_after)

    # Capture raw Opus responses for the API server (no header injection):
    # If capture_next file exists, consume it and write the next Opus response to the specified paths.
    _cr = getattr(flow, "_capture_raw", False)
    _rm = getattr(flow, "relay_mode", False)
    _cnf = getattr(flow, "_capture_next_file", None)
    if _cr or _rm:
        print(
            f"[RESPONSE] _capture_raw={_cr}, relay_mode={_rm}, _capture_next_file={_cnf}, status={flow.response.status_code if flow.response else 'None'}"
        )
    if _cr and flow.response:
        # Use stored capture_next file from request processing, or scan for it
        capture_next_file = getattr(flow, "_capture_next_file", None)
        if not capture_next_file:
            _, capture_next_file = _get_queue_files(flow)
        if capture_next_file and os.path.exists(capture_next_file):
            try:
                with open(capture_next_file, "r") as f:
                    capture = json.load(f)
                # Delete marker first to avoid double-writes if something crashes mid-write.
                os.remove(capture_next_file)

                # Check for streamed content first (when streaming is enabled)
                # flow.response.content is empty when streaming, use captured buffer
                flow_id = getattr(flow, "_stream_flow_id", None)
                raw = None
                if flow_id:
                    with _stream_lock:
                        raw = _stream_buffers.pop(flow_id, None)
                    if raw:
                        print(f"[CAPTURE] Using streamed buffer: {len(raw)} bytes")

                # Fallback to response.content for non-streamed responses
                if not raw:
                    raw = flow.response.content

                if raw and raw[:2] == b"\x1f\x8b":
                    raw = gzip.decompress(raw)

                # Log error responses for debugging
                if flow.response.status_code >= 400:
                    try:
                        error_body = raw.decode("utf-8") if raw else ""
                        print(
                            f"[ERROR] Response {flow.response.status_code}: {error_body[:500]}"
                        )
                    except:
                        pass

                meta = {
                    "status_code": flow.response.status_code,
                    "headers": dict(flow.response.headers),
                    "timestamp": time.time(),
                    "url": flow.request.pretty_url,
                }

                raw_path = capture.get("raw_path")
                meta_path = capture.get("meta_path")
                if not raw_path or not meta_path:
                    raise ValueError("capture_next.json missing raw_path/meta_path")

                # Ensure target directories exist
                os.makedirs(os.path.dirname(raw_path), exist_ok=True)
                os.makedirs(os.path.dirname(meta_path), exist_ok=True)

                # Atomic write: write to temp files, then rename.
                # meta_path rename is the readiness signal for rovodev_server.py.
                raw_tmp = raw_path + ".tmp"
                meta_tmp = meta_path + ".tmp"
                with open(raw_tmp, "wb") as f:
                    f.write(raw if raw else b"")
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(raw_tmp, raw_path)

                with open(meta_tmp, "w") as f:
                    json.dump(meta, f)
                    f.flush()
                    os.fsync(f.fileno())
                os.rename(meta_tmp, meta_path)

                print(
                    f"[RAW] Captured next Opus response -> {raw_path} ({len(raw) if raw else 0} bytes)"
                )
            except Exception as e:
                import traceback

                print(f"[RAW] Failed to capture next response: {e}")
                traceback.print_exc()

    # Handle relay response capture
    if getattr(flow, "relay_mode", False) and flow.response:
        try:
            # Check for streamed content first
            flow_id = getattr(flow, "_stream_flow_id", None)
            raw_content = None
            if flow_id:
                with _stream_lock:
                    raw_content = _stream_buffers.pop(flow_id, None)
                if raw_content:
                    print(f"[RELAY] Using streamed buffer: {len(raw_content)} bytes")

            # Fallback to response.content
            if raw_content is None:
                raw_content = flow.response.content

            response_content = (
                raw_content.decode("utf-8", errors="replace") if raw_content else ""
            )
            # Atomic write: temp file then rename so reader never sees partial data
            tmp_path = RELAY_RESPONSE_FILE + ".tmp"
            with open(tmp_path, "w") as f:
                f.write(response_content)
                f.flush()
                os.fsync(f.fileno())
            os.rename(tmp_path, RELAY_RESPONSE_FILE)
            print(
                f"[RELAY] Captured response ({len(response_content)} bytes, status={flow.response.status_code})"
            )
        except Exception as e:
            print(f"[RELAY] Failed to capture response: {e}")

    # Handle Statsig config response - remove anthropic:claude-opus-4-6 from available models
    if (
        "api.statsig.com" in flow.request.pretty_url
        and flow.response
        and flow.response.content
    ):
        try:
            raw_content = flow.response.content
            if raw_content[:2] == b"\x1f\x8b":
                raw_content = gzip.decompress(raw_content)

            data = json.loads(raw_content.decode("utf-8"))

            # Reorder Claude Opus 4.6 available models - put bedrock first, anthropic second
            if "value" in data and "available_models" in data["value"]:
                opus_46_models = data["value"]["available_models"].get(
                    "Claude Opus 4.6", []
                )

                # Separate bedrock and anthropic models
                bedrock_models = [m for m in opus_46_models if m.startswith("bedrock:")]
                anthropic_models = [
                    m for m in opus_46_models if m.startswith("anthropic:")
                ]

                # Reorder: bedrock first, then anthropic
                reordered_models = bedrock_models + anthropic_models

                if reordered_models != opus_46_models:
                    data["value"]["available_models"]["Claude Opus 4.6"] = (
                        reordered_models
                    )
                    print(
                        f"[INTERCEPTED] Reordered Opus 4.6 models: bedrock first, anthropic second"
                    )

            new_content = json.dumps(data).encode()
            flow.response.headers.pop("content-encoding", None)
            flow.response.headers.pop("transfer-encoding", None)
            flow.response.content = new_content
            flow.response.headers["content-length"] = str(len(new_content))
            print(f"[INTERCEPTED] Modified Statsig config response")
        except Exception as e:
            print(f"[ERROR] Failed to modify Statsig response: {e}")

    # Handle credits/check interception FIRST, before logging
    if "api.atlassian.com/rovodev/v3/credits/check" in flow.request.pretty_url:
        if not flow.response or not flow.response.content:
            return
        try:
            # mitmproxy auto-decodes gzip, so use get_text() or decode directly
            content_encoding = flow.response.headers.get("content-encoding", "").lower()

            # Get the raw content - mitmproxy may have already decoded it
            raw_content = flow.response.content

            # Check if still gzip compressed (magic bytes)
            if raw_content[:2] == b"\x1f\x8b":
                content = gzip.decompress(raw_content)
            else:
                content = raw_content

            data = json.loads(content.decode("utf-8"))

            # Modify the balance - set to unlimited credits
            if "balance" in data:
                data["balance"]["monthlyTotal"] = 999999
                data["balance"]["monthlyRemaining"] = 999999
                data["balance"]["monthlyUsed"] = 0

            # Modify user credit limits
            if "userCreditLimits" in data and "limits" in data["userCreditLimits"]:
                data["userCreditLimits"]["limits"]["monthlyCreditAllocation"] = 999999
                data["userCreditLimits"]["limits"]["monthlyCreditCap"] = 999999
                data["userCreditLimits"]["limits"]["creditType"] = "PAID"

            new_content = json.dumps(data).encode()

            # Always send uncompressed and remove content-encoding
            flow.response.headers.pop("content-encoding", None)
            flow.response.headers.pop("transfer-encoding", None)
            flow.response.content = new_content
            flow.response.headers["content-length"] = str(len(new_content))
            print(f"[INTERCEPTED] Modified credits response: monthlyRemaining=999999")
        except Exception as e:
            import traceback

            print(f"[ERROR] Failed to modify response: {e}")
            traceback.print_exc()

    # Log all API responses AFTER modifications
    if any(
        domain in flow.request.pretty_url
        for domain in ["api.atlassian.com", "anthropic.com", "bedrock", "openai.com"]
    ):
        log_response(flow)

    if "api.atlassian.com/acli/api/v1/plugin/rovodev" in flow.request.pretty_url:
        if not flow.response or not flow.response.content:
            return
        try:
            data = json.loads(flow.response.content.decode("utf-8"))
            original_version = data.get("version", "unknown")

            if "version" in data:
                data["version"] = TARGET_VERSION
            if "downloadUrl" in data:
                data["downloadUrl"] = data["downloadUrl"].replace(
                    original_version, TARGET_VERSION
                )

            flow.response.content = json.dumps(data).encode()
            print(f"[ROVODEV] Version: {original_version} -> {TARGET_VERSION}")
        except Exception as e:
            print(f"[ROVODEV] Error: {e}")
