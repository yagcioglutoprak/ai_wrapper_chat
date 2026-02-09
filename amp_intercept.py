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

And rewrites the destination to localhost:8000/v1/messages (rovodev_server),
letting mitmproxy handle streaming natively for true SSE pass-through.
"""

import gzip
import json
import os
import time
import threading
from mitmproxy import http

# =============================================================================
# CONFIGURATION
# =============================================================================

AMP_API_URL = "ampcode.com/api/provider/anthropic/v1/messages"
ROVODEV_HOST = "127.0.0.1"
ROVODEV_PORT = 8000
ROVODEV_PATH = "/v1/messages"
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


# =============================================================================
# SSE STREAMING TRANSFORMER — zeroes usage in-flight without buffering
# =============================================================================

_ZERO_USAGE = {
    "input_tokens": 0,
    "output_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


class SSEUsageZeroer:
    """Line-buffered SSE transformer that zeroes token/cost fields in-flight.

    Assigned to flow.response.stream so mitmproxy calls it for each chunk.
    Works with arbitrary chunk boundaries (chunks may split mid-line).

    Also ensures that message_start and message_delta events always carry a
    ``usage`` object so the downstream client never crashes on
    ``D.usage.input_tokens`` being undefined.
    """

    def __init__(self):
        self._buf = ""

    @staticmethod
    def _ensure_usage(evt: dict) -> None:
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

    def __call__(self, chunk: bytes) -> bytes:
        if not chunk:
            remaining = self._flush()
            return remaining if remaining else b""

        self._buf += chunk.decode("utf-8", errors="replace")
        out = []
        while "\n" in self._buf:
            line, self._buf = self._buf.split("\n", 1)
            stripped = line.strip()
            if stripped.startswith("data: "):
                data_str = stripped[6:]
                try:
                    evt = json.loads(data_str)
                    _zero_tokens_and_costs(evt)
                    self._ensure_usage(evt)
                    line = f"data: {json.dumps(evt)}"
                except (json.JSONDecodeError, TypeError):
                    pass
            out.append(line + "\n")
        return "".join(out).encode("utf-8") if out else b""

    def _flush(self) -> bytes:
        if self._buf.strip():
            result = self._buf + "\n"
            self._buf = ""
            return result.encode("utf-8")
        self._buf = ""
        return b""


def log_request(flow: http.HTTPFlow, label: str) -> None:
    try:
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
    except Exception:
        pass


def request(flow: http.HTTPFlow) -> None:
    """Intercept Amp CLI requests and rewrite destination to rovodev_server."""

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

        # === ONE-SHOT CONVERSATION INJECTION (skip Haiku) ===
        if "haiku" not in model.lower():
            inject_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "queue", "inject_messages.json")
            if os.path.exists(inject_file):
                try:
                    with open(inject_file, "r") as _inf:
                        old_msgs = json.load(_inf)
                    new_user_msg = None
                    for msg in reversed(data.get("messages", [])):
                        if msg.get("role") == "user":
                            new_user_msg = msg
                            break
                    if new_user_msg and old_msgs:
                        data["messages"] = old_msgs + [new_user_msg]
                        print(f"[AMP-INJECT] Restored {len(old_msgs)} old msgs + 1 new user msg = {len(data['messages'])} total")
                    os.remove(inject_file)
                    print(f"[AMP-INJECT] Deleted inject file (one-shot done)")
                except Exception as _e:
                    print(f"[AMP-INJECT ERROR] {_e}")
        # === END INJECTION ===
        is_streaming = data.get("stream", True)
        msg_count = len(data.get("messages", []))
        tool_count = len(data.get("tools", []))
        sys_len = len(json.dumps(data.get("system", "")))
        body_size = len(content)
        has_thinking = "thinking" in data
        max_tokens = data.get("max_tokens", "?")

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

        ts = time.strftime("%Y%m%d_%H%M%S")
        track_line = (
            f"[{time.strftime('%Y-%m-%d %H:%M:%S')}] "
            f"model={model} | msgs={msg_count} | tools={tool_count} | "
            f"sys={sys_len}ch | body={body_size}B | max_tokens={max_tokens} | "
            f"stream={is_streaming} | thinking={has_thinking}\n"
            f"  -> user: {user_preview[:150].replace(chr(10), ' ')}\n"
        )
        try:
            os.makedirs(os.path.dirname(TRACK_FILE), exist_ok=True)
            with open(TRACK_FILE, "a") as tf:
                tf.write(f"\n{'=' * 80}\n")
                tf.write(track_line)
        except Exception:
            pass

        print(f"[AMP-TRACK] {model} | msgs={msg_count} | tools={tool_count} | body={body_size}B | max_tokens={max_tokens}")
        print(f"[AMP-TRACK] user_preview: {user_preview[:100].replace(chr(10), ' ')}")

        amp_log_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "amp-intercept", "logs")
        try:
            os.makedirs(amp_log_dir, exist_ok=True)
            model_tag = model.split("-")[1] if "-" in model else model
            with open(os.path.join(amp_log_dir, f"amp_{model_tag}_{ts}.json"), "w") as _f:
                json.dump(data, _f, indent=2)
            print(f"[AMP] Saved full request to amp-intercept/logs/amp_{model_tag}_{ts}.json")
        except Exception:
            pass

        if "haiku" in model.lower():
            print(f"[AMP-TRACK] HAIKU request logged — passing through to ampcode.com")
            return

        print(f"[AMP] Intercepting non-Haiku request: {flow.request.pretty_url}")
        log_request(flow, "AMP REQUEST")
        print(f"[AMP] Model: {model}, messages: {msg_count}, stream: {is_streaming}")

        # === FORBID ROVODEV/ATLASSIAN TOOLS VIA SYSTEM PROMPT ===
        _forbidden_tools_instruction = (
            "\n\n<CRITICAL_TOOL_RESTRICTION>\n"
            "The following tools exist in your tool list for technical reasons ONLY. "
            "You MUST NEVER call or attempt to use them under ANY circumstances. "
            "They are non-functional placeholders required by the platform — calling them will cause errors:\n"
            "- mcp__atlassian__get_tool_schema\n"
            "- mcp__atlassian__invoke_tool\n"
            "- open_files\n"
            "- delete_file\n"
            "- move_file\n"
            "- expand_code_chunks\n"
            "- find_and_replace_code\n"
            "- expand_folder\n"
            "- bash (use Bash instead)\n"
            "- grep (use Grep instead)\n"
            "- ask_user_questions\n"
            "- exit_plan_mode\n"
            "- update_todo\n"
            "NEVER generate tool_use blocks for ANY of these tools. "
            "Use only the standard Amp tools (Bash, Read, edit_file, create_file, Grep, glob, finder, Task, etc.).\n"
            "</CRITICAL_TOOL_RESTRICTION>"
        )
        system = data.get("system", "")
        if isinstance(system, str):
            data["system"] = system + _forbidden_tools_instruction
        elif isinstance(system, list):
            data["system"] = system + [{"type": "text", "text": _forbidden_tools_instruction}]
        else:
            data["system"] = _forbidden_tools_instruction
        print(f"[AMP] Injected forbidden-tools instruction into system prompt")

        if "thinking" in data:
            original_thinking = data["thinking"]
            data["thinking"] = {"type": "adaptive"}
            print(f"[AMP] Replaced thinking {original_thinking} -> adaptive")

        if "stream" in data:
            del data["stream"]

        original_max = data.get("max_tokens")
        data["max_tokens"] = 8192
        if original_max != 8192:
            print(f"[AMP] max_tokens: {original_max} -> 8192")

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
                "cache_control": {"type": "ephemeral", "ttl": "1h"}
            }
        ]
        tools = data.get("tools", [])
        tools[:] = [t for t in tools if t.get("name") not in {"mcp__atlassian__get_tool_schema", "mcp__atlassian__invoke_tool"}]
        tools.extend(_atlassian_tools)
        data["tools"] = tools
        print(f"[AMP] Added Atlassian MCP tools (total tools: {len(tools)})")

        # =================================================================
        # PROMPT CACHING — add cache_control breakpoints
        # Order: tools (already has it on last atlassian tool) → system → messages
        # =================================================================
        _cache_marker = {"type": "ephemeral", "ttl": "1h"}

        # --- Strip any existing cache_control from Amp's original request ---
        # (Amp adds its own, but they'd conflict with our placement)
        for t in data.get("tools", []):
            if isinstance(t, dict) and t.get("name") != "mcp__atlassian__invoke_tool":
                t.pop("cache_control", None)
        sys_blocks = data.get("system")
        if isinstance(sys_blocks, list):
            for sb in sys_blocks:
                if isinstance(sb, dict):
                    sb.pop("cache_control", None)
        for msg in data.get("messages", []):
            c = msg.get("content")
            if isinstance(c, list):
                for blk in c:
                    if isinstance(blk, dict):
                        blk.pop("cache_control", None)

        # Breakpoint 2: Last system prompt block
        sys_prompt = data.get("system")
        if isinstance(sys_prompt, list) and sys_prompt:
            sys_prompt[-1]["cache_control"] = _cache_marker
            print(f"[AMP-CACHE] Added cache_control to system[-1]")
        elif isinstance(sys_prompt, str) and sys_prompt:
            data["system"] = [{"type": "text", "text": sys_prompt, "cache_control": _cache_marker}]
            print(f"[AMP-CACHE] Converted system to list with cache_control")

        # Breakpoint 3: Last content block of second-to-last message
        # (caches full conversation history; only the final user msg is uncached)
        messages = data.get("messages", [])
        if len(messages) >= 2:
            target_msg = messages[-2]
            tc = target_msg.get("content")
            if isinstance(tc, list) and tc:
                tc[-1]["cache_control"] = _cache_marker
            elif isinstance(tc, str):
                target_msg["content"] = [{"type": "text", "text": tc, "cache_control": _cache_marker}]
            print(f"[AMP-CACHE] Added cache_control to messages[-2] (conversation history)")

        cache_points = sum(1 for t in data.get("tools", []) if isinstance(t, dict) and "cache_control" in t)
        if isinstance(data.get("system"), list):
            cache_points += sum(1 for s in data["system"] if isinstance(s, dict) and "cache_control" in s)
        msg_cache = sum(1 for m in messages for blk in (m.get("content") if isinstance(m.get("content"), list) else []) if isinstance(blk, dict) and "cache_control" in blk)
        cache_points += msg_cache
        print(f"[AMP-CACHE] Total cache breakpoints: {cache_points} (tools/system/messages)")

        body_bytes = json.dumps(data).encode("utf-8")
        flow.request.set_content(body_bytes)

        flow.request.headers.pop("accept-encoding", None)
        flow.request.headers.pop("content-encoding", None)
        flow.request.headers["content-type"] = "application/json"
        flow.request.headers["accept"] = "text/event-stream"
        flow.request.headers["content-length"] = str(len(body_bytes))

        flow.request.scheme = "http"
        flow.request.host = ROVODEV_HOST
        flow.request.port = ROVODEV_PORT
        flow.request.path = ROVODEV_PATH
        flow.request.headers["host"] = f"{ROVODEV_HOST}:{ROVODEV_PORT}"

        flow._amp_intercepted = True
        print(f"[AMP] Rewrote destination to http://{ROVODEV_HOST}:{ROVODEV_PORT}{ROVODEV_PATH} (streaming pass-through)")

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


def responseheaders(flow: http.HTTPFlow) -> None:
    """Enable streaming for intercepted responses — no buffering."""
    if not getattr(flow, "_amp_intercepted", False):
        return

    if flow.response:
        flow.response.stream = SSEUsageZeroer()
        print(f"[AMP] Streaming response with usage zeroing (status={flow.response.status_code})")


def response(flow: http.HTTPFlow) -> None:
    """Log responses for debugging."""
    if not getattr(flow, "_amp_intercepted", False):
        if AMP_API_URL not in flow.request.pretty_url:
            return

    if flow.response:
        size = len(flow.response.content or b"") if not getattr(flow, "_amp_intercepted", False) else "streamed"
        print(f"[AMP] Response status: {flow.response.status_code}, size: {size}")
