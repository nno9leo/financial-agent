"""Default test-suite launch and trace isolation.

Tests marked ``live_llm`` may opt out explicitly. The fake Pi RPC integration
tests are still offline and may start only ``tests/fixtures/eval/fake_pi_rpc.py``.
All other Pi launch entry points and child processes fail closed.
"""
from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

from evals.pi_runner import run_one as _original_run_one


def pytest_addoption(parser):
    parser.addoption("--force-clean-git", action="store_true",
                     help="simulate a clean git tree for eval runner guard tests")


@pytest.fixture(autouse=True)
def block_live_launches_and_isolate_traces(request, monkeypatch, tmp_path):
    if request.node.get_closest_marker("live_llm"):
        yield
        return

    def blocked(*args, **kwargs):
        raise AssertionError("live launch attempted in default test suite")

    import evals.pi_runner as pi_runner
    import evals.runner as runner
    from app.tracing.jsonl import JsonlTraceWriter
    from app.api import app as api_module

    stub = (Path(__file__).parent / "fixtures/eval/fake_pi_rpc.py").resolve()
    def guarded_run_one(*args, **kwargs):
        pi_bin = kwargs.get("pi_bin")
        if isinstance(pi_bin, str) and Path(pi_bin).resolve() == stub:
            return _original_run_one(*args, **kwargs)
        if isinstance(pi_bin, (list, tuple)) and len(pi_bin) == 2 \
                and Path(pi_bin[0]).resolve() == Path(sys.executable).resolve() \
                and Path(pi_bin[1]).resolve() == stub:
            return _original_run_one(*args, **kwargs)
        return blocked(*args, **kwargs)

    monkeypatch.setattr(pi_runner, "run_one", guarded_run_one)
    monkeypatch.setattr(runner, "_run_pi_items", blocked)
    if request.config.getoption("--force-clean-git"):
        monkeypatch.setattr(runner, "_git", lambda: ("pytest-forced-clean", False))

    original_popen = subprocess.Popen
    def guarded_popen(args, *popen_args, **kwargs):
        argv = args if isinstance(args, (list, tuple)) else [args]
        normalized = [os.fspath(arg) for arg in argv]
        allowed = bool(normalized) and Path(normalized[0]).resolve() == stub
        allowed = allowed or (len(normalized) > 1 and Path(normalized[0]).resolve() == Path(sys.executable).resolve()
                              and Path(normalized[1]).resolve() == stub)
        if not allowed:
            raise AssertionError("live launch attempted in default test suite")
        return original_popen(args, *popen_args, **kwargs)

    monkeypatch.setattr(subprocess, "Popen", guarded_popen)
    monkeypatch.setattr(api_module, "JsonlTraceWriter", lambda *args, **kwargs: JsonlTraceWriter(tmp_path / "traces"))
    yield
