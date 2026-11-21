import json
from types import SimpleNamespace

import pytest

from hihive import paths
from hihive import setup
from hihive.pool import PoolManager


def fake_detection(monkeypatch, versions):
    calls = []
    monkeypatch.setattr(setup, "read_login_shell_env", lambda shell=None: {"PATH": "/login/bin", "HOME": "/home/test"})
    monkeypatch.setattr(setup.shutil, "which",
                        lambda command, path=None: f"/login/bin/{command}" if command in versions else None)

    def run(args, **kwargs):
        calls.append((args, kwargs))
        return SimpleNamespace(returncode=0, stdout=f"{args[0]} {versions[args[0].rsplit('/', 1)[-1]]}\n", stderr="")

    monkeypatch.setattr(setup.subprocess, "run", run)
    return calls


def test_detection_uses_login_environment_and_only_version_commands(monkeypatch):
    calls = fake_detection(monkeypatch, {"claude": "2.1.283", "pi": "0.87.1"})

    found = setup.detect_coding_agents()

    assert [item["status"] for item in found] == ["usable", "usable"]
    assert [call[0] for call in calls] == [["/login/bin/claude", "--version"], ["/login/bin/pi", "--version"]]
    assert all(call[1]["env"]["PATH"] == "/login/bin" for call in calls)


def test_login_shell_failure_does_not_probe_the_callers_path(monkeypatch):
    monkeypatch.setattr(setup, "read_login_shell_env", lambda shell=None: (_ for _ in ()).throw(RuntimeError("bad shell")))
    monkeypatch.setattr(setup.shutil, "which", lambda *_args, **_kwargs: pytest.fail("must not use caller PATH"))
    monkeypatch.setattr(setup.subprocess, "run", lambda *_args, **_kwargs: pytest.fail("must not run agents"))

    found = setup.detect_coding_agents()

    assert [item["status"] for item in found] == ["unavailable", "unavailable"]
    assert all(item["environment_error"] == "bad shell" for item in found)


def test_fresh_config_adds_only_supported_detected_agents_and_prefers_claude(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    fake_detection(monkeypatch, {"claude": "2.1.290", "pi": "0.87.3"})

    result = setup.configure_detected_agents("fresh")
    config = json.loads(paths.config_file().read_text())

    assert result["added"] == ["claude-code", "pi"]
    assert [item["type"] for item in config["harnesses"]] == ["claude-code", "pi"]
    assert config["default_harness"] == "claude-code"
    assert config["harnesses"][0]["command"] == "claude"


@pytest.mark.parametrize("versions, expected_default", [({}, None), ({"pi": "0.87.1"}, "pi")])
def test_fresh_zero_or_one_agent_is_valid_and_server_can_start(tmp_path, monkeypatch, versions, expected_default):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    fake_detection(monkeypatch, versions)

    setup.configure_detected_agents("fresh")
    config = json.loads(paths.config_file().read_text())
    manager = PoolManager(config_file=paths.config_file(), persist_file=paths.state_file())

    assert config["default_harness"] == expected_default
    assert len(config["harnesses"]) == len(versions)
    assert [item["type"] for item in manager.config()["harnesses"]] == [item["type"] for item in config["harnesses"]]


def test_too_old_or_unreadable_agent_is_not_configured(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    fake_detection(monkeypatch, {"claude": "2.1.282", "pi": "not-a-version"})

    result = setup.configure_detected_agents("fresh")

    assert [item["status"] for item in result["detected"]] == ["too_old", "unavailable"]
    assert json.loads(paths.config_file().read_text())["harnesses"] == []


def test_existing_config_is_unchanged_until_new_agent_is_explicitly_accepted(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    original = {"max_parallel": 7, "default_harness": "custom", "harnesses": [
        {"id": "custom", "name": "My Claude", "type": "claude-code", "command": "/custom/claude", "args": []}
    ]}
    paths.config_file().write_text(json.dumps(original, separators=(",", ":")) + "\n")
    before = paths.config_file().read_bytes()
    fake_detection(monkeypatch, {"claude": "2.1.290", "pi": "0.87.3"})

    preview = setup.configure_detected_agents("existing", accepted=[])
    assert paths.config_file().read_bytes() == before
    assert [item["id"] for item in setup.unconfigured_usable(preview)] == ["pi"]

    result = setup.configure_detected_agents("existing", accepted=["pi"])
    updated = json.loads(paths.config_file().read_text())
    assert result["added"] == ["pi"]
    assert updated["harnesses"][0] == original["harnesses"][0]
    assert updated["default_harness"] == "custom"
    assert [item["type"] for item in updated["harnesses"]] == ["claude-code", "pi"]


def test_custom_same_type_is_not_probed_or_offered(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    paths.config_file().write_text(json.dumps({"default_harness": "wrapped", "harnesses": [
        {"id": "wrapped", "name": "Claude in Docker", "type": "claude-code",
         "command": "/usr/local/bin/claude-docker", "args": []}
    ]}))
    calls = fake_detection(monkeypatch, {"claude": "2.1.290", "pi": "0.87.3"})

    result = setup.configure_detected_agents("existing", accepted=[])

    assert result["detected"][0]["status"] == "custom"
    assert [call[0] for call in calls] == [["/login/bin/pi", "--version"]]
    assert [item["id"] for item in setup.unconfigured_usable(result)] == ["pi"]


def test_first_agent_added_to_existing_zero_agent_config_becomes_default(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    paths.config_file().write_text('{"default_harness":null,"harnesses":[]}\n')
    fake_detection(monkeypatch, {"pi": "0.87.3"})

    result = setup.configure_detected_agents("existing", accepted=["pi"])
    config = json.loads(paths.config_file().read_text())

    assert result["added"] == ["pi"]
    assert config["default_harness"] == "pi"


def test_settings_can_remove_last_coding_agent(tmp_path):
    config = tmp_path / "config.json"
    config.write_text(json.dumps({"default_harness": "pi", "harnesses": [
        {"id": "pi", "name": "Pi", "type": "pi", "command": "pi", "args": []}
    ]}))
    manager = PoolManager(config_file=config, persist_file=tmp_path / "state.json")

    manager.update_config({"harnesses": []})

    assert manager.config()["harnesses"] == []
    assert all(project["default_harness"] is None for project in manager.config()["projects"])


def test_malformed_existing_config_is_not_replaced(tmp_path, monkeypatch):
    monkeypatch.setenv("HIVE_HOME", str(tmp_path))
    paths.config_file().parent.mkdir(parents=True, exist_ok=True)
    paths.config_file().write_text("not json\n")
    before = paths.config_file().read_bytes()
    fake_detection(monkeypatch, {})

    with pytest.raises(ValueError, match="Cannot read existing settings"):
        setup.configure_detected_agents("existing", accepted=[])

    assert paths.config_file().read_bytes() == before
