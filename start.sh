#!/usr/bin/env bash
#
# Start all RovoDev services.
#
# Usage:
#   ./start.sh          — start all services
#   ./start.sh stop     — stop all services
#   ./start.sh status   — check service status
#   ./start.sh restart  — restart all services
#

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PID_DIR="$SCRIPT_DIR/.pids"
LOG_DIR="$SCRIPT_DIR/logs"
CA_CERT="$HOME/.mitmproxy/mitmproxy-ca-cert.pem"

mkdir -p "$PID_DIR" "$LOG_DIR"

RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

_check_port() {
    lsof -i ":$1" -P -n 2>/dev/null | grep -q LISTEN
}

_wait_for_port() {
    local port=$1 name=$2 timeout=${3:-10}
    for i in $(seq 1 "$timeout"); do
        if _check_port "$port"; then
            echo -e "  ${GREEN}✓${NC} $name ready on port $port"
            return 0
        fi
        sleep 1
    done
    echo -e "  ${RED}✗${NC} $name failed to start on port $port"
    return 1
}

_stop_service() {
    local name=$1 pidfile="$PID_DIR/$1.pid"
    if [[ -f "$pidfile" ]]; then
        local pid
        pid=$(cat "$pidfile")
        if kill -0 "$pid" 2>/dev/null; then
            kill "$pid" 2>/dev/null
            sleep 1
            kill -0 "$pid" 2>/dev/null && kill -9 "$pid" 2>/dev/null
            echo -e "  ${YELLOW}■${NC} Stopped $name (PID $pid)"
        fi
        rm -f "$pidfile"
    fi
}

do_stop() {
    echo "Stopping services..."
    _stop_service amp_intercept
    _stop_service rovodev_server
    _stop_service intercept
    _stop_service flask_app

    for port in 8899 8000 8080 5001; do
        for pid in $(lsof -i ":$port" -t 2>/dev/null); do
            kill "$pid" 2>/dev/null || true
        done
    done
    sleep 1
    echo -e "${GREEN}All services stopped.${NC}"
}

do_status() {
    echo "Service status:"
    for pair in "intercept:8080" "rovodev_server:8000" "flask_app:5001" "amp_intercept:8899"; do
        name="${pair%%:*}"
        port="${pair##*:}"
        if _check_port "$port"; then
            echo -e "  ${GREEN}●${NC} $name — port $port"
        else
            echo -e "  ${RED}○${NC} $name — port $port (not running)"
        fi
    done
}

do_start() {
    echo "Starting RovoDev services..."
    echo ""

    # 1. intercept.py (mitmproxy :8080)
    if _check_port 8080; then
        echo -e "  ${GREEN}✓${NC} intercept.py already running on port 8080"
    else
        mitmdump \
            -s "$SCRIPT_DIR/intercept.py" \
            -p 8080 \
            --set connection_strategy=lazy \
            > "$LOG_DIR/intercept.log" 2>&1 &
        echo $! > "$PID_DIR/intercept.pid"
        _wait_for_port 8080 "intercept.py"
    fi

    # 2. rovodev_server.py (FastAPI :8000)
    if _check_port 8000; then
        echo -e "  ${GREEN}✓${NC} rovodev_server.py already running on port 8000"
    else
        python "$SCRIPT_DIR/rovodev_server.py" \
            > "$LOG_DIR/rovodev_server.log" 2>&1 &
        echo $! > "$PID_DIR/rovodev_server.pid"
        _wait_for_port 8000 "rovodev_server.py"
    fi

    # 3. Flask dashboard (:5001)
    if _check_port 5001; then
        echo -e "  ${GREEN}✓${NC} Flask dashboard already running on port 5001"
    else
        cd "$SCRIPT_DIR/flask_app"
        python app.py > "$LOG_DIR/flask_app.log" 2>&1 &
        echo $! > "$PID_DIR/flask_app.pid"
        cd "$SCRIPT_DIR"
        _wait_for_port 5001 "Flask dashboard"
    fi

    # 4. amp_intercept.py (mitmproxy :8899)
    if _check_port 8899; then
        echo -e "  ${GREEN}✓${NC} amp_intercept.py already running on port 8899"
    else
        mitmdump \
            -s "$SCRIPT_DIR/amp_intercept.py" \
            -p 8899 \
            --set connection_strategy=lazy \
            > "$LOG_DIR/amp_intercept.log" 2>&1 &
        echo $! > "$PID_DIR/amp_intercept.pid"
        _wait_for_port 8899 "amp_intercept.py"
    fi

    echo ""
    echo -e "${GREEN}All services running.${NC}"
    echo ""
    echo "  Dashboard:  http://localhost:5001"
    echo "  API:        http://localhost:8000/health"
    echo ""
    echo "  Run Amp with proxy:"
    echo "    HTTPS_PROXY=http://127.0.0.1:8899 SSL_CERT_FILE=$CA_CERT NODE_EXTRA_CA_CERTS=$CA_CERT amp"
    echo ""
    echo "  Logs:       $LOG_DIR/"
}

case "${1:-start}" in
    start)   do_start ;;
    stop)    do_stop ;;
    restart) do_stop; sleep 1; do_start ;;
    status)  do_status ;;
    *)       echo "Usage: $0 {start|stop|restart|status}"; exit 1 ;;
esac
