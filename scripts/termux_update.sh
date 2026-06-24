#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

mkdir -p data models
python -m nermana update
