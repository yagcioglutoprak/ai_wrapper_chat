#!/usr/bin/env python3
"""
Test which aspect of the system prompt causes 403.

Hypotheses:
1. Amp system prompt content (URLs, competitor mentions)
2. System prompt size
3. Number of system blocks (Amp has 6, RovoDev has 2)
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
import re


QUEUE_DIR = "/Users/toprakyagcioglu/.rovodev/queue"
RAW_DIR = "/Users/toprakyagcioglu/.rovodev/raw_responses"
PROXY_URL = "http://127.0.0.1:8080"
MITMPROXY_CERT = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")


# Minimal MCP tools (known to work)
MCP_TOOLS = [
    {
        "name": "mcp__atlassian__get_tool_schema",
        "description": "Get the input schema for a specific tool from the atlassian toolset.",
        "input_schema": {
            "additionalProperties": False,
            "properties": {
                "tool_name": {
                    "description": "The name of the tool to get the schema for.",
                    "type": "string",
                }
            },
            "required": ["tool_name"],
            "type": "object",
        },
    },
    {
        "name": "mcp__atlassian__invoke_tool",
        "description": "Invoke a tool from the atlassian toolset.",
        "input_schema": {
            "additionalProperties": {"additionalProperties": True, "type": "object"},
            "properties": {
                "tool_name": {
                    "description": "The name of the tool to invoke.",
                    "type": "string",
                },
                "tool_input": {
                    "anyOf": [
                        {"additionalProperties": True, "type": "object"},
                        {"type": "null"},
                    ],
                    "default": None,
                    "description": "The input to the tool.",
                },
            },
            "required": ["tool_name"],
            "type": "object",
        },
        "cache_control": {"type": "ephemeral"},
    },
]


def run_test(request_body, label):
    """Send request through proxy, return status code."""
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)

    test_id = str(uuid.uuid4())
    queue_file = os.path.join(QUEUE_DIR, f"relay_queue.{test_id}.json")
    capture_file = os.path.join(QUEUE_DIR, f"capture_next.{test_id}.json")
    raw_path = os.path.join(RAW_DIR, f"{test_id}.raw")
    meta_path = os.path.join(RAW_DIR, f"{test_id}.meta")

    for p in [queue_file, capture_file, raw_path, meta_path]:
        try:
            os.unlink(p)
        except FileNotFoundError:
            pass

    with open(queue_file, "w") as f:
        json.dump(request_body, f)
        f.flush()
        os.fsync(f.fileno())

    with open(capture_file, "w") as f:
        json.dump(
            {"raw_path": raw_path, "meta_path": meta_path, "request_id": test_id}, f
        )
        f.flush()
        os.fsync(f.fileno())

    env = os.environ.copy()
    env["ROVODEV_REQUEST_ID"] = test_id
    env["HTTPS_PROXY"] = PROXY_URL
    env["HTTP_PROXY"] = PROXY_URL
    env["SSL_CERT_FILE"] = MITMPROXY_CERT
    env["REQUESTS_CA_BUNDLE"] = MITMPROXY_CERT

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="test_")
    os.close(fd)
    try:
        os.unlink(output_path)
    except OSError:
        pass

    cmd = ["acli", "rovodev", "run", ".", "--output-file", output_path, "--yolo"]

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=os.path.expanduser("~/.rovodev"),
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL,
        )

        start_time = time.time()
        while time.time() - start_time < 60:
            if os.path.exists(meta_path):
                break
            rc = proc.poll()
            if rc is not None:
                for _ in range(100):
                    if os.path.exists(meta_path):
                        break
                    time.sleep(0.05)
                break
            time.sleep(0.1)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

        status = -1
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)
            status = meta.get("status_code", -1)

        for p in [queue_file, capture_file, raw_path, meta_path, output_path]:
            try:
                os.unlink(p)
            except (OSError, FileNotFoundError):
                pass

        body_size = len(json.dumps(request_body))
        sys_size = len(json.dumps(request_body.get("system", [])))
        print(f"  [{label}] Status: {status} | body={body_size} | system={sys_size}")
        return status

    except Exception as e:
        print(f"  [{label}] Error: {e}")
        return -1


def build_body(system_prompt, tools=None):
    """Build a standard request body with given system and tools."""
    body = {
        "max_tokens": 8192,
        "messages": [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": "Say hello"},
                    {
                        "type": "text",
                        "text": "\n\nYou have used 0 iterations.",
                        "cache_control": {"type": "ephemeral"},
                    },
                ],
            }
        ],
        "system": system_prompt,
        "tools": tools or MCP_TOOLS,
        "anthropic_version": "bedrock-2023-05-31",
    }
    return body


# Load the Amp system prompt from the log
def load_amp_system_prompt():
    """Extract the Amp system prompt from the modified request in the log."""
    log_file = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    with open(log_file, "r") as f:
        content = f.read()

    # Find the FIRST modified body (which has the full Amp system prompt)
    marker = "MODIFIED REQUEST BODY:\n" + "-" * 50 + "\n"
    # Find second occurrence (first one has parse issues)
    start = content.find(marker)
    start = content.find(marker, start + 1)  # skip first
    if start == -1:
        return None
    start += len(marker)
    end = content.find("\n====", start)
    json_str = content[start:end].strip()

    try:
        body = json.loads(json_str)
        return body.get("system", [])
    except json.JSONDecodeError:
        return None


def main():
    print("=" * 80)
    print("SYSTEM PROMPT BISECTION TEST")
    print("=" * 80)

    amp_system = load_amp_system_prompt()
    if not amp_system:
        print("Failed to load Amp system prompt from log")
        return

    print(f"Amp system: {len(amp_system)} blocks, {len(json.dumps(amp_system))} chars")

    # Short working system prompt (known to work)
    short_system = [
        {
            "type": "text",
            "text": "You are a helpful coding assistant.",
            "cache_control": {"type": "ephemeral"},
        }
    ]

    # Test 1: Short system prompt (baseline - should work)
    print("\n--- Test 1: Short system prompt (baseline) ---")
    body = build_body(short_system)
    status = run_test(body, "short_system")
    if status != 200:
        print("BASELINE FAILED - proxy/auth issue")
        return

    # Test 2: Full Amp system prompt (should fail)
    print("\n--- Test 2: Full Amp system prompt ---")
    body = build_body(amp_system)
    status = run_test(body, "amp_full")

    if status == 200:
        print("Amp system prompt works! Problem must be elsewhere.")
        return

    # Test 3: Amp system prompt with URLs stripped and Amp→opencode
    print("\n--- Test 3: Amp system prompt (URLs stripped, Amp→opencode) ---")
    sanitized_system = []
    for block in amp_system:
        new_block = dict(block)
        if "text" in new_block:
            text = new_block["text"]
            text = re.sub(r"https?://[^\s\)\]\}\>\"\'`,]+", "", text)
            text = text.replace("ampcode", "opencode").replace("Ampcode", "Opencode")
            text = re.sub(r"\bAmp\b", "opencode", text)
            text = re.sub(r"\bamp\b", "opencode", text)
            new_block["text"] = text
        sanitized_system.append(new_block)
    body = build_body(sanitized_system)
    status = run_test(body, "amp_sanitized")

    if status == 200:
        print("URL stripping / name replacement FIXES the issue!")
        return

    # Test 4: Single system block with Amp content (concatenated)
    print("\n--- Test 4: Amp system as SINGLE block ---")
    combined_text = "\n\n".join(
        block.get("text", "") for block in amp_system if isinstance(block, dict)
    )
    single_system = [
        {
            "type": "text",
            "text": combined_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    body = build_body(single_system)
    status = run_test(body, "single_block")

    if status == 200:
        print("Single block FIXES the issue! Multiple system blocks cause 403.")
        return

    # Test 5: Single block + sanitized
    print("\n--- Test 5: Single block + sanitized ---")
    sanitized_combined = re.sub(r"https?://[^\s\)\]\}\>\"\'`,]+", "", combined_text)
    sanitized_combined = sanitized_combined.replace("ampcode", "opencode")
    sanitized_combined = re.sub(r"\bAmp\b", "opencode", sanitized_combined)
    sanitized_combined = re.sub(r"\bamp\b", "opencode", sanitized_combined)
    single_sanitized = [
        {
            "type": "text",
            "text": sanitized_combined,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    body = build_body(single_sanitized)
    status = run_test(body, "single_sanitized")

    if status == 200:
        print("Single block + sanitized FIXES it!")
        return

    # Test 6: Size test - use short prompt padded to same size as Amp
    print("\n--- Test 6: Size test (padded to match Amp) ---")
    target_size = len(combined_text)
    padded_text = (
        "You are a helpful coding assistant.\n\n"
        + "Additional context: " + ("x" * (target_size - 60))
    )
    padded_system = [
        {
            "type": "text",
            "text": padded_text,
            "cache_control": {"type": "ephemeral"},
        }
    ]
    body = build_body(padded_system)
    status = run_test(body, "padded_size")

    if status == 403:
        print("SIZE is the issue! System prompt too large.")
        # Binary search for size limit
        lo, hi = 1000, target_size
        while hi - lo > 500:
            mid = (lo + hi) // 2
            test_text = "You are a helpful assistant.\n\n" + ("x" * mid)
            test_sys = [
                {
                    "type": "text",
                    "text": test_text,
                    "cache_control": {"type": "ephemeral"},
                }
            ]
            body = build_body(test_sys)
            s = run_test(body, f"size_{mid}")
            if s == 200:
                lo = mid
            else:
                hi = mid
        print(f"\nSize limit is approximately {lo}-{hi} characters")
    else:
        print("Size is NOT the issue. Must be specific content.")
        # Test 7: Try Amp system with only first block
        print("\n--- Test 7: Only first system block ---")
        first_block = [amp_system[0]]
        body = build_body(first_block)
        status = run_test(body, "first_block_only")

        if status == 200:
            # One of the other blocks causes the issue
            print("First block alone works! Testing other blocks...")
            for i in range(1, len(amp_system)):
                block = amp_system[i]
                test_sys = [amp_system[0], block]
                body = build_body(test_sys)
                text_preview = block.get("text", "")[:50].replace("\n", "\\n")
                status = run_test(body, f"block_{i}: {text_preview}")
                if status != 200:
                    print(f"  *** Block {i} causes the issue!")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
