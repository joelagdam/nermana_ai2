#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

HOST="${NERMANA_HOST:-127.0.0.1}"
PORT="${NERMANA_PORT:-8765}"

python -m nermana serve --host "$HOST" --port "$PORT"
