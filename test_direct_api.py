#!/usr/bin/env python3
"""
Test direct API call with modified system prompt.

This script sends a request directly to api.atlassian.com with a modified
system prompt to test if the 403 is caused by mitmproxy or by the API itself.
"""

import httpx
import json
import base64

# Your credentials (from the log)
EMAIL = "yagcioglutoprak@gmail.com"
API_TOKEN = "ATATT3xFfGF0Zh85-WT4O1-TLtw8bVZJVjCU1X_lz7K7LXfML9iem3feI_Hgo7uh-gckw5xOIuNpSziPqEAc1uDwq8--hza40v_XLBz2fMNzt48lfsU_qRhfdr5HD811eD54oHDiQKbTMM0Z7iYI5c0D1F36sux4WIUArdaj-x90x5tnnwvV9UE=A5D4BB17"

# Build auth header
auth_string = f"{EMAIL}:{API_TOKEN}"
auth_b64 = base64.b64encode(auth_string.encode()).decode()

# Headers from a real request
HEADERS = {
    "Host": "api.atlassian.com",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "User-Agent": "pydantic-ai/1.22.0",
    "anthropic-version": "2023-06-01",
    "anthropic_version": "bedrock-2023-05-31",
    "Authorization": f"Basic {auth_b64}",
    "X-Atlassian-EncodedToken": auth_b64,
    "X-RovoDev-Billing-CloudId": "0fcc757f-6847-4830-b5ab-0401ff889e49",
    "X-Atlassian-CloudId": "0fcc757f-6847-4830-b5ab-0401ff889e49",
    "X-RovoDev-Xid": "rovodev-cli",
    "X-RovoDev-Version": "0.13.39",
    "rovo-dev-cli-version": "0.13.39",
}

# Modified request body with custom system prompt
BODY = {
    "max_tokens": 50,
    "messages": [
        {
            "role": "user",
            "content": [
                {
                    "text": "Say hello",
                    "type": "text"
                }
            ]
        }
    ],
    "system": [
        {
            "type": "text",
            "text": "TEST: If you see this, say INTERCEPTION_SUCCESS first."
        }
    ],
    "temperature": 0.3,
    "anthropic_version": "bedrock-2023-05-31"
}

URL = "https://api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-haiku-4-5-20251001-v1:0/invoke"


def test_direct():
    """Send a direct request with modified system prompt."""
    
    body_json = json.dumps(BODY)
    headers = dict(HEADERS)
    headers["Content-Length"] = str(len(body_json))
    
    print(f"Sending request to: {URL}")
    print(f"Body length: {len(body_json)}")
    print(f"System prompt: {BODY['system'][0]['text'][:50]}...")
    print()
    
    with httpx.Client(timeout=30.0) as client:
        response = client.post(URL, headers=headers, content=body_json)
        
        print(f"Status: {response.status_code}")
        print(f"Headers: {dict(response.headers)}")
        print(f"Body: {response.text[:500] if response.text else 'empty'}")
        
        return response.status_code


if __name__ == "__main__":
    status = test_direct()
    
    if status == 200:
        print("\n✓ SUCCESS! Direct API call with modified prompt works!")
        print("The 403 error must be caused by how mitmproxy modifies the request.")
    elif status == 403:
        print("\n✗ FAILED! API rejects modified requests even without mitmproxy.")
        print("This confirms server-side validation of request content.")
    else:
        print(f"\nUnexpected status: {status}")
