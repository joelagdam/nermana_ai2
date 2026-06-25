#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

PYTHON="${PYTHON:-python}"
if ! command -v "$PYTHON" >/dev/null 2>&1; then
  echo "python: not found. Install Python through Termux pkg first."
  exit 1
fi

mkdir -p models data

config_value() {
  key="$1"
  "$PYTHON" - "$key" <<'PY'
import sys
from urllib.parse import urlparse
from nermana.config import load_config

cfg = load_config()
key = sys.argv[1]
if key == "server.host":
    print(cfg.server.host)
elif key == "server.port":
    print(cfg.server.port)
elif key == "model.port":
    parsed = urlparse(cfg.model.base_url)
    print(parsed.port or 8080)
elif key == "model.active":
    print(cfg.model.active_model or "")
PY
}

WEB_HOST="${NERMANA_HOST:-$(config_value server.host)}"
WEB_PORT="${NERMANA_PORT:-$(config_value server.port)}"
LLM_PORT="${NERMANA_LLM_PORT:-$(config_value model.port)}"
ACTIVE_MODEL="$(config_value model.active)"
CLEAN_START="${NERMANA_CLEAN_START:-1}"

kill_port() {
  port="$1"
  label="$2"
  if [ -z "$port" ]; then
    return
  fi
  pids=""
  if command -v lsof >/dev/null 2>&1; then
    pids="$(lsof -ti TCP:"$port" 2>/dev/null || true)"
  fi
  if [ -n "$pids" ]; then
    echo "$label: stopping process(es) on port $port: $pids"
    kill $pids 2>/dev/null || true
    sleep 1
    kill -9 $pids 2>/dev/null || true
    return
  fi
  if command -v fuser >/dev/null 2>&1; then
    if fuser "$port/tcp" >/dev/null 2>&1; then
      echo "$label: freeing port $port with fuser"
      fuser -k "$port/tcp" >/dev/null 2>&1 || true
      sleep 1
    fi
  fi
}

stop_old_nermana() {
  echo "nermana: stopping old web/startup processes"
  pkill -f "python.*nermana.startup" 2>/dev/null || true
  pkill -f "python.*nermana.cli.*serve" 2>/dev/null || true
  pkill -f "python.*-m nermana.startup" 2>/dev/null || true
  sleep 1
}

stop_old_llm() {
  if [ "${NERMANA_KEEP_LLM:-0}" = "1" ]; then
    echo "model: keeping existing llama-server because NERMANA_KEEP_LLM=1"
    return
  fi
  echo "model: stopping old llama.cpp server on port $LLM_PORT"
  pkill -f "llama-server.*--port $LLM_PORT" 2>/dev/null || true
  pkill -f "llama.cpp.*server.*--port $LLM_PORT" 2>/dev/null || true
  kill_port "$LLM_PORT" "model"
}

echo "nermana: starting available services"
echo "web: target http://$WEB_HOST:$WEB_PORT"
echo "model: target llama.cpp port $LLM_PORT${ACTIVE_MODEL:+ using $ACTIVE_MODEL}"
if [ "$CLEAN_START" = "1" ]; then
  stop_old_nermana
  kill_port "$WEB_PORT" "web"
  stop_old_llm
else
  echo "nermana: clean start disabled with NERMANA_CLEAN_START=0"
fi
echo "web: override port with: NERMANA_PORT=8766 sh scripts/termux_start_all.sh"
echo "model: keep existing llama-server with: NERMANA_KEEP_LLM=1 sh scripts/termux_start_all.sh"

exec "$PYTHON" -m nermana.startup
