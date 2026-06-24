#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

MODEL="${1:-}"
if [ -z "$MODEL" ]; then
  MODEL="$(find models -maxdepth 1 -type f -name '*.gguf' | head -n 1 || true)"
fi

if [ -z "$MODEL" ]; then
  echo "No .gguf model found. Put one in ./models or pass a model path."
  exit 1
fi

CTX="${NERMANA_CTX:-4096}"
PORT="${NERMANA_MODEL_PORT:-8080}"
THREADS="${NERMANA_THREADS:-4}"

exec llama-server -m "$MODEL" -c "$CTX" -t "$THREADS" --host 127.0.0.1 --port "$PORT"
