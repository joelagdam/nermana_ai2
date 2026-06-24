#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

MODEL="${1:-}"
LLAMA_SERVER="${NERMANA_LLAMA_SERVER:-}"

find_llama_server() {
  if [ -n "$LLAMA_SERVER" ] && [ -x "$LLAMA_SERVER" ]; then
    echo "$LLAMA_SERVER"
    return 0
  fi
  if command -v llama-server >/dev/null 2>&1; then
    command -v llama-server
    return 0
  fi
  for candidate in "$HOME/llama.cpp/build/bin/llama-server" "$HOME/llama.cpp/llama-server" "$HOME/llama.cpp/server"; do
    if [ -x "$candidate" ]; then
      echo "$candidate"
      return 0
    fi
  done
  return 1
}

LLAMA_SERVER="$(find_llama_server || true)"
if [ -z "$LLAMA_SERVER" ]; then
  echo "llama-server was not found."
  echo "Expected one of: PATH llama-server, ~/llama.cpp/build/bin/llama-server, ~/llama.cpp/llama-server"
  echo "Or set NERMANA_LLAMA_SERVER=/full/path/to/llama-server"
  exit 1
fi

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

exec "$LLAMA_SERVER" -m "$MODEL" -c "$CTX" -t "$THREADS" --host 127.0.0.1 --port "$PORT"
