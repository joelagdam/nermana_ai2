#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

mkdir -p models data
exec python -m nermana.startup
