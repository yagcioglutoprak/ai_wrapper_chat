#!/usr/bin/env python3
"""
Debug the 403 error - test different request configurations.
"""

import json
import os
import sys
import subprocess
import time
import tempfile


def load_request_from_log():
    log_file = "/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log"
    with open(log_file, "r") as f:
        content = f.read()

    marker = (
        "MODIFIED REQUEST BODY:\n--------------------------------------------------\n"
    )
    start = content.rfind(marker)
    if start == -1:
        return None

    start += len(marker)
    end = content.find(
        "\n====================================================================================================",
        start,
    )
    if end == -1:
        end = len(content)

    json_str = content[start:end].strip()

    try:
        return json.loads(json_str)
    except:
        return None


def run_test(request_body, test_id):
    """Run a test and return status code."""
    queue_dir = "/Users/toprakyagcioglu/.rovodev/queue"
    raw_dir = "/Users/toprakyagcioglu/.rovodev/raw_responses"
    os.makedirs(queue_dir, exist_ok=True)
    os.makedirs(raw_dir, exist_ok=True)

    queue_file = os.path.join(queue_dir, f"relay_queue.{test_id}.json")
    capture_file = os.path.join(queue_dir, f"capture_next.{test_id}.json")
    raw_path = os.path.join(raw_dir, f"{test_id}.raw")
    meta_path = os.path.join(raw_dir, f"{test_id}.meta")

    for f in [queue_file, capture_file, raw_path, meta_path]:
        try:
            if os.path.exists(f):
                os.unlink(f)
        except:
            pass

    with open(queue_file, "w") as f:
        json.dump(request_body, f)

    with open(capture_file, "w") as f:
        json.dump(
            {"raw_path": raw_path, "meta_path": meta_path, "request_id": test_id}, f
        )

    env = os.environ.copy()
    env["ROVODEV_REQUEST_ID"] = test_id
    env["HTTPS_PROXY"] = "http://127.0.0.1:8080"
    env["HTTP_PROXY"] = "http://127.0.0.1:8080"
    env["SSL_CERT_FILE"] = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")
    env["REQUESTS_CA_BUNDLE"] = os.path.expanduser("~/.mitmproxy/mitmproxy-ca-cert.pem")

    fd, output_path = tempfile.mkstemp(suffix=".txt", prefix="test_")
    os.close(fd)
    try:
        os.unlink(output_path)
    except:
        pass

    cmd = ["acli", "rovodev", "run", ".", "--output-file", output_path, "--yolo"]

    try:
        proc = subprocess.Popen(
            cmd,
            env=env,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )

        start_time = time.time()
        while time.time() - start_time < 30:
            if os.path.exists(meta_path):
                break

            rc = proc.poll()
            if rc is not None:
                for _ in range(50):
                    if os.path.exists(meta_path):
                        break
                    time.sleep(0.05)
                break

            time.sleep(0.1)

        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=3)
            except:
                proc.kill()

        if os.path.exists(meta_path):
            with open(meta_path, "r") as f:
                meta = json.load(f)
            status = meta.get("status_code", 0)
        else:
            status = -1

        for f in [queue_file, capture_file, raw_path, meta_path]:
            try:
                if os.path.exists(f):
                    os.unlink(f)
            except:
                pass
        try:
            if os.path.exists(output_path):
                os.unlink(output_path)
        except:
            pass

        return status
    except:
        return -1


def main():
    print("=" * 80)
    print("403 Error Debugging - Testing different configurations")
    print("=" * 80)

    request_body = load_request_from_log()
    if not request_body:
        print("Failed to load request")
        return 1

    print(f"\nOriginal request keys: {list(request_body.keys())}")
    print(f"Tools count: {len(request_body.get('tools', []))}")
    print(f"Messages count: {len(request_body.get('messages', []))}")
    print(f"System blocks: {len(request_body.get('system', []))}")

    tests = [
        ("No tools at all", lambda b: {k: v for k, v in b.items() if k != "tools"}),
        ("No system prompt", lambda b: {k: v for k, v in b.items() if k != "system"}),
        (
            "No tools, no system",
            lambda b: {k: v for k, v in b.items() if k not in ["tools", "system"]},
        ),
        (
            "Minimal - only messages and max_tokens",
            lambda b: {
                "messages": b.get("messages", []),
                "max_tokens": b.get("max_tokens", 8192),
                "anthropic_version": "bedrock-2023-05-31",
            },
        ),
        (
            "Minimal + Atlassian tools",
            lambda b: {
                "messages": b.get("messages", []),
                "max_tokens": b.get("max_tokens", 8192),
                "anthropic_version": "bedrock-2023-05-31",
                "tools": [
                    t
                    for t in b.get("tools", [])
                    if t.get("name", "").startswith("mcp__atlassian")
                ],
            },
        ),
    ]

    print("\n" + "=" * 80)
    print("Running tests...")
    print("=" * 80)

    for i, (name, transform) in enumerate(tests, 1):
        print(f"\n--- Test {i}: {name} ---")

        test_body = transform(request_body)
        test_id = f"debug_{i}_{int(time.time())}"

        status = run_test(test_body, test_id)
        print(f"  Status: {status}")

        if status == 200:
            print(f"  ✓ SUCCESS!")
            with open(f"/tmp/debug_success_{i}.json", "w") as f:
                json.dump(test_body, f, indent=2)
            print(f"  Saved to: /tmp/debug_success_{i}.json")
        else:
            print(f"  ✗ Failed with status {status}")

    print("\n" + "=" * 80)
    print("Done!")
    print("=" * 80)

    return 0


if __name__ == "__main__":
    sys.exit(main())
