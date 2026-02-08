
from mitmproxy import http
import sys

def responseheaders(flow: http.HTTPFlow):
    # Enable streaming and assign a modifier
    flow.response.stream = modify_stream

def modify_stream(chunks):
    print("Stream started")
    for chunk in chunks:
        print(f"Got chunk: {len(chunk)} bytes")
        yield chunk
    print("Stream ended")
