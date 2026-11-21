import os
import plistlib
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

from hihive import paths, service


class Result:
    def __init__(self, returncode=0, stdout=""):
        self.returncode = returncode
        self.stdout = stdout


@pytest.fixture
def service_home(tmp_path, monkeypatch):
    home = tmp_path / "home"
    hive = home / "hive data $cash"
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("HIVE_HOME", str(hive))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setattr(service, "_executable", lambda: "/opt/hi hive/bin/hihive")
    monkeypatch.setattr(service, "_server_config", lambda timeout=0.5: None)
    monkeypatch.setattr(service, "_wait_for_health", lambda selected, timeout=10: True)
    monkeypatch.setattr(service, "_systemd_manager_environment", lambda: {"HOME": str(home)})
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
    assert f"WorkingDirectory={hive}" in text
    assert "WorkingDirectory=" in text and "\\x20" not in text.split("WorkingDirectory=", 1)[1].splitlines()[0]
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
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "daemon-reload"],
        ["systemctl", "--user", "enable", service.UNIT_NAME],
        ["systemctl", "--user", "restart", service.UNIT_NAME],
    ]


@pytest.mark.parametrize("backend", ["launchd", "systemd"])
def test_service_install_refuses_symlink_target_without_changing_victim(service_home, monkeypatch, backend):
    home, _hive = service_home
    path = service.launchd_path() if backend == "launchd" else service.systemd_path()
    path.parent.mkdir(parents=True)
    victim = home / "victim"
    victim.write_text("KEEP\n")
    path.symlink_to(victim)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    installer = service._install_launchd if backend == "launchd" else service._install_systemd
    with pytest.raises(SystemExit, match="regular file"):
        installer(4567)

    assert path.is_symlink()
    assert victim.read_text() == "KEEP\n"


@pytest.mark.parametrize("backend", ["launchd", "systemd"])
def test_service_install_refuses_symlinked_directory_ancestor(service_home, monkeypatch, backend):
    home, _hive = service_home
    redirected = home / "redirected"
    redirected.mkdir(parents=True)
    ancestor = home / ("Library" if backend == "launchd" else ".config")
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    ancestor.symlink_to(redirected)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    installer = service._install_launchd if backend == "launchd" else service._install_systemd
    with pytest.raises(SystemExit, match="symbolic links"):
        installer(4567)

    assert not (redirected / ("LaunchAgents/com.hihive.plist" if backend == "launchd" else "systemd/user/com.hihive.service")).exists()


def test_systemd_install_refuses_symlinked_custom_xdg_config_ancestor_before_creating_directories(service_home, monkeypatch):
    home, _hive = service_home
    redirected = home / "redirected-install-xdg"
    redirected.mkdir(parents=True)
    config_link = home / "config-install-link"
    config_link.symlink_to(redirected)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_link))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(config_link)})
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    with pytest.raises(SystemExit, match="symbolic links"):
        service._install_systemd(4567)

    assert not (redirected / "systemd").exists()


@pytest.mark.parametrize("backend", ["launchd", "systemd"])
def test_service_uninstall_refuses_symlinked_ancestor_without_deleting_redirected_file(service_home, monkeypatch, backend):
    home, _hive = service_home
    redirected = home / "redirected-uninstall"
    redirected.mkdir(parents=True)
    ancestor = home / ("Library" if backend == "launchd" else ".config")
    ancestor.parent.mkdir(parents=True, exist_ok=True)
    ancestor.symlink_to(redirected)
    relative = "LaunchAgents/com.hihive.plist" if backend == "launchd" else "systemd/user/com.hihive.service"
    victim = redirected / relative
    victim.parent.mkdir(parents=True)
    victim.write_text("KEEP\n")
    calls = []
    monkeypatch.setattr(service, "_launchd_loaded", lambda: pytest.fail("launchctl state must not be checked"))
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())

    uninstaller = service._uninstall_launchd if backend == "launchd" else service._uninstall_systemd
    with pytest.raises(SystemExit, match="symbolic links"):
        uninstaller(quiet=True)

    assert victim.read_text() == "KEEP\n"
    assert calls == []


def test_systemd_uninstall_refuses_symlinked_custom_xdg_config_ancestor(service_home, monkeypatch):
    home, _hive = service_home
    redirected = home / "redirected-xdg"
    redirected.mkdir(parents=True)
    config_link = home / "config-link"
    config_link.symlink_to(redirected)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(config_link))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(config_link)})
    victim = redirected / "systemd/user/com.hihive.service"
    victim.parent.mkdir(parents=True)
    victim.write_text("KEEP\n")
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())

    with pytest.raises(SystemExit, match="symbolic links"):
        service._uninstall_systemd(quiet=True)

    assert victim.read_text() == "KEEP\n"
    assert calls == []


def test_systemd_operations_use_manager_home_and_external_xdg_not_process_values(service_home, monkeypatch):
    home, _hive = service_home
    manager_home = home.parent / "manager-home"
    manager_home.mkdir(mode=0o700)
    manager_root = home.parent / "manager-config"
    manager_root.mkdir(mode=0o700)
    process_root = home.parent / "process-config"
    process_root.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(process_root))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(manager_home), "XDG_CONFIG_HOME": str(manager_root)})
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())

    service._install_systemd(4567)
    path = manager_root / "systemd/user/com.hihive.service"
    assert path.exists()
    assert not (process_root / "systemd").exists()
    assert not (home / ".config/systemd").exists()

    service.status()
    service.restart()

    calls.clear()
    service._uninstall_systemd(quiet=True)
    assert not path.exists()
    assert calls == [
        ["systemctl", "--user", "disable", "--now", service.UNIT_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]


def test_manager_only_external_xdg_works_with_process_xdg_unset(service_home, monkeypatch):
    home, _hive = service_home
    manager_root = home.parent / "manager-only-config"
    manager_root.mkdir(mode=0o700)
    monkeypatch.delenv("XDG_CONFIG_HOME", raising=False)
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(manager_root)})
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: Result())

    service._install_systemd(4567)

    assert (manager_root / "systemd/user/com.hihive.service").exists()
    assert not (home / ".config/systemd").exists()


def test_systemd_manager_environment_is_read_without_shell_parsing(monkeypatch):
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: Result(
        stdout="XDG_CONFIG_HOME=/safe/config\nVALUE=contains=equals\n"))

    environment = service._systemd_manager_environment()

    assert environment == {"XDG_CONFIG_HOME": "/safe/config", "VALUE": "contains=equals"}


@pytest.mark.parametrize("name", ["HOME", "XDG_CONFIG_HOME"])
def test_systemd_manager_environment_rejects_ambiguous_duplicate(monkeypatch, name):
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: Result(
        stdout=f"{name}=/one\n{name}=/two\n"))

    with pytest.raises(SystemExit, match=f"ambiguous {name}"):
        service._systemd_manager_environment()


def test_systemd_manager_missing_or_relative_home_is_rejected_before_writes(service_home, monkeypatch):
    home, _hive = service_home
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())
    for environment, message in (({}, "no HOME"), ({"HOME": "relative"}, "relative HOME")):
        monkeypatch.setattr(service, "_systemd_manager_environment", lambda environment=environment: environment)
        with pytest.raises(SystemExit, match=message):
            service._install_systemd(4567)

    assert not (home / ".config/systemd").exists()
    assert calls == []


def test_manager_home_fallback_ignores_process_home_and_external_xdg(service_home, monkeypatch):
    home, _hive = service_home
    manager_home = home.parent / "fallback-manager-home"
    manager_home.mkdir(mode=0o700)
    process_only = home.parent / "process-only-config"
    process_only.mkdir(mode=0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(process_only))
    monkeypatch.setattr(service, "_systemd_manager_environment", lambda: {"HOME": str(manager_home)})
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: Result())

    service._install_systemd(4567)

    assert (manager_home / ".config/systemd/user/com.hihive.service").exists()
    assert not (home / ".config/systemd").exists()
    assert not (process_only / "systemd").exists()


def test_systemd_manager_relative_xdg_is_rejected_before_writes(service_home, monkeypatch):
    home, _hive = service_home
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": "relative"})
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda *args, **kwargs: calls.append(args) or Result())

    with pytest.raises(SystemExit, match="relative XDG_CONFIG_HOME"):
        service._install_systemd(4567)

    assert not (home / "relative").exists()
    assert calls == []


def test_systemd_external_xdg_under_root_owned_sticky_temp_supports_install_and_uninstall(service_home, monkeypatch):
    home, _hive = service_home
    shared = (Path("/private/tmp") if Path("/private/tmp").exists() else Path("/tmp")).resolve()
    details = shared.stat()
    if details.st_uid != 0 or not details.st_mode & 0o022 or not details.st_mode & 0o1000:
        pytest.skip("no root-owned sticky temporary directory is available")
    external = Path(tempfile.mkdtemp(prefix="hi-hive-xdg-", dir=shared))
    external.chmod(0o700)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(external))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(external)})
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())
    try:
        service._install_systemd(4567)
        path = external / "systemd/user/com.hihive.service"
        assert path.exists()

        calls.clear()
        service._uninstall_systemd(quiet=True)
        assert not path.exists()
        assert calls == [
            ["systemctl", "--user", "disable", "--now", service.UNIT_NAME],
            ["systemctl", "--user", "daemon-reload"],
        ]
    finally:
        shutil.rmtree(external, ignore_errors=True)


def test_systemd_uninstall_refuses_group_writable_external_xdg_without_changes(service_home, monkeypatch):
    home, _hive = service_home
    external = home.parent / "external-uninstall-unsafe"
    victim = external / "systemd/user/com.hihive.service"
    victim.parent.mkdir(parents=True)
    victim.write_text("KEEP\n")
    external.chmod(0o770)
    monkeypatch.setenv("XDG_CONFIG_HOME", str(external))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(external)})
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())

    with pytest.raises(SystemExit, match="group/world-writable"):
        service._uninstall_systemd(quiet=True)

    assert victim.read_text() == "KEEP\n"
    assert calls == []


@pytest.mark.parametrize("unsafe", ["group-writable", "sticky-non-root", "symlink", "unsafe-parent"])
def test_systemd_install_refuses_unsafe_external_xdg_config_home_without_systemctl_calls(service_home, monkeypatch, unsafe):
    home, _hive = service_home
    calls = []
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())
    if unsafe in ("group-writable", "sticky-non-root"):
        external = home.parent / f"external-{unsafe}"
        external.mkdir(mode=0o700)
        external.chmod(0o1777 if unsafe == "sticky-non-root" else 0o770)
    elif unsafe == "symlink":
        redirected = home.parent / "external-redirected"
        redirected.mkdir(mode=0o700)
        external = home.parent / "external-link"
        external.symlink_to(redirected)
    else:
        parent = home.parent / "external-parent"
        parent.mkdir(mode=0o700)
        parent.chmod(0o770)
        external = parent / "config"
    monkeypatch.setenv("XDG_CONFIG_HOME", str(external))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(external)})

    with pytest.raises(SystemExit, match="group/world-writable|symbolic links"):
        service._install_systemd(4567)

    assert calls == []


@pytest.mark.parametrize("operation", ["install", "uninstall"])
@pytest.mark.parametrize("unsafe", ["symlinked-ancestor", "group-writable-ancestor"])
def test_external_xdg_rejects_unsafe_ancestors_before_changes_or_manager_calls(
        service_home, monkeypatch, operation, unsafe):
    home, _hive = service_home
    if unsafe == "symlinked-ancestor":
        redirected = home.parent / "external-chain-target"
        redirected.mkdir(mode=0o700)
        ancestor = home.parent / "external-chain-link"
        ancestor.symlink_to(redirected)
        root = ancestor / "config"
        actual_root = redirected / "config"
    else:
        ancestor = home.parent / "external-chain-parent"
        ancestor.mkdir(mode=0o700)
        ancestor.chmod(0o770)
        root = ancestor / "config"
        actual_root = root
    actual_root.mkdir(mode=0o700)
    marker = actual_root / "KEEP"
    marker.write_text("KEEP\n")
    victim = actual_root / "systemd/user/com.hihive.service"
    if operation == "uninstall":
        victim.parent.mkdir(parents=True)
        victim.write_text("UNIT\n")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(root))
    monkeypatch.setattr(service, "_systemd_manager_environment",
                        lambda: {"HOME": str(home), "XDG_CONFIG_HOME": str(root)})
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
    calls = []
    monkeypatch.setattr(service.subprocess, "run", lambda args, **kwargs: calls.append(args) or Result())

    with pytest.raises(SystemExit, match="symbolic links|group/world-writable"):
        if operation == "install":
            service._install_systemd(4567)
        else:
            service._uninstall_systemd(quiet=True)

    assert marker.read_text() == "KEEP\n"
    assert victim.exists() is (operation == "uninstall")
    assert calls == []


@pytest.mark.parametrize("backend", ["launchd", "systemd"])
def test_service_install_refuses_non_regular_target(service_home, monkeypatch, backend):
    path = service.launchd_path() if backend == "launchd" else service.systemd_path()
    path.mkdir(parents=True)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    installer = service._install_launchd if backend == "launchd" else service._install_systemd
    with pytest.raises(SystemExit, match="regular file"):
        installer(4567)


@pytest.mark.parametrize("backend", ["launchd", "systemd"])
def test_service_install_refuses_group_writable_regular_target(service_home, monkeypatch, backend):
    path = service.launchd_path() if backend == "launchd" else service.systemd_path()
    path.parent.mkdir(parents=True)
    path.write_text("KEEP\n")
    path.chmod(0o620)
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")

    installer = service._install_launchd if backend == "launchd" else service._install_systemd
    with pytest.raises(SystemExit, match="group/world-writable"):
        installer(4567)

    assert path.read_text() == "KEEP\n"


def test_atomic_service_write_replaces_regular_file_and_leaves_no_temp(service_home):
    path = service.systemd_path()
    path.parent.mkdir(parents=True)
    path.write_text("old\n")

    service._atomic_service_file(path, b"new\n")

    assert path.read_text() == "new\n"
    assert path.stat().st_mode & 0o777 == 0o600
    assert list(path.parent.glob(f".{path.name}.*")) == []


def test_systemd_working_directory_keeps_spaces_raw_and_escapes_specifiers():
    assert service._systemd_working_directory("/tmp/wd test%name") == "/tmp/wd test%%name"
    with pytest.raises(SystemExit, match="absolute path"):
        service._systemd_working_directory("relative path")


@pytest.mark.parametrize("path", ["/tmp/bad\npath", "/tmp/bad\tpath", "/tmp/bad\fpath", "/tmp/bad\x7fpath"])
def test_systemd_working_directory_rejects_control_characters(path):
    with pytest.raises(SystemExit, match="control character"):
        service._systemd_working_directory(path)


@pytest.mark.parametrize("path", ["/tmp/back\\slash", "/tmp/trailing\\", '/tmp/double"quote'])
def test_systemd_working_directory_rejects_unit_syntax_characters(path):
    with pytest.raises(SystemExit, match="backslash or double quote"):
        service._systemd_working_directory(path)


def test_systemd_working_directory_rejects_trailing_whitespace():
    with pytest.raises(SystemExit, match="end with whitespace"):
        service._systemd_working_directory("/tmp/trailing ")


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
    monkeypatch.setattr(service.subprocess, "run", lambda args, **_kwargs: Result()
                        if args == ["systemctl", "--user", "show-environment"]
                        else pytest.fail("must fail before service installation"))

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
    monkeypatch.setattr(service, "_server_config", lambda timeout=0.5: {"service_manager": "systemd"})
    paths.server_lock_file().parent.mkdir(parents=True, exist_ok=True)
    paths.server_lock_file().write_text("4567\n")

    service.status()
    service.restart()
    service.uninstall()

    assert [call[0] for call in calls] == [
        ["systemctl", "--user", "show-environment"],
        ["systemctl", "--user", "is-active", "--quiet", service.UNIT_NAME],
        ["systemctl", "--user", "restart", service.UNIT_NAME],
        ["systemctl", "--user", "disable", "--now", service.UNIT_NAME],
        ["systemctl", "--user", "daemon-reload"],
    ]
    assert not service.systemd_path().exists()


def test_macos_install_keeps_launchd_and_marks_the_supervisor(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "darwin")
    monkeypatch.setattr(service.shutil, "which", lambda name: f"/usr/bin/{name}")
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
    assert calls[0][0][:3] == ["launchctl", "print", service._domain()]
    assert calls[1][0][:2] == ["launchctl", "bootstrap"]


def test_install_refuses_terminal_lock_owner(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "_server_config", lambda timeout=0.5: {"service_manager": None})
    monkeypatch.setattr(service, "_install_systemd", lambda _port: pytest.fail("must not replace terminal server"))

    with pytest.raises(SystemExit, match="already running in a terminal"):
        service.install(4567)


def test_health_timeout_unloads_failed_service_before_manual_guidance(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "available", lambda: True)
    monkeypatch.setattr(service, "_install_systemd", lambda _port: None)
    monkeypatch.setattr(service, "_wait_for_health", lambda selected, timeout=10: False)
    cleaned = []
    monkeypatch.setattr(service, "_cleanup_failed_service", lambda selected: cleaned.append(selected))

    with pytest.raises(SystemExit, match="did not answer") as failure:
        service.install(4567)

    assert cleaned == ["systemd"]
    assert "hihive serve" in str(failure.value)


def test_unavailable_linux_service_has_clean_manual_fallback(service_home, monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "linux")
    monkeypatch.setattr(service, "available", lambda: False)

    with pytest.raises(SystemExit, match="no systemd user session") as failure:
        service.install(4567)

    assert "hihive serve" in str(failure.value)


def test_other_platforms_keep_foreground_fallback(monkeypatch):
    monkeypatch.setattr(service.sys, "platform", "win32")
    with pytest.raises(SystemExit, match="run `hihive serve`"):
        service.install()
