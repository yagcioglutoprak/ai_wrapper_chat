#!/bin/bash

response=$(curl -v --http1.1 -X POST "https://api.atlassian.com/rovodev/v2/proxy/ai/v1/bedrock/model/anthropic.claude-opus-4-5-20251101-v1:0/invoke-with-response-stream" \
  -H "Host: api.atlassian.com" \
  -H "Accept-Encoding: gzip, deflate, zstd" \
  -H "Connection: keep-alive" \
  -H "x-stainless-timeout: NOT_GIVEN" \
  -H "Accept: application/json" \
  -H "Content-Type: application/json" \
  -H "User-Agent: pydantic-ai/1.22.0" \
  -H "X-Stainless-Lang: python" \
  -H "X-Stainless-Package-Version: 0.72.0" \
  -H "X-Stainless-OS: MacOS" \
  -H "X-Stainless-Arch: arm64" \
  -H "X-Stainless-Runtime: CPython" \
  -H "X-Stainless-Runtime-Version: 3.13.1" \
  -H "X-Api-Key: NA" \
  -H "X-Stainless-Async: async:asyncio" \
  -H "anthropic-version: 2023-06-01" \
  -H "x-stainless-retry-count: 0" \
  -H "x-stainless-read-timeout: 600" \
  -H "anthropic_version: bedrock-2023-05-31" \
  -H "Authorization: dG9wcmFrYnVnYm91bnR5QGdtYWlsLmNvbTpBVEFUVDN4RmZHRjBkT3pRS0tBRVg3amFueHQwRFBRMUlmY01ieW85RjBpRlYwY0YtOTQzQVVBZkZWWHBmbE96ZkNZbkdkNlBSLWY2UkRDMl93TXJWbGpMUU1pRjFBZHBRZ1lmUFpXZTVFZWRUWEFBZTlqVllDbk1qd3JEVlFOWnpzXzFWWnJfYk45ckJFemRhX3hEcU5RTzlyTGM4bF9MRTlWZVhWUTdSQUVfaWJYODFzTHVfTVU9NjI0ODdEQzI=" \
  -H "X-Atlassian-EncodedToken: dG9wcmFrYnVnYm91bnR5QGdtYWlsLmNvbTpBVEFUVDN4RmZHRjBkT3pRS0tBRVg3amFueHQwRFBRMUlmY01ieW85RjBpRlYwY0YtOTQzQVVBZkZWWHBmbE96ZkNZbkdkNlBSLWY2UkRDMl93TXJWbGpMUU1pRjFBZHBRZ1lmUFpXZTVFZWRUWEFBZTlqVllDbk1qd3JEVlFOWnpzXzFWWnJfYk45ckJFemRhX3hEcU5RTzlyTGM4bF9MRTlWZVhWUTdSQUVfaWJYODFzTHVfTVU9NjI0ODdEQzI=" \
  -H "X-RovoDev-Billing-CloudId: a9b5139f-b0b5-4945-b303-a1aa98ce6f30" \
  -H "X-Atlassian-CloudId: a9b5139f-b0b5-4945-b303-a1aa98ce6f30" \
  -H "X-RovoDev-Xid: rovodev-cli" \
  -H "X-RovoDev-Session-Id: 35708a05-3013-4db9-81ed-303819fbc008" \
  -H "X-RovoDev-Session-Agent-Run-Id: 35708a05-3013-4db9-81ed-303819fbc008_588b27a9-ac78-4bd8-86c0-876030e5eefe" \
  -H "X-RovoDev-Version: 0.13.35" \
  -d '{
  "max_tokens": 8192,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "text": "hş",
          "type": "text"
        },
        {
          "text": "\n\nYou have used 0 iterations.",
          "type": "text",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    }
  ],
  "system": [
    {
      "type": "text",
      "text": "You are \"Rovo Dev\" - a friendly and helpful AI agent that can help software developers with their tasks."
    }
  ]
}')

echo "$response"
