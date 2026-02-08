#!/usr/bin/env python3
"""Test: Does lowercasing tool names / stripping $schema fix 403?"""

import json, os, re, subprocess, tempfile, time, uuid, copy

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
    fd, out = tempfile.mkstemp(suffix=".txt", prefix="tn_"); os.close(fd)
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


def strip_json_schema_fields(obj):
    """Recursively strip $schema, $defs, $ref from tool schemas."""
    if isinstance(obj, dict):
        obj.pop("$schema", None)
        # Replace $ref with the referenced definition if $defs exists
        if "$defs" in obj and "properties" in obj:
            defs = obj.pop("$defs")
            # Inline any $ref references
            def resolve_refs(o):
                if isinstance(o, dict):
                    if "$ref" in o:
                        ref = o["$ref"]
                        # e.g. "#/$defs/TodoItem"
                        if ref.startswith("#/$defs/"):
                            def_name = ref[len("#/$defs/"):]
                            if def_name in defs:
                                resolved = copy.deepcopy(defs[def_name])
                                o.clear()
                                o.update(resolved)
                                return
                    for v in o.values():
                        resolve_refs(v)
                elif isinstance(o, list):
                    for item in o:
                        resolve_refs(item)
            resolve_refs(obj)
        for v in obj.values():
            strip_json_schema_fields(v)
    elif isinstance(obj, list):
        for item in obj:
            strip_json_schema_fields(item)


def lowercase_tool_names(tools):
    """Convert tool names to lowercase_underscore format."""
    result = []
    for t in tools:
        t2 = copy.deepcopy(t)
        name = t2.get("name", "")
        # Convert CamelCase/PascalCase to snake_case
        new_name = re.sub(r'(?<!^)(?=[A-Z])', '_', name).lower()
        t2["name"] = new_name
        result.append(t2)
    return result


def main():
    base = load_working_body()
    with open("/tmp/nonrelay_body.json") as f:
        amp = json.load(f)
    amp_tools = copy.deepcopy(amp.get("tools", []))
    amp_system = copy.deepcopy(amp.get("system", []))

    print("=" * 80)
    print("TOOL NAME & SCHEMA TESTS")
    print("=" * 80)

    # Test 1: Baseline - AMP combo (expect 403)
    print("\n--- Test 1: Unsanitized AMP (expect 403) ---")
    body1 = dict(base)
    body1["tools"] = amp_tools
    body1["system"] = amp_system
    s1 = run_test(body1, "baseline_403")

    # Test 2: Lowercase tool names only
    print("\n--- Test 2: Lowercase tool names + AMP system ---")
    body2 = dict(base)
    body2["tools"] = lowercase_tool_names(amp_tools)
    body2["system"] = amp_system
    tool_names = [t["name"] for t in body2["tools"]]
    print(f"  Renamed tools: {[t for t in tool_names if t != t.lower() or '_' in t][:10]}")
    s2 = run_test(body2, "lowercase_names")

    # Test 3: Strip $schema/$defs/$ref from tool schemas
    print("\n--- Test 3: Strip $schema/$defs from tool schemas + AMP system ---")
    body3 = dict(base)
    tools3 = copy.deepcopy(amp_tools)
    for t in tools3:
        if "input_schema" in t:
            strip_json_schema_fields(t["input_schema"])
    body3["tools"] = tools3
    body3["system"] = amp_system
    s3 = run_test(body3, "no_schema_fields")

    # Test 4: Both - lowercase names + strip schema fields
    print("\n--- Test 4: Lowercase names + strip $schema + AMP system ---")
    body4 = dict(base)
    tools4 = lowercase_tool_names(copy.deepcopy(amp_tools))
    for t in tools4:
        if "input_schema" in t:
            strip_json_schema_fields(t["input_schema"])
    body4["tools"] = tools4
    body4["system"] = amp_system
    s4 = run_test(body4, "lowercase+no_schema")

    # Test 5: Strip 'title' fields from tool schemas (Bedrock might not like them)
    print("\n--- Test 5: Strip title fields from schemas + AMP system ---")
    body5 = dict(base)
    tools5 = copy.deepcopy(amp_tools)
    def strip_titles(obj):
        if isinstance(obj, dict):
            obj.pop("title", None)
            for v in obj.values(): strip_titles(v)
        elif isinstance(obj, list):
            for i in obj: strip_titles(i)
    for t in tools5:
        if "input_schema" in t:
            strip_titles(t["input_schema"])
            strip_json_schema_fields(t["input_schema"])
    body5["tools"] = lowercase_tool_names(tools5)
    body5["system"] = amp_system
    s5 = run_test(body5, "full_clean")

    # Test 6: NUCLEAR - strip EVERYTHING extra from schemas
    # Keep only: type, properties, required, items, enum, description in schemas
    print("\n--- Test 6: Minimal schemas (only type/properties/required) + AMP system ---")
    body6 = dict(base)
    ALLOWED_SCHEMA_KEYS = {"type", "properties", "required", "items", "enum",
                           "description", "anyOf", "oneOf", "allOf", "default",
                           "additionalProperties"}
    def minimize_schema(obj):
        if isinstance(obj, dict):
            for k in list(obj.keys()):
                if k.startswith("$") or k == "title":
                    del obj[k]
            for v in obj.values():
                minimize_schema(v)
        elif isinstance(obj, list):
            for i in obj: minimize_schema(i)
    tools6 = lowercase_tool_names(copy.deepcopy(amp_tools))
    for t in tools6:
        if "input_schema" in t:
            strip_json_schema_fields(t["input_schema"])
            minimize_schema(t["input_schema"])
    body6["tools"] = tools6
    body6["system"] = amp_system
    s6 = run_test(body6, "minimal_schemas")

    print("\n" + "=" * 80)
    print("SUMMARY")
    print("=" * 80)
    results = [
        ("Baseline AMP combo", s1),
        ("Lowercase tool names", s2),
        ("Strip $schema/$defs", s3),
        ("Lowercase + no $schema", s4),
        ("Full clean (no titles)", s5),
        ("Minimal schemas", s6),
    ]
    for name, status in results:
        sym = "✓" if status == 200 else "✗" if status == 403 else "?"
        print(f"  {sym} {name}: {status}")


if __name__ == "__main__":
    main()
