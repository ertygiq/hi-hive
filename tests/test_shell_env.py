import os
import stat

import pytest

from hihive.shell_env import adopt_login_shell_env, read_login_shell_env


def fake_shell(directory, body):
    """A shell that is called as: shell -l -i -c COMMAND."""
    path = os.path.join(directory, "fake-shell")
    with open(path, "w") as file:
        file.write(f"#!/bin/bash\n{body}\n")
    os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)
    return path


NOISY = """\
[ "$1 $2 $3" = "-l -i -c" ] || exit 9
echo "Welcome! nvm is loading..."
export GEM_HOME=/gems/ruby-3.2.5 PORT=3000 SHLVL=5
cd /tmp
eval "$4"
echo "goodbye from .zlogout"
"""


def test_startup_file_output_is_ignored_and_variables_are_read(test_dir):
    env = read_login_shell_env(fake_shell(test_dir, NOISY))
    assert env["GEM_HOME"] == "/gems/ruby-3.2.5"
    assert "PWD" not in env and "SHLVL" not in env


def test_shell_variables_are_adopted_but_hive_settings_are_kept(test_dir, monkeypatch):
    monkeypatch.setenv("PORT", "4242")
    monkeypatch.delenv("GEM_HOME", raising=False)
    message = adopt_login_shell_env(fake_shell(test_dir, NOISY))
    assert "loaded" in message
    assert os.environ["GEM_HOME"] == "/gems/ruby-3.2.5"
    assert os.environ["PORT"] == "4242"


@pytest.mark.parametrize("body, reason", [
    ("sleep 30", "did not finish loading within 1 seconds"),
    ("echo 'syntax error in .zshrc' >&2; exit 1", "syntax error in .zshrc"),
])
def test_failure_keeps_the_current_environment(test_dir, monkeypatch, body, reason):
    monkeypatch.setenv("HIVE_MARKER", "unchanged")
    before = dict(os.environ)
    message = adopt_login_shell_env(fake_shell(test_dir, body), timeout=1)
    assert "warning" in message and reason in message
    assert dict(os.environ) == before
