#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

mkdir -p models data

echo "nermana: starting available services"
if [ -n "${NERMANA_HOST:-}" ] || [ -n "${NERMANA_PORT:-}" ]; then
  echo "web: using override ${NERMANA_HOST:-configured-host}:${NERMANA_PORT:-configured-port}"
fi
echo "web: override port with: NERMANA_PORT=8766 sh scripts/termux_start_all.sh"

exec python -m nermana.startup
