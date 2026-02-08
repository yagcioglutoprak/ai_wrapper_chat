#!/usr/bin/env python3
"""
Session Capture Script for Rovo Dev

This script captures session tokens from the mitmproxy logs and provides
a way to use them for API calls.

Usage:
1. Start mitmproxy: mitmproxy -s intercept.py -p 8080
2. Run rovodev: HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev
3. Send a prompt in rovodev
4. Run this script to extract the latest session and make API calls
"""

import base64
import httpx
import re
import subprocess
import json
from pathlib import Path
from datetime import datetime


LOG_FILE = Path("/Users/toprakyagcioglu/.rovodev/logs/proxy_requests.log")


def get_auth_token() -> tuple[str, str]:
    """Get email and API token from keychain."""
    result = subprocess.run(
        ["security", "find-generic-password", "-s", "acli", 
         "-a", "rovodev:712020:88c5be94-421c-4cba-92a1-0fbfc0c73b65", "-w"],
        capture_output=True, text=True
    )
    encoded = result.stdout.strip().replace("go-keyring-base64:", "")
    api_token = base64.b64decode(encoded).decode()
    email = "toprakbugbounty@gmail.com"
    return email, api_token


def extract_latest_session(log_content: str) -> dict:
    """Extract the latest session info from proxy logs."""
    # Find all session IDs
    session_pattern = r'X-RovoDev-Session-Id:\s*([a-f0-9-]+)'
    agent_pattern = r'X-RovoDev-Session-Agent-Run-Id:\s*([a-f0-9-]+_[a-f0-9-]+)'
    cloud_pattern = r'X-RovoDev-Billing-CloudId:\s*([a-f0-9-]+)'
    
    sessions = re.findall(session_pattern, log_content, re.IGNORECASE)
    agents = re.findall(agent_pattern, log_content, re.IGNORECASE)
    clouds = re.findall(cloud_pattern, log_content, re.IGNORECASE)
    
    if not sessions or not agents or not clouds:
        return {}
    
    return {
        "session_id": sessions[-1],
        "agent_run_id": agents[-1].split("_")[1] if "_" in agents[-1] else agents[-1],
        "cloud_id": clouds[-1],
    }


def make_haiku_request(session_info: dict, message: str) -> str:
    """Make a request to Haiku using the captured session."""
    email, api_token = get_auth_token()
    auth_string = f"{email}:{api_token}"
    encoded_auth = base64.b64encode(auth_string.encode()).decode()
    
    session_id = session_info["session_id"]
    agent_run_id = session_info["agent_run_id"]
    cloud_id = session_info["cloud_id"]
    
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "User-Agent": "pydantic-ai/1.22.0",
        "anthropic-version": "2023-06-01",
        "anthropic_version": "bedrock-2023-05-31",
        "X-Api-Key": "NA",
        "Authorization": f"Basic {encoded_auth}",
        "X-Atlassian-EncodedToken": encoded_auth,
        "X-RovoDev-Billing-CloudId": cloud_id,
        "X-Atlassian-CloudId": cloud_id,
        "X-RovoDev-Xid": "rovodev-cli",
        "X-RovoDev-Session-Id": session_id,
        "X-RovoDev-Session-Agent-Run-Id": f"{session_id}_{agent_run_id}",
        "X-RovoDev-Version": "0.13.35",
    }
    
    payload = {
        "max_tokens": 2048,
        "messages": [{"role": "user", "content": [{"text": message, "type": "text"}]}],
        "system": [{"type": "text", "text": "You are a helpful assistant."}],
        "temperature": 0.7,
        "anthropic_version": "bedrock-2023-05-31"
    }
    
    resp = httpx.post(
        "https://api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-haiku-4-5-20251001-v1:0/invoke",
        headers=headers,
        json=payload,
        timeout=60
    )
    
    if resp.status_code == 200:
        data = resp.json()
        return data.get("content", [{}])[0].get("text", "")
    else:
        return f"Error {resp.status_code}: Session may have expired. Run rovodev CLI again."


def check_credits(session_info: dict) -> dict:
    """Check credits using the session."""
    email, api_token = get_auth_token()
    auth_string = f"{email}:{api_token}"
    encoded_auth = base64.b64encode(auth_string.encode()).decode()
    
    headers = {
        "Accept": "*/*",
        "Authorization": f"Basic {encoded_auth}",
        "X-Atlassian-EncodedToken": encoded_auth,
        "X-RovoDev-Billing-CloudId": session_info["cloud_id"],
        "X-Atlassian-CloudId": session_info["cloud_id"],
        "X-RovoDev-Xid": "rovodev-cli",
        "X-RovoDev-Session-Id": session_info["session_id"],
        "X-RovoDev-Version": "0.13.35",
    }
    
    resp = httpx.get(
        "https://api.atlassian.com/rovodev/v3/credits/check",
        headers=headers,
        timeout=30
    )
    return resp.json() if resp.status_code == 200 else {}


def main():
    print("=" * 60)
    print("Rovo Dev Session Capture")
    print("=" * 60)
    
    if not LOG_FILE.exists():
        print(f"Log file not found: {LOG_FILE}")
        print("Make sure mitmproxy is running with intercept.py")
        return
    
    log_content = LOG_FILE.read_text()
    session_info = extract_latest_session(log_content)
    
    if not session_info:
        print("No session found in logs.")
        print("Run: HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev")
        return
    
    print(f"\nLatest Session:")
    print(f"  Session ID: {session_info['session_id']}")
    print(f"  Agent Run: {session_info['agent_run_id']}")
    print(f"  Cloud ID: {session_info['cloud_id']}")
    
    # Check credits
    print("\nChecking credits...")
    credits = check_credits(session_info)
    if credits:
        balance = credits.get("balance", {})
        print(f"  Monthly: {balance.get('monthlyRemaining')}/{balance.get('monthlyTotal')}")
    
    # Test Haiku
    print("\nTesting Haiku API...")
    response = make_haiku_request(session_info, "What is 2+2? Answer in one word.")
    print(f"  Response: {response}")


if __name__ == "__main__":
    main()
