#!/usr/bin/env python3
"""
Compare the 403 request with successful requests to find the difference.
"""

import json
import re


def parse_log():
    with open("/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log", "r") as f:
        content = f.read()

    # Find all requests and their responses
    pattern = r"BEDROCK PROXY REQUEST - (.*?)\n.*?URL: (.*?)\n.*?MODIFIED REQUEST BODY:\n-+\n(.*?)\n-+\nRESPONSE - .*?\nStatus: (\d+)"

    matches = re.findall(pattern, content, re.DOTALL)

    print(f"Found {len(matches)} requests\n")

    for i, (timestamp, url, body_str, status) in enumerate(matches[:5]):
        print(f"Request {i + 1}: {timestamp} - Status {status}")

        # Try to parse JSON
        try:
            # Find the last valid JSON object
            last_brace = body_str.rfind("}")
            if last_brace > 0:
                json_str = body_str[: last_brace + 1]
                body = json.loads(json_str)
                tools = body.get("tools", [])
                print(f"  Tools: {len(tools)}")
                print(f"  Tool names: {[t.get('name') for t in tools][:5]}...")
        except Exception as e:
            print(f"  Parse error: {e}")
            print(f"  Body length: {len(body_str)}")


if __name__ == "__main__":
    parse_log()
