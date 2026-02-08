#!/usr/bin/env python3
"""
Bisect AMP tools: find which tool(s) cause 403.

KNOWN:
- Original RovoDev tools (14) → 200
- AMP tools (22) → 403

Strategy:
1. Start with working RovoDev base body
2. Replace tools with: RovoDev tools + N AMP tools (binary search)
3. Also test: just AMP tools with small system prompt
4. Also test: body size threshold (pad RovoDev body to match AMP body size)
"""

import json
import os
import subprocess
import tempfile
import time
import uuid


QUEUE_DIR = "/Users/toprakyagcioglu/.rovodev/queue"
RAW_DIR = "/Users/toprakyagcioglu/.rovodev/raw_responses"
PROXY_URL = "http://127.0.0.1:8080"
MITMPROXY_CERT = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")


def run_test(body, label, timeout=45):
    os.makedirs(QUEUE_DIR, exist_ok=True)
    os.makedirs(RAW_DIR, exist_ok=True)

    test_id = str(uuid.uuid4())
    q = os.path.join(QUEUE_DIR, f"relay_queue.{test_id}.json")
    c = os.path.join(QUEUE_DIR, f"capture_next.{test_id}.json")
    r = os.path.join(RAW_DIR, f"{test_id}.raw")
    m = os.path.join(RAW_DIR, f"{test_id}.meta")

    for p in [q, c, r, m]:
        try: os.unlink(p)
        except: pass

    with open(q, "w") as f:
        json.dump(body, f); f.flush(); os.fsync(f.fileno())
    with open(c, "w") as f:
        json.dump({"raw_path": r, "meta_path": m, "request_id": test_id}, f)
        f.flush(); os.fsync(f.fileno())

    env = os.environ.copy()
    env["ROVODEV_REQUEST_ID"] = test_id
    env["HTTPS_PROXY"] = PROXY_URL
    env["HTTP_PROXY"] = PROXY_URL
    env["SSL_CERT_FILE"] = MITMPROXY_CERT
    env["REQUESTS_CA_BUNDLE"] = MITMPROXY_CERT

    fd, out = tempfile.mkstemp(suffix=".txt", prefix="tb_")
    os.close(fd)
    try: os.unlink(out)
    except: pass

    cmd = ["acli", "rovodev", "run", ".", "--output-file", out, "--yolo"]
    proc = subprocess.Popen(cmd, env=env, cwd=os.path.expanduser("~/.rovodev"),
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                            stdin=subprocess.DEVNULL)

    t0 = time.time()
    while time.time() - t0 < timeout:
        if os.path.exists(m): break
        if proc.poll() is not None:
            for _ in range(100):
                if os.path.exists(m): break
                time.sleep(0.05)
            break
        time.sleep(0.1)

    if proc.poll() is None:
        proc.terminate()
        try: proc.wait(timeout=5)
        except: proc.kill()

    status = -1
    if os.path.exists(m):
        with open(m) as f:
            status = json.load(f).get("status_code", -1)

    for p in [q, c, r, m, out]:
        try: os.unlink(p)
        except: pass

    sz = len(json.dumps(body))
    sym = "✓" if status == 200 else "✗" if status == 403 else "?"
    print(f"  {sym} [{label}] status={status} body={sz}")
    return status


def load_working_body():
    """Load the original unmodified CLI body from the log (known to work)."""
    log = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    with open(log) as f:
        content = f.read()

    marker = "UNMODIFIED REQUEST BODY:\n" + "-" * 50 + "\n"
    pos = content.rfind(marker)
    if pos < 0:
        return None
    start = pos + len(marker)
    end = content.find("\n\n--", start)
    if end == -1:
        end = content.find("\n====", start)

    body = json.loads(content[start:end].strip())
    body.pop("tool_choice", None)
    body.pop("temperature", None)
    if "anthropic_version" not in body:
        body["anthropic_version"] = "bedrock-2023-05-31"

    def strip_cc(obj):
        if isinstance(obj, dict):
            obj.pop("cache_control", None)
            for v in obj.values(): strip_cc(v)
        elif isinstance(obj, list):
            for i in obj: strip_cc(i)
    strip_cc(body)
    return body


def load_amp_tools():
    """Load the AMP tools from /tmp/nonrelay_body.json."""
    with open("/tmp/nonrelay_body.json") as f:
        body = json.load(f)
    return body.get("tools", [])


def load_amp_system():
    """Load the AMP system prompt from /tmp/nonrelay_body.json."""
    with open("/tmp/nonrelay_body.json") as f:
        body = json.load(f)
    return body.get("system", [])


def main():
    print("=" * 80)
    print("AMP TOOL BISECTION")
    print("=" * 80)

    base = load_working_body()
    if not base:
        print("Could not load working body")
        return

    amp_tools = load_amp_tools()
    amp_system = load_amp_system()

    rovodev_tools = base.get("tools", [])
    rovodev_system = base.get("system", [])

    # Filter: separate MCP tools from non-MCP
    amp_non_mcp = [t for t in amp_tools if not t.get("name","").startswith("mcp__")]
    amp_mcp = [t for t in amp_tools if t.get("name","").startswith("mcp__")]
    rovodev_non_mcp = [t for t in rovodev_tools if not t.get("name","").startswith("mcp__")]
    rovodev_mcp = [t for t in rovodev_tools if t.get("name","").startswith("mcp__")]

    print(f"RovoDev: {len(rovodev_non_mcp)} tools + {len(rovodev_mcp)} MCP")
    print(f"AMP: {len(amp_non_mcp)} tools + {len(amp_mcp)} MCP")
    print(f"AMP tool names: {[t.get('name') for t in amp_non_mcp]}")
    print(f"AMP system: {len(amp_system)} blocks, {len(json.dumps(amp_system))} chars")
    print()

    # ---- Test 1: Baseline (RovoDev tools + RovoDev system) ----
    print("--- Test 1: Baseline (RovoDev tools + RovoDev system) ---")
    status = run_test(base, "baseline_rovodev")
    if status != 200:
        print("Baseline failed!")
        return

    # ---- Test 2: RovoDev tools + AMP system prompt ----
    print("\n--- Test 2: RovoDev tools + AMP system (test if system causes 403) ---")
    body2 = dict(base)
    body2["system"] = amp_system
    status = run_test(body2, "rovodev_tools+amp_system")

    # ---- Test 3: AMP tools + RovoDev system prompt ----
    print("\n--- Test 3: AMP tools + RovoDev system (test if tools cause 403) ---")
    body3 = dict(base)
    body3["tools"] = amp_tools
    status = run_test(body3, "amp_tools+rovodev_system")

    if status == 403:
        print("\n  → AMP TOOLS cause 403 regardless of system prompt!")
        print("\n  Now bisecting which AMP tools are the problem...")

        # ---- Test 4: Add AMP tools one-by-one to RovoDev ----
        print("\n--- Test 4: Adding AMP tools one-by-one to RovoDev base ---")
        working_tools = list(rovodev_tools)  # start with working set

        for i, tool in enumerate(amp_non_mcp):
            name = tool.get("name", "?")
            test_tools = working_tools + [tool]
            # Ensure last tool has cache_control
            for t in test_tools:
                t.pop("cache_control", None)
            test_tools[-1]["cache_control"] = {"type": "ephemeral"}

            body4 = dict(base)
            body4["tools"] = test_tools
            s = run_test(body4, f"add_{name}")
            if s == 200:
                working_tools.append(tool)
            else:
                print(f"\n  *** {name} CAUSES 403! ***")
                # Check if it's the tool content or schema
                print(f"      Tool description length: {len(tool.get('description', ''))}")
                schema = tool.get("input_schema", {})
                print(f"      Schema has $schema: {'$schema' in schema}")
                print(f"      Schema has $defs: {'$defs' in schema}")
                print(f"      Schema has $ref: {json.dumps(schema).count('$ref')}")
                print(f"      Schema size: {len(json.dumps(schema))}")

    elif status == 200:
        print("\n  → AMP tools work with RovoDev system!")
        print("  The issue is likely AMP system prompt + AMP tools COMBINED.")

        # ---- Test 5: Size test ----
        print("\n--- Test 5: Body size threshold test ---")
        # Pad the working body to match AMP body size
        amp_body_size = len(json.dumps({"max_tokens": base["max_tokens"],
                                        "messages": base["messages"],
                                        "system": amp_system,
                                        "tools": amp_tools,
                                        "anthropic_version": "bedrock-2023-05-31"}))
        print(f"  AMP body size: {amp_body_size}")
        print(f"  Working body size: {len(json.dumps(base))}")

        # Test with padded system prompt to match size
        target_pad = amp_body_size - len(json.dumps(base))
        if target_pad > 0:
            padded_sys = list(base.get("system", []))
            if padded_sys:
                padded_sys[0] = dict(padded_sys[0])
                padded_sys[0]["text"] = padded_sys[0].get("text","") + "\n" + "x" * target_pad
            body5 = dict(base)
            body5["system"] = padded_sys
            s = run_test(body5, f"padded_to_{amp_body_size}")
            if s == 403:
                print(f"\n  → BODY SIZE is the issue! Threshold around {amp_body_size}")
            else:
                print(f"\n  → Body size is NOT the issue at {amp_body_size}")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
