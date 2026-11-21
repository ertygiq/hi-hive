"""The test entry point keeps worker counts bounded and live tests serial."""

import os
import subprocess
from pathlib import Path

import pytest

RUN = Path(__file__).resolve().parent / "run"


@pytest.mark.parametrize("live,extra,workers", [
    (False, [], "4"),
    (True, [], "0"),
    (True, ["-n", "4"], "0"),
    (False, ["-n", "0", "tests/test_pool.py"], "4"),
])
def test_runner_worker_defaults_and_forwarded_arguments(tmp_path, live, extra, workers):
    log = tmp_path / "commands"
    for name in ("uv", "node"):
        executable = tmp_path / name
        executable.write_text('#!/bin/sh\nprintf "%s\\n" "$0 $*" >> "$TEST_COMMAND_LOG"\n')
        executable.chmod(0o755)
    env = os.environ.copy()
    env.update(PATH=f"{tmp_path}:/usr/bin:/bin", TEST_COMMAND_LOG=str(log))
    if live:
        env["HIVE_LIVE_SMOKE"] = "1"
    else:
        env.pop("HIVE_LIVE_SMOKE", None)
    subprocess.run([str(RUN), *extra], env=env, check=True, capture_output=True, text=True)
    pytest_command, node_command = log.read_text().splitlines()
    expected = ["run", "pytest", "-q", *extra, "-n", workers] if live else ["run", "pytest", "-q", "-n", workers, *extra]
    assert pytest_command.endswith(" ".join(expected))
    assert "--test tests/ui_live_test.js tests/test_installer_ui.js" in node_command


def test_runner_help_explains_worker_override(tmp_path):
    uv = tmp_path / "uv"
    uv.write_text("#!/bin/sh\nexit 0\n")
    uv.chmod(0o755)
    env = {**os.environ, "PATH": f"{tmp_path}:/usr/bin:/bin"}
    result = subprocess.run([str(RUN), "--help"], env=env, check=True, capture_output=True, text=True)
    assert "-n 0" in result.stdout
    assert "-n N" in result.stdout
    assert "live smoke: serial" in result.stdout
