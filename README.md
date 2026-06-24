# Nermana

Nermana is an offline-first phone AI scaffold for Android through Termux. It runs a local Python agent on the phone, talks to a local `llama.cpp` OpenAI-compatible server, exposes a web control panel, and enables optional tools when their providers are available.

This project is designed for Termux first. Desktop systems are only useful as a development mirror.

The Termux setup does not run `pip install`. It uses standard-library Python for the web UI by default and skips packages or tools that are already installed.

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

The setup script checks each package before installing it. If `python`, `git`, `clang`, `cmake`, `make`, `termux-api`, or `llama-server` already exists, it skips that part. It never installs or upgrades pip.

Give Termux shared-storage access if you want Nermana to read approved files from phone storage:

```sh
termux-setup-storage
```

Build or install `llama.cpp`, place `.gguf` models in `models/`, then start the model server:

```sh
sh scripts/termux_llama_server.sh
```

If your `llama.cpp` build is in the Termux home folder, Nermana checks these automatically:

```sh
~/llama.cpp/build/bin/llama-server
~/llama.cpp/llama-server
~/llama.cpp/server
```

You can also set it in the Models page, or start manually with:

```sh
NERMANA_LLAMA_SERVER=$HOME/llama.cpp/build/bin/llama-server sh scripts/termux_llama_server.sh
```

If your Termux repository provides `llama.cpp` and you want the setup script to try installing it only when `llama-server` is missing, run:

```sh
NERMANA_INSTALL_LLAMA=1 sh scripts/termux_setup.sh
```

In another Termux session, start the web UI:

```sh
sh scripts/termux_start.sh
```

Open `http://127.0.0.1:8765` on the phone.

For normal use, the centralized launcher starts everything available: selected local model, Telegram polling if enabled, and the web UI.

```sh
sh scripts/termux_start_all.sh
```

The launcher keeps the web UI in the foreground and starts available services first. It auto-selects the first `.gguf` if no model is selected.

## Update

Nermana updates through git and keeps your local runtime data:

```sh
sh scripts/termux_update.sh
```

The updater preserves `data/config.json`, `data/`, and `models/`. You can also run `python -m nermana update` or press Update Nermana on the Settings page. Restart Nermana after an update so the running process loads the new code.

## Web Setup

The Models page is the setup hub:

- Detect and save your existing `llama-server` path from `~/llama.cpp`.
- Download model presets into `models/`.
- Download any direct `.gguf` link into `models/`.
- Select the downloaded model.
- Edit context size, threads, temperature, top-p, thinking mode, and model server URL.
- Restart the local `llama-server` after a model is selected.
- Tune performance: auto threads, batch sizes, request timeout, RAM lock, and mmap mode.
- Control semi-automatic tool decisions and confirmation behavior from the Tools page.

Preset downloads require internet on the phone. If the phone is offline, put `.gguf` files in `models/` manually and press Scan Models.

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

The default path expects a `llama.cpp` server compatible with `/v1/chat/completions`. The Models page scans the Termux project `models/` folder, selects a `.gguf`, downloads presets or direct links, edits runtime settings, and can attempt to restart `llama-server`.

If you use Qwen3 GGUF, `/no_think` is used for fast general replies and `/think` is used automatically for harder prompts.

By default, Nermana tries to run llama.cpp fast for the phone hardware: automatic CPU threads, model RAM lock with `--mlock`, tuned batch values, and a fallback start without memory flags if the phone rejects them.

## Python Packages

The app runs without pip packages on Termux. `requirements.txt` is only a placeholder that says no pip packages are required.

## Safety

The “conscience” is a practical decision layer. It blocks dangerous tools, limits file access to approved folders, exposes tool risk levels, and keeps power-user phone actions behind explicit configured allowlists. It is not real consciousness.
