#!/usr/bin/env python3
"""Test: Does applying Amp→opencode + URL stripping to AMP system fix 403?"""

import json, os, re, subprocess, tempfile, time, uuid

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
    fd, out = tempfile.mkstemp(suffix=".txt", prefix="ts_"); os.close(fd)
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


def sanitize_system(system_blocks):
    """Apply the same sanitization as the non-relay path."""
    sanitized = []
    for block in system_blocks:
        b = dict(block)
        if "text" in b:
            text = b["text"]
            # Strip URLs (same regex as non-relay path)
            text = re.sub(r'https?://[^\s\)\]\}\>"\',`]+', '', text)
            # Rename Amp → opencode (same as non-relay path)
            text = text.replace("ampcode", "opencode").replace("Ampcode", "Opencode")
            text = re.sub(r'\bAmp\b', 'opencode', text)
            text = re.sub(r'\bamp\b', 'opencode', text)
            b["text"] = text
        sanitized.append(b)
    return sanitized


def sanitize_tools(tools):
    """Apply same sanitization to tool descriptions too."""
    sanitized = []
    for tool in tools:
        t = json.loads(json.dumps(tool))  # deep copy
        if "description" in t:
            desc = t["description"]
            desc = re.sub(r'https?://[^\s\)\]\}\>"\',`]+', '', desc)
            desc = desc.replace("ampcode", "opencode").replace("Ampcode", "Opencode")
            desc = re.sub(r'\bAmp\b', 'opencode', desc)
            desc = re.sub(r'\bamp\b', 'opencode', desc)
            t["description"] = desc
        sanitized.append(t)
    return sanitized


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

    print("=" * 80)
    print("SANITIZATION TESTS")
    print("=" * 80)

    # Test 1: Baseline - unsanitized AMP combo (expect 403)
    print("\n--- Test 1: Unsanitized AMP tools + AMP system (expect 403) ---")
    body1 = dict(base)
    body1["tools"] = amp_tools
    body1["system"] = amp_system
    s1 = run_test(body1, "unsanitized")

    # Test 2: Sanitize ONLY system prompt (URLs + Amp→opencode)
    print("\n--- Test 2: AMP tools + SANITIZED AMP system ---")
    body2 = dict(base)
    body2["tools"] = amp_tools
    body2["system"] = sanitize_system(amp_system)
    s2 = run_test(body2, "sanitized_system_only")

    # Test 3: Sanitize system + tools
    print("\n--- Test 3: SANITIZED AMP tools + SANITIZED AMP system ---")
    body3 = dict(base)
    body3["tools"] = sanitize_tools(amp_tools)
    body3["system"] = sanitize_system(amp_system)
    s3 = run_test(body3, "sanitized_both")

    # Test 4: Just strip URLs from system (no Amp renaming)
    print("\n--- Test 4: AMP tools + URL-stripped AMP system (no Amp rename) ---")
    body4 = dict(base)
    sys4 = []
    for block in amp_system:
        b = dict(block)
        if "text" in b:
            b["text"] = re.sub(r'https?://[^\s\)\]\}\>"\',`]+', '', b["text"])
        sys4.append(b)
    body4["tools"] = amp_tools
    body4["system"] = sys4
    s4 = run_test(body4, "url_stripped_only")

    # Test 5: Just rename Amp→opencode (no URL stripping)
    print("\n--- Test 5: AMP tools + Amp-renamed AMP system (no URL strip) ---")
    body5 = dict(base)
    sys5 = []
    for block in amp_system:
        b = dict(block)
        if "text" in b:
            text = b["text"]
            text = text.replace("ampcode", "opencode").replace("Ampcode", "Opencode")
            text = re.sub(r'\bAmp\b', 'opencode', text)
            text = re.sub(r'\bamp\b', 'opencode', text)
            b["text"] = text
        sys5.append(b)
    body5["tools"] = amp_tools
    body5["system"] = sys5
    s5 = run_test(body5, "amp_renamed_only")

    # Test 6: Consolidate system to single block (like non-relay does)
    print("\n--- Test 6: AMP tools + consolidated system (1 block, sanitized) ---")
    body6 = dict(base)
    combined_text = "\n\n".join(b.get("text", "") for b in amp_system if b.get("text"))
    combined_text = re.sub(r'https?://[^\s\)\]\}\>"\',`]+', '', combined_text)
    combined_text = combined_text.replace("ampcode", "opencode").replace("Ampcode", "Opencode")
    combined_text = re.sub(r'\bAmp\b', 'opencode', combined_text)
    combined_text = re.sub(r'\bamp\b', 'opencode', combined_text)
    body6["tools"] = amp_tools
    body6["system"] = [{"type": "text", "text": combined_text}]
    s6 = run_test(body6, "consolidated_sanitized")

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    results = [
        ("Unsanitized AMP combo", s1),
        ("Sanitized system only", s2),
        ("Sanitized both", s3),
        ("URL stripped only", s4),
        ("Amp renamed only", s5),
        ("Consolidated + sanitized", s6),
    ]
    for name, status in results:
        sym = "✓" if status == 200 else "✗" if status == 403 else "?"
        print(f"  {sym} {name}: {status}")


if __name__ == "__main__":
    main()
