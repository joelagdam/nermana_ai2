#!/data/data/com.termux/files/usr/bin/sh
set -eu

cd "$(dirname "$0")/.."

has_pkg() {
  dpkg -s "$1" >/dev/null 2>&1
}

has_cmd() {
  command -v "$1" >/dev/null 2>&1
}

has_llama_server() {
  has_cmd llama-server ||
    [ -x "$HOME/llama.cpp/build/bin/llama-server" ] ||
    [ -x "$HOME/llama.cpp/llama-server" ] ||
    [ -x "$HOME/llama.cpp/server" ]
}

ensure_pkg() {
  pkg_name="$1"
  check_cmd="${2:-}"
  if [ -n "$check_cmd" ] && has_cmd "$check_cmd"; then
    echo "skip: $pkg_name ($check_cmd already exists)"
    return 0
  fi
  if [ -z "$check_cmd" ] && has_pkg "$pkg_name"; then
    echo "skip: $pkg_name already installed"
    return 0
  fi
  if [ -n "$check_cmd" ] && has_pkg "$pkg_name"; then
    echo "repair: $pkg_name is installed, but $check_cmd is missing"
  else
    echo "install: $pkg_name"
  fi
  pkg install -y "$pkg_name"
}

needs_pkg() {
  pkg_name="$1"
  check_cmd="${2:-}"
  if [ -n "$check_cmd" ]; then
    has_cmd "$check_cmd" && return 1
    return 0
  fi
  if has_pkg "$pkg_name"; then
    return 1
  fi
  return 0
}

ensure_llama() {
  if has_llama_server; then
    echo "skip: llama.cpp (llama-server already exists)"
    return 0
  fi
  if [ "${NERMANA_INSTALL_LLAMA:-0}" = "1" ]; then
    ensure_pkg llama.cpp llama-server
  elif has_pkg llama.cpp; then
    echo "warning: llama.cpp package is installed, but llama-server was not found in PATH; not complete, skipping repair unless NERMANA_INSTALL_LLAMA=1"
  else
    echo "skip: llama.cpp not installed. Install it separately, or run with NERMANA_INSTALL_LLAMA=1 if your Termux repo provides it."
  fi
}

echo "Checking Termux packages..."
needs_install=0
for item in "python:python" "git:git" "clang:clang" "cmake:cmake" "make:make" "libandroid-spawn:" "termux-api:termux-battery-status"; do
  pkg_name="${item%%:*}"
  check_cmd="${item#*:}"
  if needs_pkg "$pkg_name" "$check_cmd"; then
    needs_install=1
  fi
done
if [ "${NERMANA_INSTALL_LLAMA:-0}" = "1" ] && ! has_llama_server && ! has_pkg llama.cpp; then
  needs_install=1
fi
if [ "$needs_install" = "1" ]; then
  pkg update
else
  echo "skip: pkg update (all checked packages already complete)"
fi
ensure_pkg python python
ensure_pkg git git
ensure_pkg clang clang
ensure_pkg cmake cmake
ensure_pkg make make
ensure_pkg libandroid-spawn
ensure_pkg termux-api termux-battery-status
ensure_llama

mkdir -p models data

echo "Nermana is ready."
echo "No pip install was run."
echo "Put .gguf models in: $(pwd)/models"
echo "Start everything available with: sh scripts/termux_start_all.sh"
