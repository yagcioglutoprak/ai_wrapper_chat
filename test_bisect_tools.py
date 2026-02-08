#!/usr/bin/env python3
"""
Binary search tool bisection - finds which tool causes 403 error faster.
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

    # Clean up
    for f in [queue_file, capture_file, raw_path, meta_path]:
        try:
            if os.path.exists(f):
                os.unlink(f)
        except:
            pass

    # Write files
    with open(queue_file, "w") as f:
        json.dump(request_body, f)

    with open(capture_file, "w") as f:
        json.dump(
            {"raw_path": raw_path, "meta_path": meta_path, "request_id": test_id}, f
        )

    # Setup environment
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

        # Wait for meta file
        start_time = time.time()
        while time.time() - start_time < 30:  # 30 second timeout per test
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

        # Cleanup
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
    print("BINARY SEARCH Tool Bisection")
    print("=" * 80)

    request_body = load_request_from_log()
    if not request_body:
        print("Failed to load request")
        return 1

    all_tools = request_body.get("tools", [])
    print(f"Total tools: {len(all_tools)}")

    # Identify Atlassian tools
    atlassian = [t for t in all_tools if t.get("name", "").startswith("mcp__atlassian")]
    others = [
        t for t in all_tools if not t.get("name", "").startswith("mcp__atlassian")
    ]

    print(f"Atlassian tools: {len(atlassian)} (will keep)")
    print(f"Other tools: {len(others)}")

    # First test: just Atlassian tools
    print("\n" + "=" * 80)
    print("Test 1: Only Atlassian MCP tools")
    print("=" * 80)

    test_body = {k: v for k, v in request_body.items() if k != "tools"}
    test_body["tools"] = atlassian

    test_id = f"test_atl_only_{int(time.time())}"
    status = run_test(test_body, test_id)
    print(f"Status: {status}")

    if status == 200:
        print("✓ Atlassian tools alone work!")
        print("\nNow testing by adding other tools back...")

        # Add tools one by one
        working_set = atlassian.copy()

        for i, tool in enumerate(others):
            tool_name = tool.get("name", "unknown")
            print(f"\n--- Adding tool {i + 1}/{len(others)}: {tool_name} ---")

            test_body["tools"] = working_set + [tool]
            test_id = f"test_add_{i}_{int(time.time())}"
            status = run_test(test_body, test_id)

            print(f"  Status: {status}")

            if status == 200:
                print(f"  ✓ {tool_name} works, keeping it")
                working_set.append(tool)
            else:
                print(f"  ✗ {tool_name} causes error (status {status}), skipping")

        print("\n" + "=" * 80)
        print("DONE!")
        print("=" * 80)
        print(f"\nWorking tool set ({len(working_set)} tools):")
        for tool in working_set:
            print(f"  - {tool.get('name', 'unknown')}")

        with open("/tmp/working_tools.json", "w") as f:
            json.dump(working_set, f, indent=2)
        print(f"\nSaved to: /tmp/working_tools.json")

    else:
        print(f"✗ Even Atlassian tools alone get status {status}")
        print("The issue may be with the Atlassian tools or request structure")

    return 0


if __name__ == "__main__":
    sys.exit(main())
