#!/usr/bin/env python3
"""Test Atlassian Rovo Dev authentication flow."""

import base64
import httpx
import json
import subprocess
import uuid

# Get token from keychain
def get_token_from_keychain():
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "acli", 
         "-a", "rovodev:712020:88c5be94-421c-4cba-92a1-0fbfc0c73b65", "-w"],
        capture_output=True, text=True
    )
    encoded = result.stdout.strip()
    if encoded.startswith("go-keyring-base64:"):
        encoded = encoded.replace("go-keyring-base64:", "")
    return base64.b64decode(encoded).decode()

# Config
EMAIL = "toprakbugbounty@gmail.com"
API_TOKEN = get_token_from_keychain()
CLOUD_ID = "a9b5139f-b0b5-4945-b303-a1aa98ce6f30"

# Build auth header
auth_string = f"{EMAIL}:{API_TOKEN}"
encoded_auth = base64.b64encode(auth_string.encode()).decode()

print(f"[*] Using email: {EMAIL}")
print(f"[*] Token (first 20 chars): {API_TOKEN[:20]}...")

headers = {
    "Authorization": f"Basic {encoded_auth}",
    "X-Atlassian-EncodedToken": encoded_auth,
    "X-RovoDev-Xid": "rovodev-cli",
    "X-RovoDev-Version": "0.13.35",
    "rovo-dev-cli-version": "0.13.35",
    "Accept": "*/*",
    "User-Agent": "python-httpx/0.28.1",
}

def test_sites():
    """Test /v3/sites endpoint."""
    print("\n[1] Testing /v3/sites...")
    resp = httpx.get(
        "https://api.atlassian.com/rovodev/v3/sites",
        headers=headers,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        print(f"    Sites: {len(data.get('sites', []))}")
        for site in data.get('sites', []):
            print(f"      - {site['siteUrl']} (CloudId: {site['siteId']})")
        return data
    else:
        print(f"    Error: {resp.text}")
    return None

def test_credits_check():
    """Test /v3/credits/check endpoint."""
    print("\n[2] Testing /v3/credits/check...")
    h = headers.copy()
    h["X-RovoDev-Billing-CloudId"] = CLOUD_ID
    h["X-Atlassian-CloudId"] = CLOUD_ID
    
    resp = httpx.get(
        "https://api.atlassian.com/rovodev/v3/credits/check",
        headers=h,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        data = resp.json()
        balance = data.get('balance', {})
        print(f"    Status: {data.get('status')}")
        print(f"    Monthly remaining: {balance.get('monthlyRemaining')}/{balance.get('monthlyTotal')}")
        return data
    else:
        print(f"    Error: {resp.text}")
    return None

def create_session():
    """Create a session via /v2/cli/events."""
    print("\n[3] Creating session via /v2/cli/events...")
    session_id = str(uuid.uuid4())
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    timestamp = "2026-02-03T12:00:00.000000Z"
    
    h = headers.copy()
    h["X-RovoDev-Billing-CloudId"] = CLOUD_ID
    h["X-Atlassian-CloudId"] = CLOUD_ID
    h["X-RovoDev-Session-Id"] = session_id
    h["Content-Type"] = "application/json"
    
    payload = {
        "sessionId": session_id,
        "traceId": trace_id,
        "action": "created",
        "attributes": {
            "sessionId": session_id,
            "isAIFeature": 0,
            "userGeneratedAI": 0,
            "aiFeatureName": "rovodevCLI",
            "singleInstrumentationID": session_id,
            "start_time": timestamp,
            "rovodev_config_llm": "anthropic.claude-opus-4-5-20251101-v1:0",
            "trace_id": trace_id,
            "span_id": span_id,
            "timestamp": timestamp
        },
        "platform": "unknown",
        "actionSubject": "llm_session"
    }
    
    resp = httpx.post(
        "https://api.atlassian.com/rovodev/v2/cli/events",
        headers=h,
        json=payload,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    print(f"    Session ID: {session_id}")
    return session_id if resp.status_code == 204 else None

def start_llm_call_event(session_id, agent_run_id):
    """Send llm_call started event before making LLM request."""
    print("\n[4] Sending llm_call started event...")
    trace_id = uuid.uuid4().hex
    span_id = uuid.uuid4().hex[:16]
    timestamp = "2026-02-03T12:00:00.000000Z"
    
    h = headers.copy()
    h["X-RovoDev-Billing-CloudId"] = CLOUD_ID
    h["X-Atlassian-CloudId"] = CLOUD_ID
    h["X-RovoDev-Session-Id"] = session_id
    h["X-RovoDev-Session-Agent-Run-Id"] = f"{session_id}_{agent_run_id}"
    h["Content-Type"] = "application/json"
    
    payload = {
        "sessionId": session_id,
        "traceId": trace_id,
        "action": "started",
        "attributes": {
            "sessionId": session_id,
            "isAIFeature": 1,
            "userGeneratedAI": 1,
            "aiFeatureName": "rovodevCLI",
            "singleInstrumentationID": session_id,
            "start_time": timestamp,
            "rovodev_config_llm": "anthropic.claude-opus-4-5-20251101-v1:0",
            "trace_id": trace_id,
            "span_id": span_id,
            "model_id": "fallback:anthropic.claude-opus-4-5-20251101-v1:0,gpt-5.2-latest",
            "timestamp": timestamp,
            "input_tokens": 0
        },
        "platform": "unknown",
        "actionSubject": "llm_call"
    }
    
    resp = httpx.post(
        "https://api.atlassian.com/rovodev/v2/cli/events",
        headers=h,
        json=payload,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    return resp.status_code == 204

def test_credits_with_session(session_id, agent_run_id):
    """Check credits with full session headers."""
    print("\n[5.5] Checking credits with session...")
    h = headers.copy()
    h["X-RovoDev-Billing-CloudId"] = CLOUD_ID
    h["X-Atlassian-CloudId"] = CLOUD_ID
    h["X-RovoDev-Session-Id"] = session_id
    h["X-RovoDev-Session-Agent-Run-Id"] = f"{session_id}_{agent_run_id}"
    
    resp = httpx.get(
        "https://api.atlassian.com/rovodev/v3/credits/check",
        headers=h,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    return resp.status_code == 200

def test_prompt_moderation(session_id, agent_run_id):
    """Test prompt moderation."""
    print("\n[5] Testing prompt moderation...")
    
    h = headers.copy()
    h["X-RovoDev-Billing-CloudId"] = CLOUD_ID
    h["X-Atlassian-CloudId"] = CLOUD_ID
    h["X-RovoDev-Session-Id"] = session_id
    h["X-RovoDev-Session-Agent-Run-Id"] = f"{session_id}_{agent_run_id}"
    h["Content-Type"] = "application/json"
    
    payload = {"prompt": "Say hello"}
    
    resp = httpx.post(
        "https://api.atlassian.com/rovodev/v2/prompt-moderation/",
        headers=h,
        json=payload,
        timeout=30
    )
    print(f"    Status: {resp.status_code}")
    if resp.status_code == 200:
        print(f"    Response: {resp.json()}")
    return resp.status_code == 200

def test_llm_request(session_id, agent_run_id):
    """Test LLM proxy endpoint."""
    print("\n[6] Testing LLM proxy endpoint (Haiku)...")
    
    # Build headers in exact order from successful request
    h = {
        "Host": "api.atlassian.com",
        "Accept-Encoding": "gzip, deflate, zstd",
        "Connection": "keep-alive",
        "x-stainless-timeout": "600",
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "pydantic-ai/1.22.0",
        "X-Stainless-Lang": "python",
        "X-Stainless-Package-Version": "0.72.0",
        "X-Stainless-OS": "MacOS",
        "X-Stainless-Arch": "arm64",
        "X-Stainless-Runtime": "CPython",
        "X-Stainless-Runtime-Version": "3.13.1",
        "X-Api-Key": "NA",
        "X-Stainless-Async": "async:asyncio",
        "anthropic-version": "2023-06-01",
        "x-stainless-retry-count": "0",
        "x-stainless-read-timeout": "600",
        "anthropic_version": "bedrock-2023-05-31",
        "Authorization": f"Basic {encoded_auth}",
        "X-Atlassian-EncodedToken": encoded_auth,
        "X-RovoDev-Billing-CloudId": CLOUD_ID,
        "X-Atlassian-CloudId": CLOUD_ID,
        "X-RovoDev-Xid": "rovodev-cli",
        "X-RovoDev-Session-Id": session_id,
        "X-RovoDev-Session-Agent-Run-Id": f"{session_id}_{agent_run_id}",
        "X-RovoDev-Version": "0.13.35",
    }
    
    payload = {
        "max_tokens": 50,
        "messages": [
            {"role": "user", "content": [{"text": "Say hello", "type": "text"}]}
        ],
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "temperature": 0.3,
        "anthropic_version": "bedrock-2023-05-31"
    }
    
    resp = httpx.post(
        "https://api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-haiku-4-5-20251001-v1:0/invoke",
        headers=h,
        json=payload,
        timeout=60
    )
    print(f"    Status: {resp.status_code}")
    print(f"    Response Headers: {dict(resp.headers)}")
    print(f"    Response: {resp.text[:1000] if resp.text else 'empty'}")
    return resp

if __name__ == "__main__":
    print("=" * 60)
    print("Rovo Dev Authentication Test")
    print("=" * 60)
    
    test_sites()
    test_credits_check()
    session_id = create_session()
    if session_id:
        agent_run_id = str(uuid.uuid4())
        if start_llm_call_event(session_id, agent_run_id):
            test_prompt_moderation(session_id, agent_run_id)
            # Check credits again with session headers before LLM call
            test_credits_with_session(session_id, agent_run_id)
            test_llm_request(session_id, agent_run_id)
