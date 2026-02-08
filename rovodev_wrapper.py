#!/usr/bin/env python3
"""
RovoDev Wrapper - Intercepts and modifies API requests

This script wraps the rovodev CLI and intercepts its HTTPS requests
by running a local proxy that the CLI connects to.

The key insight: We run our OWN proxy that:
1. Receives requests from rovodev CLI
2. Modifies the system prompt
3. Forwards to the REAL api.atlassian.com
4. Returns the response

This works because rovodev authenticates using Basic Auth (email:token),
which is included in the request headers, not computed from the body.
"""

import asyncio
import json
import ssl
import os
import sys
import signal
import httpx
from aiohttp import web
import logging

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s'
)
logger = logging.getLogger(__name__)

# Real API
REAL_API = "https://api.atlassian.com"

# Custom system prompt
CUSTOM_SYSTEM_PROMPT = """TEST PROMPT - If you see this, the interception worked!

You are a helpful assistant. Just say "INTERCEPTION SUCCESSFUL" and then answer normally."""


class ProxyHandler:
    """Handles proxying requests to real API with modification."""
    
    def __init__(self):
        self.client = None
    
    async def init_client(self):
        """Initialize HTTP client."""
        self.client = httpx.AsyncClient(
            verify=True,  # Verify real API's SSL
            timeout=httpx.Timeout(600.0, connect=30.0),
            follow_redirects=True
        )
    
    async def close(self):
        if self.client:
            await self.client.aclose()
    
    async def handle_connect(self, request: web.Request) -> web.StreamResponse:
        """Handle CONNECT requests for HTTPS tunneling."""
        # For CONNECT, we need to tunnel the connection
        # But since we're a terminating proxy, we handle it differently
        return web.Response(status=200, text="Connection Established")
    
    async def handle_request(self, request: web.Request) -> web.StreamResponse:
        """Handle HTTP/HTTPS requests."""
        
        # Parse the request URL
        path = request.path
        method = request.method
        
        # Handle CONNECT method (HTTPS tunneling setup)
        if method == "CONNECT":
            return await self.handle_connect(request)
        
        # Get request body
        body = await request.read()
        
        # Check if this is a Bedrock request
        is_bedrock = "/rovodev/v2/proxy/ai/v1/bedrock/model/" in path
        is_streaming = "invoke-with-response-stream" in path
        
        # Modify system prompt for Bedrock requests
        modified = False
        if is_bedrock and body:
            try:
                data = json.loads(body.decode("utf-8"))
                if "system" in data:
                    if isinstance(data["system"], list):
                        data["system"] = [{"type": "text", "text": CUSTOM_SYSTEM_PROMPT}]
                    else:
                        data["system"] = CUSTOM_SYSTEM_PROMPT
                    body = json.dumps(data).encode("utf-8")
                    modified = True
                    logger.info(f"✓ Modified system prompt for {method} {path}")
            except Exception as e:
                logger.error(f"Failed to modify: {e}")
        
        # Build target URL
        target_url = f"{REAL_API}{path}"
        if request.query_string:
            target_url += f"?{request.query_string}"
        
        # Build headers, updating content-length if modified
        headers = {}
        skip_headers = {'host', 'connection', 'content-length', 'transfer-encoding', 
                       'keep-alive', 'proxy-connection', 'upgrade'}
        
        for key, value in request.headers.items():
            if key.lower() not in skip_headers:
                headers[key] = value
        
        headers['Host'] = 'api.atlassian.com'
        if body:
            headers['Content-Length'] = str(len(body))
        
        logger.info(f"→ {method} {path}" + (" [MODIFIED]" if modified else ""))
        
        try:
            if is_streaming:
                # Handle streaming response
                return await self._handle_streaming(request, method, target_url, headers, body)
            else:
                # Regular request
                resp = await self.client.request(
                    method=method,
                    url=target_url,
                    headers=headers,
                    content=body
                )
                
                # Build response headers
                resp_headers = {}
                skip_resp = {'transfer-encoding', 'connection', 'content-encoding'}
                for key, value in resp.headers.items():
                    if key.lower() not in skip_resp:
                        resp_headers[key] = value
                
                logger.info(f"← {resp.status_code}")
                
                return web.Response(
                    status=resp.status_code,
                    body=resp.content,
                    headers=resp_headers
                )
                
        except Exception as e:
            logger.error(f"Proxy error: {e}")
            return web.Response(status=502, text=f"Proxy error: {e}")
    
    async def _handle_streaming(self, request, method, url, headers, body):
        """Handle streaming response."""
        async with self.client.stream(method, url, headers=headers, content=body) as resp:
            # Prepare streaming response
            response = web.StreamResponse(status=resp.status_code)
            
            # Copy headers
            for key, value in resp.headers.items():
                if key.lower() not in {'transfer-encoding', 'connection', 'content-encoding'}:
                    response.headers[key] = value
            
            response.headers['Transfer-Encoding'] = 'chunked'
            await response.prepare(request)
            
            # Stream chunks
            async for chunk in resp.aiter_bytes():
                await response.write(chunk)
            
            await response.write_eof()
            return response


async def run_proxy(host: str = "127.0.0.1", port: int = 8888):
    """Run the proxy server."""
    
    handler = ProxyHandler()
    await handler.init_client()
    
    app = web.Application()
    app.router.add_route('*', '/{path:.*}', handler.handle_request)
    app.router.add_route('*', '/', handler.handle_request)
    
    runner = web.AppRunner(app)
    await runner.setup()
    
    site = web.TCPSite(runner, host, port)
    await site.start()
    
    logger.info("=" * 60)
    logger.info(f"RovoDev Proxy running on http://{host}:{port}")
    logger.info("=" * 60)
    logger.info("")
    logger.info("To use, run rovodev with:")
    logger.info(f"  HTTPS_PROXY=http://{host}:{port} acli rovodev")
    logger.info("")
    logger.info("This proxy will modify system prompts in Bedrock requests.")
    logger.info("=" * 60)
    
    # Handle shutdown
    loop = asyncio.get_event_loop()
    stop = asyncio.Event()
    
    def signal_handler():
        stop.set()
    
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, signal_handler)
    
    await stop.wait()
    
    await handler.close()
    await runner.cleanup()
    logger.info("Shutdown complete")


if __name__ == "__main__":
    asyncio.run(run_proxy())
