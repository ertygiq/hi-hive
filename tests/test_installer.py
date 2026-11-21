import json
import os
import stat
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
INSTALLER = ROOT / "install.sh"
SHA = "07ede6f7f9b6e9bfa61f220f20f31684252fffb4"


def executable(path, text):
    path.write_text("#!/bin/sh\nset -eu\n" + text)
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def installer_environment(tmp_path, *, root=False, invalid_sha=False):
    home = tmp_path / "home with space"
    mocks = tmp_path / "mocks"
    temporary = tmp_path / "tmp"
    bin_dir = home / ".local" / "bin"
    home.mkdir()
    mocks.mkdir()
    temporary.mkdir()
    bin_dir.mkdir(parents=True)
    log = tmp_path / "commands.log"

    executable(mocks / "id", f"if [ \"${{1:-}}\" = -u ]; then echo {'0' if root else os.getuid()}; else /usr/bin/id \"$@\"; fi\n")
    executable(mocks / "uname", 'case "${1:-}" in -s) echo Linux;; -m) echo x86_64;; *) echo Linux;; esac\n')
    sha = "bad" if invalid_sha else SHA
    executable(mocks / "curl", f'''out=
while [ "$#" -gt 0 ]; do
  if [ "$1" = --output ]; then shift; out=$1; fi
  shift
done
case "$out" in
  *.tar.gz) printf 'fake archive\\n' > "$out";;
  *) printf '{{"ref":"refs/heads/main","object":{{"sha":"{sha}"}}}}\\n' > "$out";;
esac
printf 'curl\\n' >> "{log}"
''')
    executable(mocks / "systemctl", 'exit 1\n')
    executable(mocks / "uv", f'''printf 'uv %s\\n' "$*" >> "{log}"
if [ "${{1:-}}" = --version ]; then echo 'uv 0.10.0'; exit 0; fi
if [ "${{1:-}} ${{2:-}} ${{3:-}}" = 'tool dir --bin' ]; then echo '{bin_dir}'; exit 0; fi
if [ "${{1:-}} ${{2:-}}" = 'tool install' ]; then
  if [ -z "${{UV_TOOL_BIN_DIR:-}}" ] && [ "${{HIVE_FAKE_FINAL_INSTALL_FAIL:-0}}" = 1 ]; then exit 42; fi
  target_bin=${{UV_TOOL_BIN_DIR:-{bin_dir}}}
  target_tools=${{UV_TOOL_DIR:-{home}/.local/share/uv/tools}}
  mkdir -p "$target_bin" "$target_tools/hi-hive/bin"
  cat > "$target_bin/hihive" <<'SH'
#!/bin/sh
printf 'hihive %s\\n' "$*" >> "$HIHIVE_TEST_LOG"
case "${{1:-}}" in
  --version) echo 'hi-hive 0.2.0';;
  setup-harnesses) : ;;
  service)
    if [ "${{2:-}}" = installed ]; then exit "${{HIVE_FAKE_SERVICE_INSTALLED_EXIT:-3}}"; fi
    exit "${{HIVE_FAKE_SERVICE_EXIT:-0}}"
    ;;
  health) exit "${{HIVE_FAKE_HEALTH_EXIT:-1}}" ;;
  open) : ;;
esac
SH
  chmod +x "$target_bin/hihive"
  cp "$target_bin/hihive" "$target_bin/hih"
  cat > "$target_tools/hi-hive/bin/python" <<'SH'
#!/bin/sh
case "$*" in
  *platform.python_version*) echo 3.14.7;;
  *sys.executable*) printf '%s\\n' "$0";;
esac
SH
  chmod +x "$target_tools/hi-hive/bin/python"
  exit 0
fi
if [ "${{1:-}} ${{2:-}}" = 'tool uninstall' ]; then rm -f '{bin_dir}/hihive' '{bin_dir}/hih'; exit 0; fi
exit 1
''')

    env = os.environ.copy()
    env.update({
        "HOME": str(home), "PATH": f"{mocks}:/usr/bin:/bin", "SHELL": "/bin/sh",
        "TERM": "dumb", "HIHIVE_TEST_LOG": str(log), "XDG_STATE_HOME": str(home / ".state"),
        "TMPDIR": str(temporary),
    })
    return home, bin_dir, log, env


def run_installer(tmp_path, *args, **options):
    home, bin_dir, log, env = installer_environment(tmp_path, **options)
    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service", *args],
                            env=env, capture_output=True, text=True, timeout=30)
    return result, home, bin_dir, log, env


def test_install_documentation_explains_curl_pipe_trust_boundary():
    documentation = (ROOT / "docs/install.md").read_text()

    assert "immediately runs it as your user" in documentation
    assert "trust the repository and download host" in documentation
    assert "exact `main@SHA` protects the hi-hive source archive, not the already-running installer script" in documentation
    assert "inspect" in documentation
    assert "manual uv" in documentation


def test_truncated_curl_pipe_prefixes_do_not_execute_or_change_files(tmp_path):
    lines = INSTALLER.read_text().splitlines(keepends=True)
    prefixes = [1, 20, next(i for i, line in enumerate(lines, 1) if line == "main() {\n") + 20, len(lines) - 1]
    for index, count in enumerate(prefixes):
        case = tmp_path / str(index)
        case.mkdir()
        home, bin_dir, log, env = installer_environment(case)
        prefix = case / "truncated.sh"
        prefix.write_text("".join(lines[:count]))

        subprocess.run(["dash", str(prefix), "--plain", "--no-service"], env=env,
                       capture_output=True, text=True, timeout=10)

        assert not (home / ".profile").exists()
        assert not (bin_dir / "hihive").exists()
        assert not log.exists()
        assert not list((case / "tmp").iterdir())


def test_compatible_uv_in_normal_user_bin_is_reused_without_overwrite(tmp_path):
    home, bin_dir, _log, env = installer_environment(tmp_path)
    mocks = Path(env["PATH"].split(":", 1)[0])
    (mocks / "uv").replace(bin_dir / "uv")
    executable(mocks / "login-shell", "exit 0\n")
    env["SHELL"] = str(mocks / "login-shell")
    before = (bin_dir / "uv").read_bytes()

    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert (bin_dir / "uv").read_bytes() == before
    assert f"found 0.10.0 at {bin_dir}/uv" in result.stdout


@pytest.mark.parametrize("existing", ["uv", "uvx"])
def test_missing_uv_refuses_to_overwrite_unowned_standard_bin_files(tmp_path, existing):
    home, bin_dir, _log, env = installer_environment(tmp_path)
    mocks = Path(env["PATH"].split(":", 1)[0])
    (mocks / "uv").unlink()
    executable(mocks / "login-shell", "exit 0\n")
    env["SHELL"] = str(mocks / "login-shell")
    occupied = bin_dir / existing
    occupied.write_text("KEEP ME\n")
    before = occupied.read_bytes()

    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    assert "refusing to overwrite existing" in result.stderr
    assert occupied.read_bytes() == before
    assert not (bin_dir / "hihive").exists()
    assert not (home / ".profile").exists()


def test_missing_uv_downloads_fixed_verified_standard_uv_to_normal_user_bin(tmp_path):
    home, bin_dir, log, env = installer_environment(tmp_path)
    mocks = Path(env["PATH"].split(":", 1)[0])
    template = tmp_path / "uv-template"
    template.write_bytes((mocks / "uv").read_bytes())
    template.chmod(0o755)
    (mocks / "uv").unlink()
    executable(mocks / "login-shell", "exit 0\n")
    env["SHELL"] = str(mocks / "login-shell")
    env["HIHIVE_UV_TEMPLATE"] = str(template)
    executable(mocks / "sha256sum", "echo '230e328948c92dd1ebad83949c4d56e83813dfe9c6362a4c519e6a227973f1ae  file'\n")
    executable(mocks / "tar", '''dest=
while [ "$#" -gt 0 ]; do
  if [ "$1" = -C ]; then shift; dest=$1; fi
  shift
done
mkdir -p "$dest/uv-x86_64-unknown-linux-gnu"
cp "$HIHIVE_UV_TEMPLATE" "$dest/uv-x86_64-unknown-linux-gnu/uv"
chmod +x "$dest/uv-x86_64-unknown-linux-gnu/uv"
''')

    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 0, result.stderr
    assert "downloading 0.10.0 from https://github.com/astral-sh/uv/releases/download/0.10.0/" in result.stdout
    assert f"using 0.10.0 at {bin_dir}/uv" in result.stdout
    assert (bin_dir / "uv").exists()
    assert "astral.sh/uv/install" not in result.stdout + result.stderr
    assert "curl" in log.read_text()
    assert not list((tmp_path / "tmp").iterdir())

    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    env["PATH"] = f"{bin_dir}:{env['PATH']}"
    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                           capture_output=True, text=True, timeout=30)
    assert rerun.returncode == 0, rerun.stderr
    assert f"found 0.10.0 at {bin_dir}/uv" in rerun.stdout


def test_wrong_uv_checksum_stops_before_extraction_or_profile_change(tmp_path):
    home, _bin_dir, _log, env = installer_environment(tmp_path)
    mocks = Path(env["PATH"].split(":", 1)[0])
    (mocks / "uv").unlink()
    executable(mocks / "login-shell", "exit 0\n")
    env["SHELL"] = str(mocks / "login-shell")
    executable(mocks / "sha256sum", "echo 'bad  file'\n")
    executable(mocks / "tar", "echo tar-ran > '$HOME/tar-ran'\n")

    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    assert "failed SHA-256" in result.stderr
    assert not (home / "tar-ran").exists()
    assert not (home / ".profile").exists()
    assert not list((tmp_path / "tmp").iterdir())


def test_plain_fresh_install_uses_exact_sha_python_314_and_reversible_path_block(tmp_path):
    result, home, bin_dir, log, env = run_installer(tmp_path)

    assert result.returncode == 0, result.stderr
    assert f"hi-hive 0.2.0 from main@{SHA}" in result.stdout
    assert "Python       selected 3.14.7" in result.stdout
    assert "default yes" in result.stdout
    assert (bin_dir / "hihive").exists()
    record = json.loads((home / ".state/hi-hive/install.json").read_text())
    assert record["commit"] == SHA
    profile = (home / ".profile").read_text()
    assert profile.count("# >>> hi-hive installer PATH >>>") == 1
    commands = log.read_text()
    assert f"archive/{SHA}.tar.gz" not in result.stdout or SHA in result.stdout
    assert "--python 3.14" in commands or "uv tool install" in commands
    assert "SECRET_TOKEN" not in result.stdout + result.stderr + profile


def test_same_sha_rerun_skips_package_install_and_does_not_duplicate_profile(tmp_path):
    result, home, bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    before = log.read_text().count("uv tool install")

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    assert "package install skipped" in rerun.stdout
    assert log.read_text().count("uv tool install") == before
    assert (home / ".profile").read_text().count("# >>> hi-hive installer PATH >>>") == 1


def test_force_reinstalls_same_sha(tmp_path):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    before = log.read_text().count("uv tool install")

    repaired = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service", "--repair"],
                              env=env, capture_output=True, text=True, timeout=30)

    assert repaired.returncode == 0, repaired.stderr
    assert log.read_text().count("uv tool install") == before + 2


def test_failed_final_install_keeps_record_config_and_existing_command(tmp_path):
    result, home, bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    old_sha = "b" * 40
    record = home / ".state/hi-hive/install.json"
    state = json.loads(record.read_text())
    state["commit"] = old_sha
    record.write_text(json.dumps(state))
    hive = home / ".hihive"
    hive.mkdir()
    config = hive / "config.json"
    config.write_text('{"marker":"keep","harnesses":[]}\n')
    before_command = (bin_dir / "hihive").read_bytes()
    env["HIVE_FAKE_FINAL_INSTALL_FAIL"] = "1"

    update = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert update.returncode == 1
    assert json.loads(record.read_text())["commit"] == old_sha
    assert config.read_text() == '{"marker":"keep","harnesses":[]}\n'
    assert (bin_dir / "hihive").read_bytes() == before_command
    assert "final package install failed" in update.stderr


def test_fresh_service_failure_records_installed_commit_and_keeps_manual_fallback(tmp_path):
    home, _bin_dir, _log, env = installer_environment(tmp_path)
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_EXIT"] = "1"

    install = subprocess.run(["dash", str(INSTALLER), "--plain"], env=env,
                             capture_output=True, text=True, timeout=30)

    assert install.returncode == 1
    record = json.loads((home / ".state/hi-hive/install.json").read_text())
    assert record["commit"] == SHA
    assert "Run it in a terminal instead" in install.stderr


def test_service_health_failure_rolls_package_record_back_and_keeps_data(tmp_path):
    result, home, _bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    old_sha = "b" * 40
    record = home / ".state/hi-hive/install.json"
    state = json.loads(record.read_text())
    state["commit"] = old_sha
    record.write_text(json.dumps(state))
    hive = home / ".hihive"
    hive.mkdir()
    (hive / "config.json").write_text('{"harnesses":[]}\n')
    (hive / "token").write_text("SECRET_TOKEN\n")
    unit = home / ".config/systemd/user/com.hihive.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("old unit\n")
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "0"
    env["HIVE_FAKE_SERVICE_EXIT"] = "1"

    update = subprocess.run(["dash", str(INSTALLER), "--plain"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert update.returncode == 1
    assert json.loads(record.read_text())["commit"] == old_sha
    assert (hive / "token").read_text() == "SECRET_TOKEN\n"
    assert "SECRET_TOKEN" not in update.stdout + update.stderr
    assert "Restoring the previous package" in update.stderr


def test_rerun_uses_machine_service_query_to_restart_existing_service_on_requested_port(tmp_path):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "0"
    log.write_text("")

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--repair", "--port", "4876"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    commands = log.read_text()
    assert "hihive service installed" in commands
    assert "hihive service install --port 4876" in commands
    assert "running in the background on port 4876" in rerun.stdout


def test_malformed_manager_service_query_fails_without_guessing_run_mode(tmp_path):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "2"
    log.write_text("")

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--repair"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 1
    assert "could not determine whether the manager-visible hi-hive service is installed" in rerun.stderr
    assert "hihive service install --port" not in log.read_text()
    assert "previous version in a terminal" not in rerun.stdout


def test_no_service_wins_on_rerun_even_when_a_service_unit_exists(tmp_path):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    unit = home / ".config/systemd/user/com.hihive.service"
    unit.parent.mkdir(parents=True)
    unit.write_text("existing unit\n")
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "0"
    log.write_text("")

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service", "--repair"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    assert "hihive service installed" in log.read_text()
    assert "hihive service install --port" not in log.read_text()
    assert "existing background service was not restarted because of --no-service" in rerun.stdout
    assert "hihive service restart" in rerun.stdout


@pytest.mark.parametrize("no_service", [False, True])
def test_rerun_continues_in_terminal_mode_when_systemd_user_manager_is_unavailable(tmp_path, no_service):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "2"
    log.write_text("")
    arguments = ["dash", str(INSTALLER), "--plain", "--repair"]
    if no_service:
        arguments.append("--no-service")

    rerun = subprocess.run(arguments, env=env, capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    assert "hihive service installed" not in log.read_text()
    assert "hihive service install --port" not in log.read_text()
    assert "installed but NOT running yet" in rerun.stdout


def test_unavailable_manager_uses_neutral_guidance_for_running_process(tmp_path):
    result, home, _bin_dir, log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    env["HIVE_FAKE_HEALTH_EXIT"] = "0"
    log.write_text("")

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    assert "another hi-hive process is still running the previous version" in rerun.stdout
    assert "cannot identify how that process was started" in rerun.stdout
    assert "in a terminal" not in rerun.stdout
    assert "hihive service installed" not in log.read_text()


def test_available_manager_and_absent_service_classifies_healthy_server_as_foreground(tmp_path):
    result, home, _bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')
    mocks = Path(env["PATH"].split(":", 1)[0])
    executable(mocks / "systemctl", "exit 0\n")
    env["HIVE_FAKE_SERVICE_INSTALLED_EXIT"] = "3"
    env["HIVE_FAKE_HEALTH_EXIT"] = "0"

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 0, rerun.stderr
    assert "still running the previous version in a terminal" in rerun.stdout
    assert "Press Ctrl-C in that terminal, then run: hihive serve" in rerun.stdout


def test_no_modify_path_uses_absolute_commands_without_profile_edit(tmp_path):
    result, home, bin_dir, _log, _env = run_installer(tmp_path, "--no-modify-path")

    assert result.returncode == 0, result.stderr
    assert not (home / ".profile").exists()
    assert f"{bin_dir}/hihive serve" in result.stdout


def test_partial_profile_marker_is_refused_without_touching_user_text(tmp_path):
    result, home, _bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    profile = home / ".profile"
    profile.write_text("user text\n# >>> hi-hive installer PATH >>>\nuser edit\n")
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service", "--repair"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 1
    assert "partial or duplicate" in rerun.stderr
    assert profile.read_text() == "user text\n# >>> hi-hive installer PATH >>>\nuser edit\n"


def test_modified_complete_profile_block_is_refused(tmp_path):
    result, home, _bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    profile = home / ".profile"
    profile.write_text(profile.read_text().replace("export PATH=", "echo changed; export PATH="))
    (home / ".hihive").mkdir()
    (home / ".hihive/config.json").write_text('{"harnesses":[]}\n')

    rerun = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service", "--repair"], env=env,
                           capture_output=True, text=True, timeout=30)

    assert rerun.returncode == 1
    assert "modified hi-hive PATH block" in rerun.stderr
    assert "echo changed" in profile.read_text()


def test_invalid_main_sha_stops_before_tool_or_profile_changes(tmp_path):
    result, home, bin_dir, log, _env = run_installer(tmp_path, invalid_sha=True)

    assert result.returncode == 1
    assert "invalid main commit SHA" in result.stderr
    assert not (bin_dir / "hihive").exists()
    assert not (home / ".profile").exists()
    assert "uv tool install" not in log.read_text()


def test_unsafe_user_writable_destinations_are_rejected_without_changes(tmp_path):
    for unsafe in ("home", "state", "tool-bin", "profile"):
        case = tmp_path / unsafe
        case.mkdir()
        home, bin_dir, log, env = installer_environment(case)
        if unsafe == "home":
            home.chmod(0o777)
        elif unsafe == "state":
            state = home / ".state/hi-hive"
            state.mkdir(parents=True)
            state.chmod(0o777)
        elif unsafe == "tool-bin":
            bin_dir.chmod(0o777)
        else:
            profile = home / ".profile"
            profile.write_text("KEEP\n")
            profile.chmod(0o666)

        result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                                capture_output=True, text=True, timeout=30)

        assert result.returncode == 1
        assert "group/world-writable" in result.stderr
        assert not (bin_dir / "hihive").exists()
        if unsafe == "profile":
            assert (home / ".profile").read_text() == "KEEP\n"
        if unsafe in ("home", "state"):
            assert not log.exists()
        elif unsafe == "tool-bin":
            assert "curl" not in log.read_text()


def test_install_record_symlink_is_rejected_without_changing_target(tmp_path):
    home, bin_dir, _log, env = installer_environment(tmp_path)
    state = home / ".state/hi-hive"
    state.mkdir(parents=True)
    victim = home / "victim"
    victim.write_text("KEEP\n")
    (state / "install.json").symlink_to(victim)

    result = subprocess.run(["dash", str(INSTALLER), "--plain", "--no-service"], env=env,
                            capture_output=True, text=True, timeout=30)

    assert result.returncode == 1
    assert "install record must not be a symbolic link" in result.stderr
    assert victim.read_text() == "KEEP\n"
    assert not (bin_dir / "hihive").exists()


def test_install_record_is_private_and_atomic_temp_is_removed(tmp_path):
    result, home, _bin_dir, _log, _env = run_installer(tmp_path)
    assert result.returncode == 0, result.stderr
    state = home / ".state/hi-hive"
    record = state / "install.json"

    assert record.stat().st_mode & 0o777 == 0o600
    assert list(state.glob(".install.json.*")) == []


def test_root_is_rejected_before_network_or_writes(tmp_path):
    result, home, _bin_dir, log, _env = run_installer(tmp_path, root=True)

    assert result.returncode == 1
    assert "do not run" in result.stderr
    assert not log.exists()
    assert not (home / ".profile").exists()


def test_uninstall_removes_only_owned_path_block_and_preserves_data(tmp_path):
    result, home, _bin_dir, _log, env = run_installer(tmp_path)
    assert result.returncode == 0
    profile = home / ".profile"
    profile.write_text("before\n" + profile.read_text() + "after\n")
    data = home / ".hihive/token"
    data.parent.mkdir(exist_ok=True)
    data.write_text("SECRET_TOKEN\n")

    removed = subprocess.run(["dash", str(INSTALLER), "--plain", "--uninstall"], env=env,
                             capture_output=True, text=True, timeout=30)

    assert removed.returncode == 0, removed.stderr
    assert profile.read_text() == "before\nafter\n"
    assert data.read_text() == "SECRET_TOKEN\n"
    assert "SECRET_TOKEN" not in removed.stdout + removed.stderr
