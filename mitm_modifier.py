"""
mitmproxy addon that modifies Bedrock requests.

This is designed to work as a standalone mitmproxy addon.
The key difference from intercept.py: we ONLY modify Bedrock requests,
and we ensure proper content-length handling.

Usage:
    mitmproxy -s mitm_modifier.py -p 8080

Then run rovodev with:
    SSL_CERT_FILE=~/.mitmproxy/mitmproxy-ca-cert.pem HTTPS_PROXY=http://127.0.0.1:8080 acli rovodev
"""

import json
import gzip
from mitmproxy import http
import logging

# Setup logging to see what's happening
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("mitm_modifier")

# Test system prompt - short to minimize size difference
CUSTOM_SYSTEM_PROMPT = """TEST: Interception worked! Say "SUCCESS" first, then help normally."""


class BedrockModifier:
    """Addon to modify Bedrock API requests."""
    
    def request(self, flow: http.HTTPFlow) -> None:
        """Modify outgoing requests."""
        
        # Only intercept Bedrock API calls
        if "/rovodev/v2/proxy/ai/v1/bedrock/model/" not in flow.request.pretty_url:
            return
        
        if not flow.request.content:
            return
        
        logger.info(f"[BEDROCK] Intercepting: {flow.request.pretty_url}")
        
        try:
            # Parse request body
            content = flow.request.content
            
            # Check if gzip compressed
            if content[:2] == b'\x1f\x8b':
                content = gzip.decompress(content)
            
            data = json.loads(content.decode("utf-8"))
            
            # Modify system prompt if present
            if "system" not in data:
                logger.info("[BEDROCK] No system prompt found")
                return
            
            original = data["system"]
            
            if isinstance(original, list):
                # Keep cache_control if present
                new_system = [{"type": "text", "text": CUSTOM_SYSTEM_PROMPT}]
                if len(original) > 0 and "cache_control" in original[0]:
                    new_system[0]["cache_control"] = original[0]["cache_control"]
                data["system"] = new_system
            else:
                data["system"] = CUSTOM_SYSTEM_PROMPT
            
            # Re-encode
            new_content = json.dumps(data, separators=(',', ':')).encode("utf-8")
            
            # Use set_content which properly handles content-length
            flow.request.set_content(new_content)
            
            # Explicitly remove content-encoding header if present
            flow.request.headers.pop("content-encoding", None)
            
            # Log the change
            old_len = len(content)
            new_len = len(new_content)
            logger.info(f"[BEDROCK] Modified! Size: {old_len} -> {new_len}")
            logger.info(f"[BEDROCK] Content-Length header: {flow.request.headers.get('content-length', 'NOT SET')}")
            
        except Exception as e:
            logger.error(f"[BEDROCK] Error: {e}")
            import traceback
            traceback.print_exc()
    
    def response(self, flow: http.HTTPFlow) -> None:
        """Log responses for debugging."""
        
        if "/rovodev/v2/proxy/ai/v1/bedrock/model/" in flow.request.pretty_url:
            status = flow.response.status_code if flow.response else "N/A"
            logger.info(f"[BEDROCK] Response: {status}")
            
            if flow.response and flow.response.status_code == 403:
                logger.error("[BEDROCK] Got 403 - request was rejected")
                if flow.response.content:
                    logger.error(f"[BEDROCK] Response body: {flow.response.content[:500]}")


# Create addon instance
addons = [BedrockModifier()]
