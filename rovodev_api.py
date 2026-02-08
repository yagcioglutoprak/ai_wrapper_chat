#!/usr/bin/env python3
"""
Rovo Dev API Client - Works with existing CLI sessions.

To get a valid session:
1. Run: HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev
2. Type a prompt and let it respond
3. Copy the X-RovoDev-Session-Id and X-RovoDev-Session-Agent-Run-Id from the proxy logs

Then use this script to make API calls using that session.
"""

import base64
import httpx
import subprocess
import sys
import os
from typing import Optional
import json

# Proxy configuration
PROXY_URL = os.environ.get("HTTPS_PROXY", "http://127.0.0.1:8080")


class RovoDevClient:
    BASE_URL = "https://api.atlassian.com"

    def __init__(self, cloud_id: str, session_id: str, agent_run_id: str):
        self.cloud_id = cloud_id
        self.session_id = session_id
        self.agent_run_id = agent_run_id
        self.auth_token = self._get_auth_token()

    def _get_auth_token(self) -> str:
        """Get auth token from macOS keychain."""
        result = subprocess.run(
            ["security", "find-generic-password", "-s", "acli", "-w", "-g"],
            capture_output=True,
            text=True,
        )
        # Find the rovodev entry
        result = subprocess.run(
            ["security", "dump-keychain"], capture_output=True, text=True
        )

        # Get the specific rovodev token
        for account in ["rovodev:712020:3b9a24cb-dc1d-4a47-a200-0c2a3f0a8bd2"]:
            result = subprocess.run(
                [
                    "security",
                    "find-generic-password",
                    "-s",
                    "acli",
                    "-a",
                    account,
                    "-w",
                ],
                capture_output=True,
                text=True,
            )
            if result.returncode == 0:
                token = result.stdout.strip()
                if token.startswith("go-keyring-base64:"):
                    token = token.replace("go-keyring-base64:", "")
                return base64.b64decode(token).decode()
        raise RuntimeError("Could not find rovodev token in keychain")

    def _get_headers(self) -> dict:
        """Get base headers for API requests."""
        email = "toprakbugbounty@gmail.com"  # Change this to your email
        auth_string = f"{email}:{self.auth_token}"
        encoded_auth = base64.b64encode(auth_string.encode()).decode()

        return {
            "Authorization": f"Basic {encoded_auth}",
            "X-Atlassian-EncodedToken": encoded_auth,
            "X-RovoDev-Billing-CloudId": self.cloud_id,
            "X-Atlassian-CloudId": self.cloud_id,
            "X-RovoDev-Xid": "rovodev-cli",
            "X-RovoDev-Session-Id": self.session_id,
            "X-RovoDev-Session-Agent-Run-Id": f"{self.session_id}_{self.agent_run_id}",
            "X-RovoDev-Version": "0.13.39",
            "rovo-dev-cli-version": "0.13.39",
        }

    def check_credits(self) -> dict:
        """Check credit balance."""
        headers = self._get_headers()
        headers["Accept"] = "*/*"

        with httpx.Client(proxy=PROXY_URL, verify=False) as client:
            resp = client.get(
                f"{self.BASE_URL}/rovodev/v3/credits/check", headers=headers, timeout=30
            )
        resp.raise_for_status()
        return resp.json()

    def chat_haiku(
        self, message: str, system_prompt: str = "You are a helpful assistant."
    ) -> str:
        """Chat with Claude Haiku 4.5 - works with existing session."""
        headers = self._get_headers()
        headers.update(
            {
                "Accept": "application/json",
                "Content-Type": "application/json",
                "User-Agent": "pydantic-ai/1.22.0",
                "anthropic-version": "2023-06-01",
                "anthropic_version": "vertex-2023-10-16",
                "X-Api-Key": "NA",
            }
        )

        payload = {
            "max_tokens": 8192,
            "messages": [
                {"role": "user", "content": [{"text": message, "type": "text"}]}
            ],
            "system": [{"type": "text", "text": system_prompt}],
            "anthropic_version": "vertex-2023-10-16",
        }

        with httpx.Client(proxy=PROXY_URL, verify=False) as client:
            resp = client.post(
                f"{self.BASE_URL}/rovodev/v2/proxy/ai/v1/google/v1/publishers/anthropic/models/claude-opus-4-6:streamRawPredict",
                headers=headers,
                json=payload,
                timeout=120,
            )

        if resp.status_code != 200:
            raise RuntimeError(f"API returned {resp.status_code}: {resp.text}")

        data = resp.json()
        return data.get("content", [{}])[0].get("text", "")


def main():
    # These values come from the proxy logs after running the CLI
    # Run: HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev
    CLOUD_ID = "a9b5139f-b0b5-4945-b303-a1aa98ce6f30"
    # Using fresh session from latest CLI request
    SESSION_ID = "wae12c0b-a43b-476b-b065-5564259c12bc"
    AGENT_RUN_ID = "36df1064-a587-4f7e-ad8e-c71b37267235"

    client = RovoDevClient(CLOUD_ID, SESSION_ID, AGENT_RUN_ID)

    # Check credits
    print("Checking credits...")
    credits = client.check_credits()
    balance = credits.get("balance", {})
    print(
        f"Monthly credits: {balance.get('monthlyRemaining')}/{balance.get('monthlyTotal')}"
    )

    # Chat with Haiku
    print("\nChatting with Haiku...")
    response = client.chat_haiku("What is 2+2? Answer briefly.")
    print(f"Response: {response}")


if __name__ == "__main__":
    main()
