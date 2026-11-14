"""The home-folder lock and the CLI's client config."""

import pytest

from hihive import paths


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delenv("HIVE_HOME", raising=False)
    return tmp_path


def test_second_server_on_same_home_is_refused(home):
    first = paths.lock_home(4567)
    try:
        with pytest.raises(SystemExit, match="port 4567"):
            paths.lock_home(4600)
    finally:
        first.close()


def test_lock_is_free_again_after_the_server_stops(home):
    paths.lock_home(4567).close()
    paths.lock_home(4600).close()



def test_cli_finds_the_port_the_server_recorded(home):
    lock = paths.lock_home(4242)
    try:
        assert paths.server_url() == "http://localhost:4242"
    finally:
        lock.close()


def test_cli_uses_the_default_port_before_any_server_ran(home):
    assert paths.server_url() == f"http://localhost:{paths.DEFAULT_PORT}"


def test_each_home_has_its_own_server(home, monkeypatch):
    paths.lock_home(4242).close()
    monkeypatch.setenv("HIVE_HOME", str(home / "other"))
    paths.lock_home(4343).close()

    assert paths.server_url() == "http://localhost:4343"
