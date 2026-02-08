#!/usr/bin/env python3
"""
Test rovodev_server.py API with varying numbers of tools.
Sends non-streaming requests with 1, 10, 20, and 30 tools.
"""

import json
import time
import requests

BASE_URL = "http://localhost:8000"


def make_tool(name: str, description: str, properties: dict = None) -> dict:
    """Generate an Anthropic-style tool definition."""
    if properties is None:
        properties = {
            "input": {
                "type": "string",
                "description": f"Input for {name}",
            }
        }
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": properties,
            "required": list(properties.keys()),
        },
    }


def generate_tools(n: int) -> list:
    """Generate n unique tool definitions."""
    tool_defs = [
        ("get_weather", "Get current weather for a location", {"location": {"type": "string", "description": "City name"}}),
        ("search_web", "Search the web for information", {"query": {"type": "string", "description": "Search query"}}),
        ("read_file", "Read contents of a file", {"path": {"type": "string", "description": "File path"}}),
        ("write_file", "Write content to a file", {"path": {"type": "string", "description": "File path"}, "content": {"type": "string", "description": "File content"}}),
        ("list_files", "List files in a directory", {"directory": {"type": "string", "description": "Directory path"}}),
        ("run_command", "Execute a shell command", {"command": {"type": "string", "description": "Shell command"}}),
        ("calculate", "Perform a mathematical calculation", {"expression": {"type": "string", "description": "Math expression"}}),
        ("translate", "Translate text between languages", {"text": {"type": "string", "description": "Text to translate"}, "target_lang": {"type": "string", "description": "Target language"}}),
        ("send_email", "Send an email message", {"to": {"type": "string", "description": "Recipient email"}, "subject": {"type": "string", "description": "Email subject"}}),
        ("get_time", "Get current time in a timezone", {"timezone": {"type": "string", "description": "Timezone name"}}),
        ("create_task", "Create a new task", {"title": {"type": "string", "description": "Task title"}, "priority": {"type": "string", "description": "Priority level"}}),
        ("delete_task", "Delete a task by ID", {"task_id": {"type": "string", "description": "Task ID"}}),
        ("update_task", "Update an existing task", {"task_id": {"type": "string", "description": "Task ID"}, "status": {"type": "string", "description": "New status"}}),
        ("get_stock_price", "Get stock price for a ticker", {"ticker": {"type": "string", "description": "Stock ticker symbol"}}),
        ("resize_image", "Resize an image", {"path": {"type": "string", "description": "Image path"}, "width": {"type": "integer", "description": "New width"}}),
        ("compress_file", "Compress a file", {"path": {"type": "string", "description": "File to compress"}, "format": {"type": "string", "description": "Compression format"}}),
        ("parse_json", "Parse and validate JSON", {"json_string": {"type": "string", "description": "JSON string to parse"}}),
        ("format_code", "Format source code", {"code": {"type": "string", "description": "Code to format"}, "language": {"type": "string", "description": "Programming language"}}),
        ("git_status", "Get git repository status", {"repo_path": {"type": "string", "description": "Repository path"}}),
        ("git_commit", "Commit changes to git", {"message": {"type": "string", "description": "Commit message"}}),
        ("database_query", "Run a database query", {"query": {"type": "string", "description": "SQL query"}, "database": {"type": "string", "description": "Database name"}}),
        ("http_request", "Make an HTTP request", {"url": {"type": "string", "description": "Request URL"}, "method": {"type": "string", "description": "HTTP method"}}),
        ("encrypt_text", "Encrypt text with a key", {"text": {"type": "string", "description": "Text to encrypt"}, "key": {"type": "string", "description": "Encryption key"}}),
        ("decrypt_text", "Decrypt text with a key", {"text": {"type": "string", "description": "Text to decrypt"}, "key": {"type": "string", "description": "Decryption key"}}),
        ("generate_uuid", "Generate a UUID", {"version": {"type": "integer", "description": "UUID version (1 or 4)"}}),
        ("hash_text", "Hash text with specified algorithm", {"text": {"type": "string", "description": "Text to hash"}, "algorithm": {"type": "string", "description": "Hash algorithm"}}),
        ("convert_units", "Convert between units", {"value": {"type": "number", "description": "Value to convert"}, "from_unit": {"type": "string", "description": "Source unit"}}),
        ("regex_match", "Match text against a regex pattern", {"text": {"type": "string", "description": "Text to match"}, "pattern": {"type": "string", "description": "Regex pattern"}}),
        ("base64_encode", "Base64 encode text", {"text": {"type": "string", "description": "Text to encode"}}),
        ("base64_decode", "Base64 decode text", {"text": {"type": "string", "description": "Text to decode"}}),
    ]

    tools = []
    for i in range(n):
        if i < len(tool_defs):
            name, desc, props = tool_defs[i]
            tools.append(make_tool(name, desc, props))
        else:
            # Generate additional tools beyond predefined ones
            tools.append(make_tool(
                f"custom_tool_{i}",
                f"Custom tool number {i} for testing",
                {"input": {"type": "string", "description": f"Input for custom tool {i}"}},
            ))
    return tools


def test_with_tools(num_tools: int) -> dict:
    """Send a request with the specified number of tools and return results."""
    tools = generate_tools(num_tools)

    payload = {
        "model": "claude-opus-4-6",
        "max_tokens": 1024,
        "stream": False,
        "messages": [
            {
                "role": "user",
                "content": (
                    f"I'm testing tool passthrough. I sent {num_tools} custom tool(s). "
                    f"Please list ALL the tool names you have access to. "
                    f"Do NOT use any tools — just respond with text listing every tool name you see."
                ),
            }
        ],
        "tools": tools,
    }

    print(f"\n{'='*70}")
    print(f"TEST: {num_tools} tool(s)")
    print(f"{'='*70}")
    print(f"Payload size: {len(json.dumps(payload)):,} bytes")
    print(f"Tools: {[t['name'] for t in tools[:5]]}{'...' if num_tools > 5 else ''}")
    print(f"Sending request...")

    start = time.time()
    try:
        resp = requests.post(
            f"{BASE_URL}/v1/messages",
            json=payload,
            headers={"Content-Type": "application/json"},
            timeout=660,
        )
        elapsed = time.time() - start

        print(f"Status: {resp.status_code}")
        print(f"Time: {elapsed:.1f}s")

        if resp.status_code == 200:
            body = resp.json()
            content = body.get("content", [])
            text_parts = [b["text"] for b in content if b.get("type") == "text"]
            tool_uses = [b for b in content if b.get("type") == "tool_use"]
            usage = body.get("usage", {})

            print(f"Response type: {body.get('type')}")
            print(f"Stop reason: {body.get('stop_reason')}")
            print(f"Input tokens: {usage.get('input_tokens', 'N/A')}")
            print(f"Output tokens: {usage.get('output_tokens', 'N/A')}")
            print(f"Text blocks: {len(text_parts)}")
            print(f"Tool use blocks: {len(tool_uses)}")
            if text_parts:
                full_response = " ".join(text_parts)
                preview = full_response[:300]
                print(f"Response preview: {preview}")

                # Check which of our custom tool names appear in the response
                expected_names = [t["name"] for t in tools]
                found = [n for n in expected_names if n in full_response]
                cli_default_names = ["read", "edit", "write", "glob", "grep", "bash",
                                     "task", "todowrite", "webfetch", "question", "skill",
                                     "open_files", "create_file", "find_and_replace_code",
                                     "expand_code_chunks", "expand_folder"]
                cli_found = [n for n in cli_default_names if n in full_response]

                print(f"Client tools mentioned: {len(found)}/{len(expected_names)} — {found[:10]}")
                if cli_found:
                    print(f"CLI default tools mentioned: {cli_found}")
                if found:
                    print(f"PASS: Client tools are being passed through!")
                else:
                    print(f"WARN: No client tool names found in response")

            if tool_uses:
                for tu in tool_uses:
                    print(f"  Tool call: {tu.get('name')}({json.dumps(tu.get('input', {}))[:100]})")

            return {"status": "success", "code": 200, "elapsed": elapsed, "body": body}
        else:
            print(f"Error response: {resp.text[:500]}")
            return {"status": "error", "code": resp.status_code, "elapsed": elapsed, "error": resp.text[:500]}

    except requests.exceptions.Timeout:
        elapsed = time.time() - start
        print(f"TIMEOUT after {elapsed:.1f}s")
        return {"status": "timeout", "elapsed": elapsed}
    except requests.exceptions.ConnectionError as e:
        elapsed = time.time() - start
        print(f"CONNECTION ERROR: {e}")
        return {"status": "connection_error", "elapsed": elapsed, "error": str(e)}
    except Exception as e:
        elapsed = time.time() - start
        print(f"UNEXPECTED ERROR: {e}")
        return {"status": "error", "elapsed": elapsed, "error": str(e)}


def main():
    print("Testing rovodev_server.py API with multiple tools")
    print(f"Server: {BASE_URL}")

    # Check health first
    try:
        health = requests.get(f"{BASE_URL}/health", timeout=5).json()
        print(f"Server health: {health.get('status')}")
        print(f"Proxy: {health.get('proxy')}")
        if health.get("rate_limited"):
            print(f"WARNING: Server is rate limited! Remaining: {health.get('rate_limit_remaining_s')}s")
            return
    except Exception as e:
        print(f"Cannot reach server: {e}")
        return

    tool_counts = [30, 10, 20, 30]
    results = {}

    for count in tool_counts:
        results[count] = test_with_tools(count)
        # Wait between tests to avoid rate limiting
        if count != tool_counts[-1]:
            print(f"\nWaiting 5s before next test...")
            time.sleep(5)

    # Summary
    print(f"\n{'='*70}")
    print("SUMMARY")
    print(f"{'='*70}")
    print(f"{'Tools':<10} {'Status':<20} {'HTTP Code':<12} {'Time (s)':<10}")
    print(f"{'-'*52}")
    for count in tool_counts:
        r = results[count]
        print(f"{count:<10} {r['status']:<20} {r.get('code', 'N/A'):<12} {r['elapsed']:.1f}")


if __name__ == "__main__":
    main()
