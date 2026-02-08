#!/usr/bin/env python3
"""Test: Does adding RovoDev tools to AMP request fix 403?
The gateway likely REQUIRES specific RovoDev tool names to be present."""

import json, os, subprocess, tempfile, time, uuid, copy

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
    fd, out = tempfile.mkstemp(suffix=".txt", prefix="rr_"); os.close(fd)
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
    rovodev_non_mcp = [t for t in rovodev_tools if not t.get("name","").startswith("mcp__")]
    rovodev_mcp = [t for t in rovodev_tools if t.get("name","").startswith("mcp__")]
    amp_non_mcp = [t for t in amp_tools if not t.get("name","").startswith("mcp__")]

    print("=" * 80)
    print("ROVODEV TOOLS REQUIRED TEST")
    print(f"RovoDev tools: {[t['name'] for t in rovodev_non_mcp]}")
    print("=" * 80)

    # Test 1: RovoDev tools + MCP + AMP system (known working)
    print("\n--- Test 1: RovoDev tools + MCP + AMP system (baseline) ---")
    body1 = dict(base)
    body1["system"] = amp_system
    s1 = run_test(body1, "rovodev_baseline")

    # Test 2: ALL tools (RovoDev + AMP + MCP) + AMP system
    print("\n--- Test 2: RovoDev + AMP tools + AMP system ---")
    # Deduplicate: remove AMP tools that share names with RovoDev tools
    rovodev_names = {t["name"] for t in rovodev_tools}
    extra_amp = [t for t in amp_non_mcp if t["name"] not in rovodev_names]
    combined = copy.deepcopy(rovodev_non_mcp) + copy.deepcopy(extra_amp) + copy.deepcopy(rovodev_mcp)
    body2 = dict(base)
    body2["tools"] = combined
    body2["system"] = amp_system
    print(f"  Total tools: {len(combined)} ({len(rovodev_non_mcp)} rovodev + {len(extra_amp)} amp + {len(rovodev_mcp)} mcp)")
    s2 = run_test(body2, "rovodev+amp_all")

    if s2 == 200:
        print("\n  ✓✓✓ Adding RovoDev tools fixes 403! ✓✓✓")
        print("\n  Now finding MINIMUM RovoDev tools needed...")

        # Test 3: Find minimum RovoDev tools needed
        # Binary search: how many RovoDev tools do we need?
        for i in range(len(rovodev_non_mcp)):
            subset = rovodev_non_mcp[:i+1]
            combined3 = copy.deepcopy(subset) + copy.deepcopy(amp_non_mcp) + copy.deepcopy(rovodev_mcp)
            # Deduplicate
            seen = set()
            deduped = []
            for t in combined3:
                if t["name"] not in seen:
                    seen.add(t["name"])
                    deduped.append(t)
            body3 = dict(base)
            body3["tools"] = deduped
            body3["system"] = amp_system
            s3 = run_test(body3, f"min_rovodev_{i+1}_{subset[-1]['name']}")
            if s3 == 200:
                print(f"\n  → Minimum RovoDev tools needed: {i+1}")
                print(f"  → Required tools: {[t['name'] for t in subset]}")
                break
        else:
            print("\n  → All RovoDev tools needed!")

        # Test 4: Just the bash tool (since non-relay path inserts it at index 8)
        print("\n--- Test 4: Just RovoDev 'bash' tool + AMP tools + AMP system ---")
        bash_tool = next((t for t in rovodev_non_mcp if t["name"] == "bash"), None)
        if bash_tool:
            combined4 = [copy.deepcopy(bash_tool)] + copy.deepcopy(amp_non_mcp) + copy.deepcopy(rovodev_mcp)
            body4 = dict(base)
            body4["tools"] = combined4
            body4["system"] = amp_system
            s4 = run_test(body4, "just_bash+amp")
    else:
        print("\n  ✗ Even with RovoDev tools added, still 403!")
        print("  The issue might be the total size or something else.")

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
