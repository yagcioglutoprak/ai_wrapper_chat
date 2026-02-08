#!/usr/bin/env python3
"""Test: AMP tools + AMP system = 403?"""

import json, os, subprocess, tempfile, time, uuid

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
    fd, out = tempfile.mkstemp(suffix=".txt", prefix="tc_"); os.close(fd)
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
    log = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    with open(log) as f:
        content = f.read()
    marker = "UNMODIFIED REQUEST BODY:\n" + "-" * 50 + "\n"
    pos = content.rfind(marker)
    if pos < 0: return None
    start = pos + len(marker)
    end = content.find("\n\n--", start)
    if end == -1: end = content.find("\n====", start)
    body = json.loads(content[start:end].strip())
    body.pop("tool_choice", None); body.pop("temperature", None)
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


def main():
    base = load_working_body()
    with open("/tmp/nonrelay_body.json") as f:
        amp = json.load(f)
    amp_tools = amp.get("tools", [])
    amp_system = amp.get("system", [])
    rovodev_tools = base.get("tools", [])
    rovodev_system = base.get("system", [])

    print("=" * 80)
    print("COMBINATION TESTS")
    print("=" * 80)

    # Test 1: AMP tools + AMP system = the full combo
    print("\n--- Test 1: AMP tools + AMP system (full combo) ---")
    body1 = dict(base)
    body1["tools"] = amp_tools
    body1["system"] = amp_system
    s1 = run_test(body1, "amp_tools+amp_system")

    if s1 == 403:
        print("\n  → COMBINATION of AMP tools + AMP system = 403!")
        print("\n  Now testing: which system block(s) + AMP tools cause 403?")

        # Test 2: AMP tools + first system block only
        print("\n--- Test 2: AMP tools + system[0] only ---")
        body2 = dict(base)
        body2["tools"] = amp_tools
        body2["system"] = [amp_system[0]]
        s2 = run_test(body2, "amp_tools+sys[0]")

        # Test 3: AMP tools + first 2 system blocks
        print("\n--- Test 3: AMP tools + system[0:2] ---")
        body3 = dict(base)
        body3["tools"] = amp_tools
        body3["system"] = amp_system[:2]
        s3 = run_test(body3, "amp_tools+sys[0:2]")

        # Test 4: AMP tools + first 3 system blocks
        print("\n--- Test 4: AMP tools + system[0:3] ---")
        body4 = dict(base)
        body4["tools"] = amp_tools
        body4["system"] = amp_system[:3]
        s4 = run_test(body4, "amp_tools+sys[0:3]")

        # Test 5: AMP tools + first 4 system blocks
        print("\n--- Test 5: AMP tools + system[0:4] ---")
        body5 = dict(base)
        body5["tools"] = amp_tools
        body5["system"] = amp_system[:4]
        s5 = run_test(body5, "amp_tools+sys[0:4]")

        # Test 6: AMP tools + first 5 system blocks
        print("\n--- Test 6: AMP tools + system[0:5] ---")
        body6 = dict(base)
        body6["tools"] = amp_tools
        body6["system"] = amp_system[:5]
        s6 = run_test(body6, "amp_tools+sys[0:5]")

        # Test 7: Full body size test - AMP tools + padded RovoDev system to same size
        total_amp_sys_chars = sum(len(b.get("text", "")) for b in amp_system)
        total_rov_sys_chars = sum(len(b.get("text", "")) for b in rovodev_system)
        pad = total_amp_sys_chars - total_rov_sys_chars
        print(f"\n--- Test 7: AMP tools + padded RovoDev system (+{pad} chars) ---")
        padded = [dict(b) for b in rovodev_system]
        if padded:
            padded[0] = dict(padded[0])
            padded[0]["text"] = padded[0].get("text", "") + "\n" + "x" * max(0, pad)
        body7 = dict(base)
        body7["tools"] = amp_tools
        body7["system"] = padded
        s7 = run_test(body7, f"amp_tools+padded_rov_sys")

        # Test 8: TOTAL body size = amp size, RovoDev tools + padded AMP system
        amp_full_size = len(json.dumps(body1))
        current_size = len(json.dumps(body1))
        print(f"\n  Full AMP+AMP body size: {amp_full_size}")

    elif s1 == 200:
        print("\n  → Even full combo works! The issue might be something else...")
        print("  Let me send the EXACT nonrelay_body.json directly")

        # Test: send the exact nonrelay body
        print("\n--- Test 2: Exact nonrelay_body.json ---")
        body_exact = dict(amp)
        # Remove disallowed keys
        for k in list(body_exact.keys()):
            if k not in ("system", "tools", "messages", "max_tokens", "anthropic_version", "model"):
                body_exact.pop(k)
        if "anthropic_version" not in body_exact:
            body_exact["anthropic_version"] = "bedrock-2023-05-31"
        body_exact.pop("model", None)
        def strip_cc(obj):
            if isinstance(obj, dict):
                obj.pop("cache_control", None)
                for v in obj.values(): strip_cc(v)
            elif isinstance(obj, list):
                for i in obj: strip_cc(i)
        strip_cc(body_exact)
        s2 = run_test(body_exact, "exact_nonrelay")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
