import os
import plistlib
import shutil
import subprocess

import pytest

from hihive import service


class Result:
    def __init__(self, returncode=0):
        self.returncode = returncode


@pytest.fixture
def service_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hive = home / "hive data $cash"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HIVE_HOME", str(hive))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(service, "_executable", lambda: "/opt/hi hive/bin/hihive")
    return home, hive


def test_linux_install_writes_private_direct_systemd_user_service(service_home, monkeypatch):
    _home, hive = service_home
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda args, **kwargs: calls.append((args, kwargs)) or Result())

    previous_umask = os.umask(0o022)
    try:
        service.install(4789)
    finally:
        os.umask(previous_umask)

    path = service.systemd_path()
    text = path.read_text()
    assert path.stat().st_mode & 0o777 == 0o600
    assert hive.stat().st_mode & 0o777 == 0o700
    assert 'ExecStart=/opt/hi\\x20hive/bin/hihive serve --shell-env' in text
    assert f"WorkingDirectory={service._systemd_path(hive)}" in text
    assert 'Environment="PORT=4789"' in text
    assert f'Environment="HIVE_HOME={hive}"' in text
    assert "$cash" in text and "$$cash" not in text
    assert 'Environment="HIVE_SUPERVISOR=systemd"' in text
    assert "UMask=0077" in text
    assert "KillMode=control-group" in text
    assert "Restart=always" in text
    assert "After=default.target" not in text
    assert "token" not in text.lower()
    assert "/bin/sh" not in text
    assert [call[0] for call in calls] == [
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", service.UNIT_NAME],
        ["systemctl", "--user", "restart", service.UNIT_NAME],
    ]


def test_linux_reinstall_applies_changed_port_and_preserves_existing_home_mode(service_home, monkeypatch):
    _home, hive = service_home
    hive.mkdir(parents=True)
    hive.chmod(0o755)
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda args, **kwargs: calls.append(args) or Result())

    service.install(4567)
    service.install(4789)

    text = service.systemd_path().read_text()
    assert 'Environment="PORT=4789"' in text
    assert hive.stat().st_mode & 0o777 == 0o755
    assert calls.count(["systemctl", "--user", "restart", service.UNIT_NAME]) == 2


def test_linux_install_rejects_hive_home_that_is_a_file(service_home, monkeypatch):
    _home, hive = service_home
    hive.parent.mkdir(parents=True)
    hive.write_text("not a directory")
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must fail before systemctl"))

    with pytest.raises(FileExistsError):
        service.install(4567)


@pytest.mark.skipif(not shutil.which("systemd-analyze"), reason="systemd-analyze is not installed")
def test_generated_linux_unit_passes_systemd_analyze(service_home, tmp_path, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "_executable", lambda: "/bin/echo")
    path = tmp_path / "com.hihive.service"
    path.write_text(service._systemd_unit(4567))

    result = subprocess.run(["systemd-analyze", "verify", str(path)], capture_output=True, text=True)

    assert result.returncode == 0, result.stderr


def test_linux_status_restart_and_uninstall_use_systemctl_user(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    service.systemd_path().parent.mkdir(parents=True, exist_ok=True)
    service.systemd_path().write_text("unit")
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda args, **kwargs: calls.append((args, kwargs)) or Result())

    service.status()
    service.restart()
    service.uninstall()

    assert [call[0] for call in calls] == [
        ["systemctl", "--user", "status", service.UNIT_NAME, "--no-pager"],
        ["systemctl", "--user", "restart", service.UNIT_NAME],
        ["systemctl", "--user", "disable", "--now", service.UNIT_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not service.systemd_path().exists()


def test_macos_install_keeps_launchd_and_marks_the_supervisor(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service, "_launchd_bootout", lambda: None)
    calls = []
    monkeypatch.setattr(service.subprocess, "run",
                        lambda args, **kwargs: calls.append((args, kwargs)) or Result())

    service.install(4567)

    with open(service.launchd_path(), "rb") as file:
        plist = plistlib.load(file)
    assert plist["ProgramArguments"] == ["/opt/hi hive/bin/hihive", "serve", "--shell-env"]
    assert plist["EnvironmentVariables"]["HIVE_SUPERVISOR"] == "launchd"
    assert plist["Umask"] == 0o077
    assert calls[0][0][:2] == ["launchctl", "bootstrap"]


def test_other_platforms_keep_foreground_fallback(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    with pytest.raises(SystemExit, match="run `hihive serve`"):
        service.install()
