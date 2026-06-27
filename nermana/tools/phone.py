from __future__ import annotations

import json
import re
import shlex
import shutil
import subprocess

from nermana.config import AppConfig
from nermana.tooling import Tool, ToolRegistry


PACKAGE_RE = re.compile(r"^[A-Za-z0-9_.]+$")
PERMISSION_RE = re.compile(r"^[A-Z_a-z0-9.]+$")
SETTING_RE = re.compile(r"^[A-Za-z0-9_.:-]+$")
SHELL_META_RE = re.compile(r"[;&|<>`$()]")


def register_phone_tools(registry: ToolRegistry, config: AppConfig) -> None:
    def termux_available() -> tuple[bool, str]:
        if not config.phone.enabled or not config.phone.termux_enabled:
            return False, "Termux tools disabled"
        if shutil.which("termux-battery-status") or shutil.which("termux-open-url"):
            return True, "Termux:API command found"
        return False, "Termux:API commands not found"

    def shizuku_available() -> tuple[bool, str]:
        if not config.phone.enabled or not config.phone.shizuku_enabled:
            return False, "Shizuku disabled"
        if shutil.which(config.phone.rish_path):
            return True, "rish found"
        return False, "rish not found"

    def phone_status(_: dict) -> dict:
        battery = _run(["termux-battery-status"], config.phone.command_timeout_seconds)
        if battery["ok"]:
            try:
                battery["json"] = json.loads(battery["stdout"])
            except json.JSONDecodeError:
                pass
        return {"ok": True, "battery": battery, "termux": termux_available()[1], "shizuku": shizuku_available()[1]}

    def phone_status_available() -> tuple[bool, str]:
        if not config.phone.enabled:
            return False, "phone control disabled"
        termux_ok, termux_details = termux_available()
        shizuku_ok, shizuku_details = shizuku_available()
        state = "; ".join([f"Termux: {termux_details}", f"Shizuku: {shizuku_details}"])
        if termux_ok or shizuku_ok:
            return True, state
        return True, f"diagnostic only; {state}"

    def open_url(payload: dict) -> dict:
        url = str(payload.get("url", "")).strip()
        if not url:
            return {"ok": False, "error": "url is required"}
        return _run(["termux-open-url", url], config.phone.command_timeout_seconds)

    def list_packages(_: dict) -> dict:
        return _privileged(config, "pm list packages")

    def force_stop(payload: dict) -> dict:
        package = _package(payload)
        return _privileged(config, f"am force-stop {package}")

    def set_app_enabled(payload: dict) -> dict:
        package = _package(payload)
        enabled = bool(payload.get("enabled", True))
        state = "enable" if enabled else "disable-user"
        return _privileged(config, f"pm {state} {package}")

    def set_permission(payload: dict) -> dict:
        package = _package(payload)
        permission = str(payload.get("permission", ""))
        if not PERMISSION_RE.match(permission):
            return {"ok": False, "error": "invalid permission"}
        action = "grant" if bool(payload.get("granted", True)) else "revoke"
        return _privileged(config, f"pm {action} {package} {permission}")

    def appops_set(payload: dict) -> dict:
        package = _package(payload)
        op = str(payload.get("op", ""))
        mode = str(payload.get("mode", ""))
        if not SETTING_RE.match(op) or mode not in {"allow", "ignore", "deny", "default", "foreground"}:
            return {"ok": False, "error": "invalid appop or mode"}
        return _privileged(config, f"cmd appops set {package} {op} {mode}")

    def settings_get(payload: dict) -> dict:
        namespace = _namespace(config, payload)
        key = str(payload.get("key", ""))
        if not SETTING_RE.match(key):
            return {"ok": False, "error": "invalid key"}
        return _privileged(config, f"settings get {namespace} {key}")

    def settings_put(payload: dict) -> dict:
        namespace = _namespace(config, payload)
        key = str(payload.get("key", ""))
        value = str(payload.get("value", ""))
        if not SETTING_RE.match(key) or "\n" in value:
            return {"ok": False, "error": "invalid key or value"}
        return _privileged(config, f"settings put {namespace} {key} {value}")

    def termux_command_available() -> tuple[bool, str]:
        if not config.phone.enabled or not config.phone.termux_enabled:
            return False, "Termux command tool disabled"
        allowed = [name for name in config.phone.allowed_termux_commands if shutil.which(name)]
        if allowed:
            return True, f"allowed commands available: {', '.join(allowed[:8])}"
        return False, "no configured Termux commands found in PATH"

    def termux_command(payload: dict) -> dict:
        command = str(payload.get("command", "")).strip()
        if not command:
            return {"ok": False, "error": "command is required"}
        if len(command) > 500:
            return {"ok": False, "error": "command is too long"}
        if "\n" in command or SHELL_META_RE.search(command):
            return {"ok": False, "error": "shell metacharacters are not allowed; pass one direct command only"}
        try:
            parts = shlex.split(command)
        except ValueError as exc:
            return {"ok": False, "error": f"invalid command: {exc}"}
        if not parts:
            return {"ok": False, "error": "command is required"}
        executable = parts[0].rsplit("/", 1)[-1]
        if executable not in set(config.phone.allowed_termux_commands):
            return {"ok": False, "error": f"{executable} is not in allowed_termux_commands"}
        return _run(parts, config.phone.command_timeout_seconds)

    registry.register(
        Tool(
            name="phone_status",
            description="Read basic Termux and Shizuku availability plus battery if Termux:API exists.",
            provider="termux",
            input_schema={"type": "object"},
            offline_required=True,
            risk="read",
            handler=phone_status,
            availability=phone_status_available,
        )
    )
    registry.register(
        Tool(
            name="open_url",
            description="Open a URL on the phone through Termux:API.",
            provider="termux",
            input_schema={"type": "object", "properties": {"url": {"type": "string"}}},
            offline_required=True,
            risk="safe",
            handler=open_url,
            availability=termux_available,
        )
    )
    registry.register(
        Tool(
            name="termux_command",
            description="Run one allowlisted Termux command without shell expansion.",
            provider="termux",
            input_schema={"type": "object", "properties": {"command": {"type": "string"}}},
            offline_required=True,
            risk="power",
            timeout_seconds=config.phone.command_timeout_seconds,
            handler=termux_command,
            availability=termux_command_available,
        )
    )
    for name, description, handler in [
        ("list_packages", "List Android packages with Shizuku/rish.", list_packages),
        ("force_stop_app", "Force-stop an Android package with Shizuku/rish.", force_stop),
        ("set_app_enabled", "Enable or disable an Android package with Shizuku/rish.", set_app_enabled),
        ("set_permission", "Grant or revoke an Android runtime permission.", set_permission),
        ("appops_set", "Set an appops mode for a package.", appops_set),
        ("settings_get", "Read an Android setting.", settings_get),
        ("settings_put", "Write an Android setting.", settings_put),
    ]:
        registry.register(
            Tool(
                name=name,
                description=description,
                provider="shizuku",
                input_schema={"type": "object"},
                offline_required=True,
                risk="power" if name not in {"list_packages", "settings_get"} else "read",
                timeout_seconds=config.phone.command_timeout_seconds,
                handler=handler,
                availability=shizuku_available,
            )
        )


def _run(command: list[str], timeout: float) -> dict:
    if shutil.which(command[0]) is None:
        return {"ok": False, "error": f"{command[0]} not found"}
    try:
        completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    except Exception as exc:
        return {"ok": False, "error": str(exc), "command": command}
    return {
        "ok": completed.returncode == 0,
        "returncode": completed.returncode,
        "stdout": completed.stdout.strip(),
        "stderr": completed.stderr.strip(),
        "command": command,
    }


def _privileged(config: AppConfig, command: str) -> dict:
    if shutil.which(config.phone.rish_path):
        return _run([config.phone.rish_path, "-c", command], config.phone.command_timeout_seconds)
    first = command.split(" ", 1)[0]
    if shutil.which(first):
        return _run(command.split(" "), config.phone.command_timeout_seconds)
    return {"ok": False, "error": "Shizuku rish or Android shell command not available", "command": command}


def _package(payload: dict) -> str:
    package = str(payload.get("package", "")).strip()
    if not PACKAGE_RE.match(package):
        raise ValueError("invalid package")
    return package


def _namespace(config: AppConfig, payload: dict) -> str:
    namespace = str(payload.get("namespace", "system"))
    if namespace not in config.phone.allowed_settings_namespaces:
        raise ValueError("settings namespace is not allowed")
    return namespace
