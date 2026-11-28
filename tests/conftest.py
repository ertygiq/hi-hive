import os
import shutil
import tempfile
import time
from pathlib import Path

import pytest

TESTS = Path(__file__).resolve().parent
ROOT = TESTS.parent
FIXTURE = str(TESTS / "fixtures" / "fake_harness.py")


@pytest.fixture
def mock_native(monkeypatch):
    import sys
    from hihive import session_import
    path = os.path.realpath(sys.executable)
    info = os.stat(path)
    monkeypatch.setattr(session_import, "executable_identity_matches", lambda source: True)
    monkeypatch.setattr(session_import, "native_executable", lambda harness: {
        "executable": path, "executable_device": info.st_dev, "executable_inode": info.st_ino,
        "executable_size": info.st_size, "executable_mtime_ns": info.st_mtime_ns,
        "executable_ctime_ns": info.st_ctime_ns})


@pytest.fixture
def test_dir():
    """A scratch directory under the ignored .test-tmp folder."""
    base = ROOT / ".test-tmp"
    base.mkdir(exist_ok=True)
    path = tempfile.mkdtemp(prefix="hive-", dir=base)
    yield path
    shutil.rmtree(path, ignore_errors=True)


def wait_until(condition, timeout=10, describe=lambda: ""):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError(f"Timed out: {describe()}")
        time.sleep(0.01)


def touch(directory, name):
    with open(os.path.join(directory, name), "w") as file:
        file.write("yes")


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
