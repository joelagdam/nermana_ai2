from __future__ import annotations

import shutil
import subprocess
import time
from pathlib import Path
from typing import Any

from .config import DATA_DIR, DEFAULT_CONFIG_PATH, MODELS_DIR, PROJECT_ROOT


def update_status(fetch: bool = False) -> dict[str, Any]:
    if not (PROJECT_ROOT / ".git").exists():
        return {"ok": False, "error": "This folder is not a git checkout."}
    current = _git(["rev-parse", "--short", "HEAD"])
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if not current["ok"]:
        return current
    if not upstream["ok"]:
        return {
            "ok": True,
            "message": "No upstream branch is configured.",
            "current": current.get("stdout", ""),
            "branch": branch.get("stdout", ""),
            "update_available": False,
            "fetch": None,
        }

    fetch_result = None
    if fetch:
        fetch_result = _git(["fetch", "--all", "--prune"])
        if not fetch_result["ok"]:
            return _status_result(False, "Could not check remote updates.", current, branch, upstream, fetch_result)

    remote = _git(["rev-parse", "--short", "@{u}"])
    current_full = _git(["rev-parse", "HEAD"])
    remote_full = _git(["rev-parse", "@{u}"])
    base = _git(["merge-base", "HEAD", "@{u}"])
    if not (remote["ok"] and current_full["ok"] and remote_full["ok"] and base["ok"]):
        return _status_result(False, "Could not compare local and upstream commits.", current, branch, upstream, fetch_result)

    local_sha = current_full.get("stdout", "")
    remote_sha = remote_full.get("stdout", "")
    base_sha = base.get("stdout", "")
    behind = local_sha != remote_sha and base_sha == local_sha
    ahead = local_sha != remote_sha and base_sha == remote_sha
    diverged = local_sha != remote_sha and not behind and not ahead
    if behind:
        message = "Update available from upstream."
    elif ahead:
        message = "Local checkout is ahead of upstream."
    elif diverged:
        message = "Local checkout and upstream have diverged."
    else:
        message = "Already up to date."
    return {
        "ok": True,
        "message": message,
        "current": current.get("stdout", ""),
        "remote": remote.get("stdout", ""),
        "branch": branch.get("stdout", ""),
        "upstream": upstream.get("stdout", ""),
        "update_available": behind,
        "ahead": ahead,
        "diverged": diverged,
        "fetch": fetch_result,
    }


def update_system() -> dict[str, Any]:
    if not (PROJECT_ROOT / ".git").exists():
        return {"ok": False, "error": "This folder is not a git checkout."}
    backup = _backup_config()
    before = _git(["rev-parse", "--short", "HEAD"])
    if not before["ok"]:
        return before
    fetch = _git(["fetch", "--all", "--prune"])
    if not fetch["ok"]:
        return _result(False, "git fetch failed", before, fetch, backup)
    pull = _git(["pull", "--ff-only"])
    _restore_config_if_missing(backup)
    _ensure_persistent_dirs()
    after = _git(["rev-parse", "--short", "HEAD"])
    ok = pull["ok"] and after["ok"]
    status = update_status(fetch=False)
    message = "Updated. Restart Nermana to load new code." if before.get("stdout") != after.get("stdout") else "Already up to date."
    return {
        "ok": ok,
        "message": message if ok else "Update failed.",
        "before": before.get("stdout", ""),
        "after": after.get("stdout", ""),
        "backup": str(backup) if backup else "",
        "models_dir": str(MODELS_DIR),
        "config_path": str(DEFAULT_CONFIG_PATH),
        "fetch": fetch,
        "pull": pull,
        "status": status,
    }


def _git(args: list[str]) -> dict[str, Any]:
    try:
        completed = subprocess.run(["git", *args], cwd=PROJECT_ROOT, capture_output=True, text=True, timeout=120)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "args": args}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "args": args,
    }


def _backup_config() -> Path | None:
    if not DEFAULT_CONFIG_PATH.exists():
        return None
    backup_dir = DATA_DIR / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    backup = backup_dir / f"config.{int(time.time())}.json"
    shutil.copy2(DEFAULT_CONFIG_PATH, backup)
    return backup


def _restore_config_if_missing(backup: Path | None) -> None:
    if backup and backup.exists() and not DEFAULT_CONFIG_PATH.exists():
        DEFAULT_CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(backup, DEFAULT_CONFIG_PATH)


def _ensure_persistent_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    MODELS_DIR.mkdir(parents=True, exist_ok=True)


def _result(ok: bool, message: str, before: dict, step: dict, backup: Path | None) -> dict[str, Any]:
    return {
        "ok": ok,
        "message": message,
        "before": before.get("stdout", ""),
        "backup": str(backup) if backup else "",
        "step": step,
        "config_path": str(DEFAULT_CONFIG_PATH),
        "models_dir": str(MODELS_DIR),
    }


def _status_result(ok: bool, message: str, current: dict, branch: dict, upstream: dict, fetch: dict | None) -> dict[str, Any]:
    return {
        "ok": ok,
        "message": message,
        "current": current.get("stdout", ""),
        "branch": branch.get("stdout", ""),
        "upstream": upstream.get("stdout", ""),
        "update_available": False,
        "fetch": fetch,
    }
