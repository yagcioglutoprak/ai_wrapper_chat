#!/bin/bash
# Run acli rovodev with mitmproxy interception

# Terminal 1: Start mitmproxy (run this first in a separate terminal)
# mitmproxy -s ~/.rovodev/intercept_credits.py -p 8080

# Terminal 2: Run acli with proxy and custom CA
export HTTPS_PROXY=http://127.0.0.1:8080
export HTTP_PROXY=http://127.0.0.1:8080
export REQUESTS_CA_BUNDLE=~/.mitmproxy/mitmproxy-ca-cert.pem
export SSL_CERT_FILE=~/.mitmproxy/mitmproxy-ca-cert.pem

acli rovodev "$@"
