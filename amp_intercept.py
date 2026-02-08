"""
mitmproxy script to intercept Amp CLI requests and route them through
the local rovodev_server.py (which handles Atlassian Bedrock auth).

Usage:
    mitmdump -s amp_intercept.py -p 8899

Then run Amp CLI with proxy:
    HTTPS_PROXY=http://127.0.0.1:8899 amp

Prerequisites:
    1. mitmdump -s intercept.py -p 8080  (rovodev mitmproxy)
    2. python rovodev_server.py           (FastAPI server on :8000)

This script intercepts Amp CLI requests to:
    ampcode.com/api/provider/anthropic/v1/messages

And forwards the body to localhost:8000/v1/messages (rovodev_server),
which spawns `acli rovodev run` with proper Atlassian auth.
"""

import asyncio
import gzip
import json
import os
import time
import urllib.request
import urllib.error
from http.client import IncompleteRead
from concurrent.futures import ThreadPoolExecutor
from mitmproxy import http
from mitmproxy import ctx

_executor = ThreadPoolExecutor(max_workers=4)

# =============================================================================
# CONFIGURATION
# =============================================================================

AMP_API_URL = "ampcode.com/api/provider/anthropic/v1/messages"
ROVODEV_SERVER = "http://127.0.0.1:8000/v1/messages"
LOG_FILE = "/Users/toprakyagcioglu/.rovodev/logs/amp_proxy.log"
TRACK_FILE = "/Users/toprakyagcioglu/.rovodev/amp-intercept/amp_all_requests.log"

os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)

# =============================================================================
# TOKEN / COST ZEROING — prevent Amp from tracking real usage
# =============================================================================

TOKEN_KEYS = {
    "input_tokens", "output_tokens", "total_tokens",
    "cache_creation_input_tokens", "cache_read_input_tokens",
    "ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens",
    "inputTokens", "outputTokens", "totalInputTokens",
    "cacheCreationInputTokens", "cacheReadInputTokens", "maxInputTokens",
    "inputTokenCount", "outputTokenCount",
    "cacheReadInputTokenCount", "cacheWriteInputTokenCount",
    "gen_ai.usage.input_tokens", "gen_ai.usage.output_tokens",
    "gen_ai.usage.details.cache_creation_input_tokens",
    "gen_ai.usage.details.input_tokens", "gen_ai.usage.details.output_tokens",
    "gen_ai.usage.details.cache_write_tokens",
    "gen_ai.usage.details.cache_read_input_tokens",
}

COST_KEYS = {"totalCostUSD", "freeUSD", "paidUSD"}

INTERCEPT_URLS = [
    "/api/telemetry",
    "/api/internal?uploadThread",
    "/api/internal?threadDisplayCostInfo",
    "/cli/events",
]


def _zero_tokens_and_costs(obj, depth=0) -> bool:
    if depth > 20:
        return False
    changed = False
    if isinstance(obj, dict):
        for k in list(obj.keys()):
            if k in TOKEN_KEYS or k in COST_KEYS:
                obj[k] = 0
                changed = True
            if isinstance(obj[k], dict):
                if _zero_tokens_and_costs(obj[k], depth + 1):
                    changed = True
            elif isinstance(obj[k], list):
                for item in obj[k]:
                    if isinstance(item, dict):
                        if _zero_tokens_and_costs(item, depth + 1):
                            changed = True
    elif isinstance(obj, list):
        for item in obj:
            if isinstance(item, dict):
                if _zero_tokens_and_costs(item, depth + 1):
                    changed = True
    return changed


def _zero_sse_usage(data: bytes) -> bytes:
    """Zero out usage/token counts in SSE response events."""
    lines = data.decode("utf-8", errors="replace").splitlines()
    out = []
    for line in lines:
        if line.strip().startswith("data: "):
            data_str = line.strip()[6:]
            try:
                evt = json.loads(data_str)
                if _zero_tokens_and_costs(evt):
                    line = f"data: {json.dumps(evt)}"
            except (json.JSONDecodeError, TypeError):
                pass
        out.append(line)
    return "\n".join(out).encode("utf-8")


def log_request(flow: http.HTTPFlow, label: str) -> None:
    with open(LOG_FILE, "a") as f:
        f.write(f"\n{'=' * 80}\n")
        f.write(f"{label} - {time.strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"URL: {flow.request.pretty_url}\n")
        f.write(f"Method: {flow.request.method}\n")
        f.write(f"Headers:\n")
        for header, value in flow.request.headers.items():
            if header.lower() in ["authorization", "x-api-key"]:
                f.write(f"  {header}: [REDACTED]\n")
            else:
                f.write(f"  {header}: {value}\n")


async def request(flow: http.HTTPFlow) -> None:
    """Intercept Amp CLI requests and forward to rovodev_server."""

    # Zero out token/cost data in telemetry, uploadThread, costInfo requests
    url = flow.request.pretty_url
    ct = flow.request.headers.get("content-type", "")
    if "json" in ct and any(ep in url for ep in INTERCEPT_URLS):
        raw = flow.request.get_content()
        if raw:
            try:
                obj = json.loads(raw)
                if _zero_tokens_and_costs(obj):
                    new_body = json.dumps(obj).encode("utf-8")
                    flow.request.set_content(new_body)
                    flow.request.headers["content-length"] = str(len(new_body))
                    print(f"[AMP] Zeroed token/cost fields in {url.split('?')[-1]}")
            except (json.JSONDecodeError, ValueError):
                pass

    if AMP_API_URL not in flow.request.pretty_url:
        return

    if not flow.request.content:
        return

    try:
        content = flow.request.content
        if content[:2] == b"\x1f\x8b":
            content = gzip.decompress(content)

        data = json.loads(content.decode("utf-8"))

        model = data.get("model", "")
        is_streaming = data.get("stream", True)
        msg_count = len(data.get("messages", []))
        tool_count = len(data.get("tools", []))
        sys_len = len(json.dumps(data.get("system", "")))
        body_size = len(content)
        has_thinking = "thinking" in data
        max_tokens = data.get("max_tokens", "?")

        # --- Extract user message preview ---
        user_preview = ""
        for msg in reversed(data.get("messages", [])):
            if msg.get("role") == "user":
                c = msg.get("content", "")
                if isinstance(c, list):
                    for block in c:
                        if isinstance(block, dict) and block.get("type") == "text":
                            user_preview = block.get("text", "")[:200]
                            break
                elif isinstance(c, str):
                    user_preview = c[:200]
                break

        # --- Track ALL requests to log file ---
        ts = time.strftime("%Y%m%d_%H%M%S")
        track_entry = {
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "model": model,
            "stream": is_streaming,
            "messages": msg_count,
            "tools": tool_count,
            "system_chars": sys_len,
            "body_bytes": body_size,
            "max_tokens": max_tokens,
            "thinking": has_thinking,
            "user_preview": user_preview.replace("\n", "\\n"),
        }
        track_line = (
            f"[{track_entry['timestamp']}] "
            f"model={model} | msgs={msg_count} | tools={tool_count} | "
            f"sys={sys_len}ch | body={body_size}B | max_tokens={max_tokens} | "
            f"stream={is_streaming} | thinking={has_thinking}\n"
            f"  -> user: {user_preview[:150].replace(chr(10), ' ')}\n"
        )
        with open(TRACK_FILE, "a") as tf:
            tf.write(f"\n{'=' * 80}\n")
            tf.write(track_line)

        print(f"[AMP-TRACK] {model} | msgs={msg_count} | tools={tool_count} | body={body_size}B | max_tokens={max_tokens}")
        print(f"[AMP-TRACK] user_preview: {user_preview[:100].replace(chr(10), ' ')}")

        # --- Save full request body to per-request JSON ---
        amp_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amp-intercept", "logs")
        os.makedirs(amp_log_dir, exist_ok=True)
        model_tag = model.split("-")[1] if "-" in model else model
        with open(os.path.join(amp_log_dir, f"amp_{model_tag}_{ts}.json"), "w") as _f:
            json.dump(data, _f, indent=2)
        with open("/tmp/amp_original_request.json", "w") as _f:
            json.dump(data, _f, indent=2)
        print(f"[AMP] Saved full request to amp-intercept/logs/amp_{model_tag}_{ts}.json")

        # --- If Haiku: log it but let it pass through to real Amp API (don't block) ---
        if "haiku" in model.lower():
            print(f"[AMP-TRACK] HAIKU request logged — passing through to ampcode.com (not intercepting)")
            with open(TRACK_FILE, "a") as tf:
                tf.write(f"  >> ACTION: PASS-THROUGH (Haiku goes to real Amp API)\n")
            return

        print(f"[AMP] Intercepting non-Haiku request: {flow.request.pretty_url}")
        log_request(flow, "AMP REQUEST")
        print(f"[AMP] Model: {model}, messages: {msg_count}, stream: {is_streaming}")

        # --- Modify request to match Bedrock compatibility ---
        # Remove thinking (Bedrock gateway rejects it)
        if "thinking" in data:
            print(f"[AMP] Removing thinking: {data['thinking']}")
            del data["thinking"]

        # Remove stream (handled by Bedrock endpoint selection)
        if "stream" in data:
            print(f"[AMP] Removing stream: {data['stream']}")
            del data["stream"]

        # Force max_tokens to 8192
        original_max = data.get("max_tokens")
        data["max_tokens"] = 8192
        if original_max != 8192:
            print(f"[AMP] max_tokens: {original_max} -> 8192")

        # Append Atlassian MCP tools (remove existing first, then add at end)
        _atlassian_tools = [
            {
                "name": "mcp__atlassian__get_tool_schema",
                "description": "<summary>Get the input schema for a specific tool from the atlassian toolset.\n\nAvailable tools are:\n<tool>get_atlassian_site_urls(): Get the current user's Atlassian site URLs</tool>\n<tool>get_confluence_page(page_url, get_comments, markdown, output_file): Get the content of a Confluence page in Atlassian Document Format (ADF) JSON</tool>\n<tool>get_confluence_spaces(site_url, space_key, space_name_regex, types): Get details of all available Confluence spaces or a specific Confluence space</tool>\n<tool>view_confluence_descendants(page_url, max_depth, max_pages): View descendant pages of a specific page or space</tool>\n<tool>view_confluence_ancestors(page_url): View all ancestors of a specific page in Confluence</tool>\n<tool>get_adf_documentation(nodes, marks): Get the ADF documentation and schema for specific nodes and marks</tool>\n<tool>create_confluence_page(parent_url, title, content, is_live_doc): Create a new page in Confluence in Atlassian Document Format (ADF) JSON</tool>\n<tool>update_confluence_page(page_url, title, find, replace, version_message, match_index): Update an existing Confluence page</tool>\n<tool>add_confluence_page_comment(page_url, comment, find, match_index): Add a comment to a Confluence page, either as a footer or inline comment</tool>\n<tool>search_confluence_using_cql(site_url, cql, limit): Search content in Confluence using CQL (Confluence Query Language)</tool>\n<tool>get_jira_issue(issue_url, show_transitions, show_links, get_comments, extra_fields): Get details of a Jira issue</tool>\n<tool>get_jira_projects(site_url, include_issue_types, project_filter, include_custom_fields): Get visible Jira projects</tool>\n<tool>create_jira_issue(project_url, issue_type, summary, description, assignee, parent_issue, fields): Create a new Jira issue in a project</tool>\n<tool>update_jira_issue(issue_url, summary, description, assignee, transition, comment, fields): Update a Jira issue with various operations</tool>\n<tool>search_jira_using_jql(site_url, jql, limit): Search Jira issues using JQL (Jira Query Language)</tool>\n<tool>download_jira_issue_attachment(issue_url, attachment_id, workspace_path): Download an attachment from a Jira issue and return its contents</tool>\n<tool>upload_jira_issue_attachment(issue_url, file_path, mime_type): Upload a file as an attachment to a Jira issue</tool></summary>\n<returns>\n<description>The input schema for the specified tool.</description>\n</returns>",
                "input_schema": {
                    "additionalProperties": False,
                    "properties": {
                        "tool_name": {
                            "description": "The name of the tool to get the schema for.",
                            "type": "string"
                        }
                    },
                    "required": ["tool_name"],
                    "type": "object"
                }
            },
            {
                "name": "mcp__atlassian__invoke_tool",
                "description": "<summary>Invoke a tool from the atlassian toolset.</summary>\n<returns>\n<description>The output from the tool.</description>\n</returns>",
                "input_schema": {
                    "additionalProperties": {
                        "additionalProperties": True,
                        "type": "object"
                    },
                    "properties": {
                        "tool_name": {
                            "description": "The name of the tool to invoke.",
                            "type": "string"
                        },
                        "tool_input": {
                            "anyOf": [
                                {"additionalProperties": True, "type": "object"},
                                {"type": "null"}
                            ],
                            "default": None,
                            "description": "The input to the tool. Schemas can be retrieved using the approprate `get_tool_schema` function."
                        }
                    },
                    "required": ["tool_name"],
                    "type": "object"
                },
                "cache_control": {"type": "ephemeral"}
            }
        ]
        # Remove any existing MCP tools, then append fresh ones at the end
        tools = data.get("tools", [])
        tools[:] = [t for t in tools if t.get("name") not in {"mcp__atlassian__get_tool_schema", "mcp__atlassian__invoke_tool"}]
        tools.extend(_atlassian_tools)
        data["tools"] = tools
        print(f"[AMP] Added Atlassian MCP tools (total tools: {len(tools)})")

        body_bytes = json.dumps(data).encode("utf-8")
        req = urllib.request.Request(
            ROVODEV_SERVER,
            data=body_bytes,
            headers={
                "Content-Type": "application/json",
                "Accept": "text/event-stream" if is_streaming else "application/json",
            },
            method="POST",
        )

        print(f"[AMP] Forwarding to rovodev_server ({ROVODEV_SERVER})...")

        def _do_forward(req):
            resp = urllib.request.urlopen(req, timeout=660)
            status = resp.status
            content_type = resp.headers.get("Content-Type", "text/event-stream")
            chunks = []
            try:
                while True:
                    chunk = resp.read(8192)
                    if not chunk:
                        break
                    chunks.append(chunk)
            except IncompleteRead as e:
                chunks.append(e.partial)
                print(f"[AMP] IncompleteRead — using {sum(len(c) for c in chunks)} bytes received so far")
            return status, content_type, b"".join(chunks)

        try:
            loop = asyncio.get_event_loop()
            status, content_type, response_data = await loop.run_in_executor(
                _executor, _do_forward, req
            )

            print(f"[AMP] Got response: status={status}, size={len(response_data)}, type={content_type}")

            response_data = _zero_sse_usage(response_data)

            flow.response = http.Response.make(
                status,
                response_data,
                {
                    "Content-Type": content_type,
                    "Cache-Control": "no-cache, no-store",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )
        except urllib.error.HTTPError as e:
            error_body = e.read().decode("utf-8", errors="replace")
            print(f"[AMP ERROR] rovodev_server returned {e.code}: {error_body[:500]}")

            with open(LOG_FILE, "a") as f:
                f.write(f"\n[ERROR] rovodev_server {e.code}: {error_body[:500]}\n")

            flow.response = http.Response.make(
                e.code,
                error_body.encode("utf-8"),
                {"Content-Type": "application/json"},
            )
        except urllib.error.URLError as e:
            error_msg = f"Cannot connect to rovodev_server at {ROVODEV_SERVER}: {e.reason}"
            print(f"[AMP ERROR] {error_msg}")
            flow.response = http.Response.make(
                502,
                json.dumps({
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": error_msg,
                    },
                }).encode("utf-8"),
                {"Content-Type": "application/json"},
            )

    except Exception as e:
        import traceback
        print(f"[AMP ERROR] Request processing failed: {e}")
        traceback.print_exc()
        flow.response = http.Response.make(
            500,
            json.dumps({
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": str(e),
                },
            }).encode("utf-8"),
            {"Content-Type": "application/json"},
        )


def response(flow: http.HTTPFlow) -> None:
    """Log responses for debugging."""
    if AMP_API_URL not in flow.request.pretty_url:
        return

    if flow.response:
        print(f"[AMP] Response status: {flow.response.status_code}, size: {len(flow.response.content or b'')} bytes")
