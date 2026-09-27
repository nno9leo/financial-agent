import copy
import json
from pathlib import Path

import pytest

from app.agent.pi_args import PiIsolationError, build_pi_argv, read_runtime_config, validate_pi_argv


ROOT = Path(__file__).resolve().parents[1]


def test_config_and_python_builder_have_all_mandatory_isolation_flags():
    config = read_runtime_config()
    args = build_pi_argv("/tmp/pi-smoke-session", config)
    assert args == ["--mode", "rpc", *config["pi_cli_args"], *config["pi_isolation_args"], "--session-dir", "/tmp/pi-smoke-session"]
    assert "--tools" in args and args[args.index("--tools") + 1] == ",".join(config["pi_tool_allowlist"])
    assert all(args.count(flag) == 1 for flag in ("--no-extensions", "-na", "--no-skills", "--no-prompt-templates"))
    assert args.count("--extension") == 1 and args[args.index("--extension") + 1] == config["pi_extension_path"]


@pytest.mark.parametrize("mutation", ["missing_tools", "altered_tools", "missing_no_extensions", "altered_extension", "missing_na", "missing_skills", "missing_templates", "approve_short", "approve_long"])
def test_python_builder_refuses_isolation_regressions(mutation):
    config = read_runtime_config()
    args = build_pi_argv("/tmp/pi-smoke-session", config)
    args = list(args)
    if mutation == "missing_tools":
        del args[args.index("--tools"):args.index("--tools") + 2]
    elif mutation == "altered_tools":
        args[args.index("--tools") + 1] = "read,bash"
    elif mutation == "missing_no_extensions":
        args.remove("--no-extensions")
    elif mutation == "altered_extension":
        args[args.index("--extension") + 1] = "./other.ts"
    elif mutation == "missing_na":
        args.remove("-na")
    elif mutation == "missing_skills":
        args.remove("--no-skills")
    elif mutation == "missing_templates":
        args.remove("--no-prompt-templates")
    elif mutation == "approve_short":
        args.append("-a")
    else:
        args.append("--approve")
    with pytest.raises(PiIsolationError):
        validate_pi_argv(args, config)
