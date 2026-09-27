"""Shared, fail-closed Pi argv builder for Python launchers."""

import json
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
REQUIRED_SWITCHES = ("--no-extensions", "-na", "--no-skills", "--no-prompt-templates")


class PiIsolationError(ValueError):
    """Pi's configured isolation arguments are missing or altered."""


def read_runtime_config(config_path: Path | str | None = None) -> dict[str, Any]:
    path = Path(config_path) if config_path else ROOT / "config" / "agent_runtime.json"
    return json.loads(path.read_text(encoding="utf-8"))


def build_pi_argv(session_dir: Path | str, config: dict[str, Any] | None = None) -> list[str]:
    runtime = config if config is not None else read_runtime_config()
    args = ["--mode", "rpc", *runtime["pi_cli_args"], *runtime["pi_isolation_args"], "--session-dir", str(session_dir)]
    validate_pi_argv(args, runtime)
    return args


def validate_pi_argv(args: list[str], config: dict[str, Any]) -> bool:
    try:
        cli_args = config["pi_cli_args"]
        isolation_args = config["pi_isolation_args"]
        allowlist = config["pi_tool_allowlist"]
        extension_path = config["pi_extension_path"]
    except (KeyError, TypeError) as exc:
        raise PiIsolationError("Pi isolation config is malformed") from exc
    if not all(isinstance(value, list) for value in (cli_args, isolation_args, allowlist)) or not isinstance(extension_path, str):
        raise PiIsolationError("Pi isolation config is malformed")
    if "-a" in args or "--approve" in args:
        raise PiIsolationError("Pi approval flag is forbidden")

    def values_for(flag: str) -> list[str | None]:
        return [args[i + 1] if i + 1 < len(args) else None for i, arg in enumerate(args) if arg == flag]

    if values_for("--tools") != [",".join(allowlist)]:
        raise PiIsolationError("Pi --tools must exactly match pi_tool_allowlist")
    for flag in REQUIRED_SWITCHES:
        if args.count(flag) != 1:
            raise PiIsolationError(f"Pi isolation flag required exactly once: {flag}")
    if values_for("--extension") != [extension_path]:
        raise PiIsolationError("Pi must load exactly pi_extension_path")
    isolation_start = 2 + len(cli_args)
    if args[isolation_start:-2] != isolation_args:
        raise PiIsolationError("Pi argv isolation arguments differ from config")
    if len(args) < 2 or args[-2] != "--session-dir" or not args[-1]:
        raise PiIsolationError("Pi argv must end with --session-dir <dir>")
    return True
