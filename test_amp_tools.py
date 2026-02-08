#!/usr/bin/env python3
"""
Test the 403 error with all AMP tools.
Uses the extracted request from the log.
"""

import json
import os
import sys
import subprocess
import time
import tempfile


def run_test(request_body, test_id):
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
    print("TESTING ALL AMP TOOLS")
    print("=" * 80)

    # Load the extracted request
    try:
        with open("/tmp/full_request.json", "r") as f:
            content = f.read()
            # Find the last complete JSON object (ends with })
            last_brace = content.rfind("}")
            if last_brace > 0:
                json_str = content[: last_brace + 1]
                request_body = json.loads(json_str)
            else:
                request_body = json.loads(content)
    except Exception as e:
        print(f"Failed to load request: {e}")
        return 1

    all_tools = request_body.get("tools", [])
    print(f"\nLoaded request with {len(all_tools)} tools")

    # Separate Atlassian and AMP tools
    atlassian = [t for t in all_tools if t.get("name", "").startswith("mcp__atlassian")]
    amp_tools = [
        t for t in all_tools if not t.get("name", "").startswith("mcp__atlassian")
    ]

    print(f"Atlassian tools: {len(atlassian)}")
    print(f"AMP tools: {len(amp_tools)}")

    print("\n" + "=" * 80)
    print("Testing full request with ALL tools...")
    print("=" * 80)

    test_id = f"all_tools_{int(time.time())}"
    status = run_test(request_body, test_id)
    print(f"Status: {status}")

    if status == 200:
        print("\n✓ SUCCESS! All tools work!")
        return 0

    print(f"\n✗ Got {status} with all tools")
    print("\nRemoving AMP tools one by one...")
    print("=" * 80)

    # Remove one by one
    remaining_amp = amp_tools.copy()

    for i, tool in enumerate(amp_tools):
        tool_name = tool.get("name", "unknown")
        remaining_amp.pop(0)

        print(f"\n--- Step {i + 1}/{len(amp_tools)}: Without {tool_name} ---")

        test_body = {k: v for k, v in request_body.items() if k != "tools"}
        test_body["tools"] = remaining_amp + atlassian

        status = run_test(test_body, f"step_{i}_{int(time.time())}")
        print(f"Status: {status}")

        if status == 200:
            print("\n" + "=" * 80)
            print("✓ FOUND IT!")
            print("=" * 80)
            print(f"Problematic tool: {tool_name}")
            print(f"\nWorking tools ({len(remaining_amp) + len(atlassian)} total):")
            for t in remaining_amp + atlassian:
                print(f"  - {t.get('name', 'unknown')}")

            with open("/tmp/working_config.json", "w") as f:
                json.dump(remaining_amp + atlassian, f, indent=2)
            print(f"\nSaved to: /tmp/working_config.json")
            return 0

    print("\n" + "=" * 80)
    print("REMOVED ALL AMP TOOLS - STILL FAILING")
    print("=" * 80)
    return 1


if __name__ == "__main__":
    sys.exit(main())
