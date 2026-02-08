#!/usr/bin/env python3
"""Test: Is there a tool count limit with AMP system? Binary search."""

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
    fd, out = tempfile.mkstemp(suffix=".txt", prefix="tc2_"); os.close(fd)
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

    # Separate MCP tools
    amp_non_mcp = [t for t in amp_tools if not t.get("name","").startswith("mcp__")]
    amp_mcp = [t for t in amp_tools if t.get("name","").startswith("mcp__")]

    print("=" * 80)
    print("TOOL COUNT BINARY SEARCH (with AMP system)")
    print(f"AMP non-MCP tools ({len(amp_non_mcp)}): {[t['name'] for t in amp_non_mcp]}")
    print(f"AMP MCP tools ({len(amp_mcp)}): {[t['name'] for t in amp_mcp]}")
    print("=" * 80)

    # Test increasing subsets of AMP tools + AMP system
    # Always include MCP tools at the end
    counts_to_test = [0, 2, 5, 8, 10, 12, 14, 16, 18, 20]

    for count in counts_to_test:
        subset = amp_non_mcp[:count] + amp_mcp
        body = dict(base)
        body["tools"] = copy.deepcopy(subset)
        body["system"] = amp_system
        names = [t["name"] for t in subset if not t["name"].startswith("mcp__")]
        s = run_test(body, f"{count}_amp+mcp")
        if s == 200:
            print(f"  → {count} AMP tools + MCP + AMP system = OK!")
        else:
            print(f"  → FAILS at {count} AMP tools! Previous count worked.")
            # Now binary search between last working and this count
            if count > 0:
                lo = counts_to_test[counts_to_test.index(count) - 1]
                hi = count
                print(f"\n  Binary searching between {lo} and {hi}...")
                while lo + 1 < hi:
                    mid = (lo + hi) // 2
                    subset2 = amp_non_mcp[:mid] + amp_mcp
                    body2 = dict(base)
                    body2["tools"] = copy.deepcopy(subset2)
                    body2["system"] = amp_system
                    s2 = run_test(body2, f"bisect_{mid}")
                    if s2 == 200:
                        lo = mid
                    else:
                        hi = mid
                print(f"\n  → Threshold: {lo} tools OK, {hi} tools FAIL")
                # Print the tool that causes the break
                if hi <= len(amp_non_mcp):
                    print(f"  → Breaking tool: {amp_non_mcp[hi-1]['name']}")
                    # Also try: skip that tool and continue
                    skip_tools = amp_non_mcp[:hi-1] + amp_non_mcp[hi:hi+2] + amp_mcp
                    body3 = dict(base)
                    body3["tools"] = copy.deepcopy(skip_tools)
                    body3["system"] = amp_system
                    run_test(body3, f"skip_{amp_non_mcp[hi-1]['name']}")
            break

    print("\n" + "=" * 80)
    print("DONE")
    print("=" * 80)


if __name__ == "__main__":
    main()
