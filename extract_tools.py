#!/usr/bin/env python3
import sys, json, re

content = sys.stdin.read()

# Find the Request Body section
start = content.find("Request Body:")
if start == -1:
    print("Could not find 'Request Body:'")
    sys.exit(1)

# Find the end (next line of ==== or end of file)
body_start = start + len("Request Body:")
end = content.find("\n================", body_start)
if end == -1:
    end = len(content)

body_str = content[body_start:end].strip()

try:
    data = json.loads(body_str)
    # Save tools
    if "tools" in data:
        with open("/Users/toprakyagcioglu/.rovodev/claude_code_tools.json", "w") as f:
            json.dump(data["tools"], f, indent=2)
        print(f"Saved {len(data['tools'])} tools to claude_code_tools.json")

        # Also print first few tool names
        for i, t in enumerate(data["tools"][:5]):
            print(f"  {i + 1}. {t.get('name', 'unknown')}")
        if len(data["tools"]) > 5:
            print(f"  ... and {len(data['tools']) - 5} more")
    else:
        print("No tools in request")
        print(f"Available keys: {list(data.keys())}")
except Exception as e:
    print(f"Error parsing JSON: {e}")
    print(f"Body preview: {body_str[:200]}...")
