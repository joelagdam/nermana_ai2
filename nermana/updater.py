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
    if not current["ok"]:
        return current

    fetch_result = None
    if fetch:
        fetch_result = _git(["fetch", "--all", "--prune"])
        if not fetch_result["ok"]:
            return _status_result(False, "Could not check remote updates. Check internet or git credentials.", current, branch, {}, fetch_result)
    dirty = _dirty_status()

    target = _update_target(branch)
    if not target["ok"]:
        return _status_result(False, target.get("error", "No update source configured."), current, branch, target, fetch_result)

    target_ref = target["target"]
    remote = _git(["rev-parse", "--short", target_ref])
    current_full = _git(["rev-parse", "HEAD"])
    remote_full = _git(["rev-parse", target_ref])
    base = _git(["merge-base", "HEAD", target_ref])
    if not (remote["ok"] and current_full["ok"] and remote_full["ok"] and base["ok"]):
        return _status_result(False, "Could not compare local and update source commits.", current, branch, target, fetch_result)

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
        "upstream": target_ref,
        "target": target,
        "update_available": behind,
        "ahead": ahead,
        "diverged": diverged,
        "dirty": dirty.get("dirty", False),
        "dirty_files": dirty.get("files", []),
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
    branch = _git(["rev-parse", "--abbrev-ref", "HEAD"])
    target = _update_target(branch)
    if not target["ok"]:
        return _result(False, target.get("error", "No update source configured."), before, target, backup)
    dirty = _dirty_status()
    stash = None
    if dirty.get("dirty"):
        stash = _stash_worktree()
        if not stash["ok"]:
            return _result(False, "Could not protect local source changes before update.", before, stash, backup)
    pull = _git(["merge", "--ff-only", target["target"]])
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
        "dirty": dirty.get("dirty", False),
        "dirty_files": dirty.get("files", []),
        "stash": stash,
        "target": target,
        "status": status,
    }


def _update_target(branch: dict[str, Any]) -> dict[str, Any]:
    upstream = _git(["rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}"])
    if upstream["ok"] and upstream.get("stdout"):
        return {"ok": True, "target": upstream["stdout"], "source": "upstream"}

    branch_name = branch.get("stdout", "") if branch.get("ok") else ""
    candidates = []
    if branch_name and branch_name != "HEAD":
        candidates.append(f"origin/{branch_name}")
    candidates.extend(["origin/main", "origin/master"])
    seen = set()
    for candidate in candidates:
        if candidate in seen:
            continue
        seen.add(candidate)
        check = _git(["rev-parse", "--verify", candidate])
        if check["ok"]:
            return {
                "ok": True,
                "target": candidate,
                "source": "origin fallback",
                "note": "No upstream branch is configured; using the origin fallback.",
                "upstream_error": upstream.get("stderr") or upstream.get("error", ""),
            }
    return {
        "ok": False,
        "error": "No update source configured. Set an upstream branch or add origin/main.",
        "source": "none",
        "upstream_error": upstream.get("stderr") or upstream.get("error", ""),
    }


def _dirty_status() -> dict[str, Any]:
    status = _git(["status", "--porcelain"])
    if not status["ok"]:
        return {"ok": False, "dirty": False, "files": [], "error": status.get("stderr") or status.get("error", "")}
    files = [line.strip() for line in status.get("stdout", "").splitlines() if line.strip()]
    return {"ok": True, "dirty": bool(files), "files": files[:40], "count": len(files)}


def _stash_worktree() -> dict[str, Any]:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    return _git(["stash", "push", "--include-untracked", "-m", f"nermana-auto-update-{stamp}"])


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
