#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

pkg update
pkg install -y python git clang cmake make libandroid-spawn termux-api
python -m pip install --upgrade pip
python -m pip install -r requirements.txt

mkdir -p models data

echo "Nermana is ready."
echo "Put .gguf models in: $(pwd)/models"
echo "Start the web UI with: sh scripts/termux_start.sh"
