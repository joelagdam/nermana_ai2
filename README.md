# Nermana

Nermana is an offline-first phone AI scaffold for Android through Termux. It runs a local Python agent on the phone, talks to a local `llama.cpp` OpenAI-compatible server, exposes a web control panel, and enables optional tools when their providers are available.

This project is designed for Termux first. Desktop systems are only useful as a development mirror.

## What Works

- Local ADK-style agent core with memory, tools, settings, and safety checks.
- Web UI for chat, `.gguf` model switching, providers, files, phone tools, Telegram, logs, and raw settings.
- Local model adapter for `llama.cpp` server at `http://127.0.0.1:8080/v1`.
- Online search through a configured SearXNG JSON endpoint.
- Weather through Open-Meteo when online.
- File reading and memory indexing from approved folders only.
- Optional image and vision provider endpoints.
- Termux and Shizuku/rish phone tools with a power-user allowlist.
- Telegram bot polling when configured.

## Termux Setup

```sh
pkg update
pkg install git
git clone <your-repo-url> nermana
cd nermana
sh scripts/termux_setup.sh
```

Give Termux shared-storage access if you want Nermana to read approved files from phone storage:

```sh
termux-setup-storage
```

Build or install `llama.cpp`, place `.gguf` models in `models/`, then start the model server:

```sh
sh scripts/termux_llama_server.sh
```

In another Termux session, start the web UI:

```sh
sh scripts/termux_start.sh
```

Open `http://127.0.0.1:8765` on the phone.

## Phone Control

- Termux tools use Termux:API commands such as `termux-battery-status` and `termux-open-url`.
- Shizuku tools use `rish` when available.
- Power-user Shizuku actions are allowlisted: package list, force-stop, enable/disable, permission grant/revoke, appops, and selected Android settings.
- Broad arbitrary shell control is intentionally blocked in v1.

## Useful Commands

```sh
python -m nermana doctor
python -m nermana models
python -m nermana tools
python -m nermana chat "hello"
python -m nermana telegram --once
```

## Local Model

The default path expects a `llama.cpp` server compatible with `/v1/chat/completions`. The Models page scans the Termux project `models/` folder, selects a `.gguf`, edits runtime settings, and can attempt to restart `llama-server`.

If you use Qwen3 GGUF, `/no_think` is used for fast general replies and `/think` is used automatically for harder prompts.

## Safety

The “conscience” is a practical decision layer. It blocks dangerous tools, limits file access to approved folders, exposes tool risk levels, and keeps power-user phone actions behind explicit configured allowlists. It is not real consciousness.
