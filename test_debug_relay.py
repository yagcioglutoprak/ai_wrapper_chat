#!/usr/bin/env python3
"""
Comprehensive relay debug test.

HYPOTHESIS: ALL relay requests get 403, ALL non-relay get 200.
The problem is in the relay mechanism itself, not the content.

This script:
1. Captures a known-good non-relay request body
2. Sends that EXACT body through relay
3. Compares relay vs non-relay at the byte level
4. Tests various relay body formats
"""

import json
import os
import subprocess
import tempfile
import time
import uuid
import sys


QUEUE_DIR = "/Users/toprakyagcioglu/.rovodev/queue"
RAW_DIR = "/Users/toprakyagcioglu/.rovodev/raw_responses"
PROXY_URL = "http://127.0.0.1:8080"
MITMPROXY_CERT = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
DEBUG_REQUEST = "/tmp/debug_request.json"
DEBUG_FULL_DIR = "/tmp"


def run_relay_test(request_body, label, timeout=60):
    """Send request through relay mechanism, return (status_code, response_preview)."""
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

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="test_relay_")
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        start_time = time.time()
        while time.time() - start_time < timeout:
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

        stdout = (proc.stdout.read() or b"").decode(errors="replace")[:300]
        stderr = (proc.stderr.read() or b"").decode(errors="replace")[:300]

        status = -1
        resp_preview = ""
        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)
            status = meta.get("status_code", -1)
            if os.path.exists(raw_path):
                with open(raw_path, "rb") as f:
                    raw = f.read()
                resp_preview = raw[:200].decode("utf-8", errors="replace")
        else:
            resp_preview = f"NO META FILE. stdout={stdout[:100]} stderr={stderr[:100]}"

        # Capture the debug request that intercept.py wrote
        debug_body = None
        if os.path.exists(DEBUG_REQUEST):
            try:
                with open(DEBUG_REQUEST) as f:
                    debug_body = json.load(f)
            except:
                pass

        for p in [queue_file, capture_file, raw_path, meta_path, output_path]:
            try:
                os.unlink(p)
            except (OSError, FileNotFoundError):
                pass

        body_size = len(json.dumps(request_body))
        print(f"\n  [{label}]")
        print(f"    Status: {status}")
        print(f"    Relay body size: {body_size}")
        if debug_body:
            print(f"    Final body size (after intercept.py): {len(json.dumps(debug_body))}")
            print(f"    Final body keys: {sorted(debug_body.keys())}")
            print(f"    Final tools: {len(debug_body.get('tools', []))}")
            print(f"    Final sys blocks: {len(debug_body.get('system', []))}")
            final_tools = [t.get("name","?") for t in debug_body.get("tools",[])]
            print(f"    Final tool names: {final_tools}")
            # Check cache_control placement
            cc_places = []
            for i, b in enumerate(debug_body.get("system", [])):
                if isinstance(b, dict) and "cache_control" in b:
                    cc_places.append(f"system[{i}]")
            for i, t in enumerate(debug_body.get("tools", [])):
                if isinstance(t, dict) and "cache_control" in t:
                    cc_places.append(f"tools[{i}]/{t.get('name','?')}")
            for mi, m in enumerate(debug_body.get("messages", [])):
                content = m.get("content", [])
                if isinstance(content, list):
                    for ci, c in enumerate(content):
                        if isinstance(c, dict) and "cache_control" in c:
                            cc_places.append(f"msg[{mi}].content[{ci}]")
            print(f"    cache_control at: {cc_places}")
        if resp_preview:
            print(f"    Response: {resp_preview[:150]}")
        return status, debug_body

    except Exception as e:
        print(f"  [{label}] Error: {e}")
        import traceback
        traceback.print_exc()
        return -1, None


def run_nonrelay_test(label, timeout=60):
    """Run a NON-relay request (let CLI do its thing normally).
    Returns the debug_request.json that intercept.py writes."""
    # Delete the debug file first so we know we get a fresh one
    try:
        os.unlink(DEBUG_REQUEST)
    except FileNotFoundError:
        pass

    env = os.environ.copy()
    env["HTTPS_PROXY"] = PROXY_URL
    env["HTTP_PROXY"] = PROXY_URL
    env["SSL_CERT_FILE"] = MITMPROXY_CERT
    env["REQUESTS_CA_BUNDLE"] = MITMPROXY_CERT
    # Do NOT set ROVODEV_REQUEST_ID - this ensures no relay queue is found

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="test_nonrelay_")
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
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            stdin=subprocess.DEVNULL,
        )

        # Wait for debug_request.json to appear (written by intercept.py)
        start_time = time.time()
        while time.time() - start_time < timeout:
            if os.path.exists(DEBUG_REQUEST):
                # Wait a bit more for it to be fully written
                time.sleep(0.5)
                break
            rc = proc.poll()
            if rc is not None:
                time.sleep(1)
                break
            time.sleep(0.2)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except Exception:
                proc.kill()

        debug_body = None
        if os.path.exists(DEBUG_REQUEST):
            with open(DEBUG_REQUEST) as f:
                debug_body = json.load(f)

        for p in [output_path]:
            try:
                os.unlink(p)
            except (OSError, FileNotFoundError):
                pass

        if debug_body:
            print(f"\n  [{label}]")
            print(f"    Body size: {len(json.dumps(debug_body))}")
            print(f"    Keys: {sorted(debug_body.keys())}")
            print(f"    Tools: {len(debug_body.get('tools', []))}")
            tool_names = [t.get("name","?") for t in debug_body.get("tools",[])]
            print(f"    Tool names: {tool_names}")
            print(f"    Sys blocks: {len(debug_body.get('system', []))}")
            cc_places = []
            for i, b in enumerate(debug_body.get("system", [])):
                if isinstance(b, dict) and "cache_control" in b:
                    cc_places.append(f"system[{i}]")
            for i, t in enumerate(debug_body.get("tools", [])):
                if isinstance(t, dict) and "cache_control" in t:
                    cc_places.append(f"tools[{i}]/{t.get('name','?')}")
            for mi, m in enumerate(debug_body.get("messages", [])):
                content = m.get("content", [])
                if isinstance(content, list):
                    for ci, c in enumerate(content):
                        if isinstance(c, dict) and "cache_control" in c:
                            cc_places.append(f"msg[{mi}].content[{ci}]")
            print(f"    cache_control at: {cc_places}")
        else:
            print(f"  [{label}] No debug body captured!")

        return debug_body

    except Exception as e:
        print(f"  [{label}] Error: {e}")
        return None


def main():
    print("=" * 80)
    print("COMPREHENSIVE RELAY DEBUG")
    print("=" * 80)

    # ----------------------------------------------------------------
    # Step 1: Capture a known-good non-relay request
    # ----------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 1: Capture non-relay request (baseline)")
    print("=" * 80)
    nonrelay_body = run_nonrelay_test("nonrelay_baseline")
    if not nonrelay_body:
        print("FAILED: Could not capture non-relay body. Is mitmproxy running?")
        return

    # Save the non-relay body for comparison
    with open("/tmp/nonrelay_body.json", "w") as f:
        json.dump(nonrelay_body, f, indent=2)
    print(f"\n  Saved non-relay body to /tmp/nonrelay_body.json")

    # ----------------------------------------------------------------
    # Step 2: Send the EXACT same body through relay
    # ----------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 2: Send EXACT non-relay body through relay")
    print("=" * 80)
    print("  (If this fails, the issue is the relay mechanism itself)")

    status, relay_debug = run_relay_test(nonrelay_body, "exact_nonrelay_via_relay")

    if status == 200:
        print("\n  SUCCESS! Non-relay body works through relay too.")
        print("  The issue is in how the relay body is constructed.")
    else:
        print(f"\n  FAILED with {status}! Even the exact non-relay body fails through relay.")
        print("  The issue is the relay MECHANISM, not the body content.")

        # ----------------------------------------------------------------
        # Step 3: Compare what intercept.py does differently for relay
        # ----------------------------------------------------------------
        print("\n" + "=" * 80)
        print("STEP 3: Deep comparison - relay vs non-relay final bodies")
        print("=" * 80)

        if relay_debug and nonrelay_body:
            # Compare key by key
            all_keys = sorted(set(list(nonrelay_body.keys()) + list(relay_debug.keys())))
            for key in all_keys:
                nr_val = nonrelay_body.get(key)
                rl_val = relay_debug.get(key)
                if key in ("messages", "system", "tools"):
                    nr_json = json.dumps(nr_val, sort_keys=True)
                    rl_json = json.dumps(rl_val, sort_keys=True)
                    if nr_json == rl_json:
                        print(f"    {key}: IDENTICAL ({len(nr_json)} chars)")
                    else:
                        print(f"    {key}: DIFFERENT!")
                        print(f"      non-relay: {len(nr_json)} chars")
                        print(f"      relay:     {len(rl_json)} chars")
                        # Find first difference
                        for i in range(min(len(nr_json), len(rl_json))):
                            if nr_json[i] != rl_json[i]:
                                ctx = 50
                                print(f"      First diff at char {i}:")
                                print(f"        non-relay: ...{nr_json[max(0,i-ctx):i+ctx]}...")
                                print(f"        relay:     ...{rl_json[max(0,i-ctx):i+ctx]}...")
                                break
                else:
                    if nr_val == rl_val:
                        print(f"    {key}: IDENTICAL ({nr_val})")
                    else:
                        print(f"    {key}: DIFFERENT! non-relay={nr_val} relay={rl_val}")

        # ----------------------------------------------------------------
        # Step 4: Test if the issue is in intercept.py's relay handling
        # ----------------------------------------------------------------
        print("\n" + "=" * 80)
        print("STEP 4: Test relay with ORIGINAL unmodified CLI body")
        print("=" * 80)
        print("  Sending the unmodified CLI body (from the log) through relay...")

        # Read the unmodified body from the proxy_requests.log
        log_file = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
        with open(log_file, "r") as f:
            log_content = f.read()

        # Find last UNMODIFIED REQUEST BODY
        marker = "UNMODIFIED REQUEST BODY:\n" + "-" * 50 + "\n"
        pos = log_content.rfind(marker)
        if pos >= 0:
            start = pos + len(marker)
            end = log_content.find("\n\n--", start)
            if end == -1:
                end = log_content.find("\n====", start)
            unmod_json = log_content[start:end].strip()
            try:
                unmod_body = json.loads(unmod_json)
                # Remove fields that cause issues
                unmod_body.pop("tool_choice", None)
                unmod_body.pop("temperature", None)
                if "anthropic_version" not in unmod_body:
                    unmod_body["anthropic_version"] = "bedrock-2023-05-31"

                # Strip cache_control like intercept.py does
                def strip_cc(obj):
                    if isinstance(obj, dict):
                        obj.pop("cache_control", None)
                        for v in obj.values():
                            strip_cc(v)
                    elif isinstance(obj, list):
                        for item in obj:
                            strip_cc(item)
                strip_cc(unmod_body)

                print(f"  Unmodified body: {len(unmod_body.get('tools',[]))} tools, "
                      f"{len(unmod_body.get('system',[]))} sys blocks")

                status2, _ = run_relay_test(unmod_body, "unmodified_cli_body_via_relay")
                if status2 == 200:
                    print("\n  Original CLI body works through relay!")
                else:
                    print(f"\n  Original CLI body also fails ({status2}) through relay.")
            except json.JSONDecodeError as e:
                print(f"  Failed to parse unmodified body: {e}")

    # ----------------------------------------------------------------
    # Step 5: Test the FIRST modified request from the log (the main AMP request)
    # ----------------------------------------------------------------
    print("\n" + "=" * 80)
    print("STEP 5: Test the ORIGINAL AMP modified request (main request)")
    print("=" * 80)

    log_file = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    with open(log_file, "r") as f:
        log_content = f.read()

    # Find the FIRST modified body
    marker = "MODIFIED REQUEST BODY:\n" + "-" * 50 + "\n"
    pos = log_content.find(marker)
    if pos >= 0:
        start = pos + len(marker)
        end = log_content.find("\n====", start)
        json_str = log_content[start:end].strip()

        # Handle potential parse issues - try to find valid JSON
        # The log might have extra content after the JSON
        brace_count = 0
        json_end = 0
        for i, c in enumerate(json_str):
            if c == '{':
                brace_count += 1
            elif c == '}':
                brace_count -= 1
                if brace_count == 0:
                    json_end = i + 1
                    break

        if json_end > 0:
            json_str = json_str[:json_end]

        try:
            amp_body = json.loads(json_str)
            print(f"  AMP modified body: {len(amp_body.get('tools',[]))} tools, "
                  f"{len(amp_body.get('system',[]))} sys blocks, "
                  f"{len(json.dumps(amp_body))} bytes")
            tool_names = [t.get("name","?") for t in amp_body.get("tools",[])]
            print(f"  Tool names: {tool_names[:5]}... (total {len(tool_names)})")

            status3, debug3 = run_relay_test(amp_body, "original_amp_modified_body")
            print(f"\n  AMP body result: {status3}")

        except json.JSONDecodeError as e:
            print(f"  Failed to parse AMP body: {e}")

    print("\n" + "=" * 80)
    print("DONE - SUMMARY")
    print("=" * 80)


if __name__ == "__main__":
    main()
