"""Owns and reaps one harness process.

Usage: python -I guardian.py '<JSON argv>'

The stdin pipe is also the owner's liveness lease. Even SIGKILL of Hive closes
it. Own and reap the harness here so a server crash cannot leave it running.
This file uses only the standard library and is run as a plain script.
"""

import json
import os
import select
import signal
import subprocess
import sys
import threading
import time

BUFFER_LIMIT = 1_048_576


def main() -> int:
    argv = json.loads(sys.argv[1])
    child = subprocess.Popen(argv, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                             process_group=0, bufsize=0)
    writer = child.stdin.fileno()
    os.set_blocking(writer, False)
    state = {"stopping": False, "relay_failed": False}

    def stop(_signum, _frame):
        state["stopping"] = True

    for name in ("SIGTERM", "SIGINT", "SIGHUP"):
        signal.signal(getattr(signal, name), stop)

    # Separate relays preserve stdout EOF even if the child remains alive after
    # closing its protocol output. No diagnostics are mixed into protocol stdout.
    def relay(source, target):
        try:
            while chunk := os.read(source.fileno(), 65_536):
                view = memoryview(chunk)
                while view:
                    view = view[os.write(target, view):]
        except OSError as error:
            state["relay_failed"] = True
            state["stopping"] = True
            if target == 1:
                try:
                    os.write(2, f"HIVE_GUARDIAN_RELAY_FAILURE stdout {type(error).__name__}\n".encode())
                except OSError:
                    pass
        finally:
            source.close()
            # Replace the descriptor so Hive receives EOF while we still reap.
            try:
                null = os.open(os.devnull, os.O_WRONLY)
                os.dup2(null, target)
                os.close(null)
            except OSError:
                pass

    relays = [threading.Thread(target=relay, args=(child.stdout, 1), daemon=True),
              threading.Thread(target=relay, args=(child.stderr, 2), daemon=True)]
    for thread in relays:
        thread.start()

    status = None
    buffer = b""
    try:
        while not state["stopping"]:
            status = child.poll()
            if status is not None:
                break
            readers = [0] if len(buffer) < BUFFER_LIMIT else []
            writers = [writer] if buffer else []
            ready_read, ready_write, _ = select.select(readers, writers, [], 0.1)
            if 0 in ready_read:
                chunk = os.read(0, 65_536)
                if not chunk:
                    break
                buffer += chunk
            if writer in ready_write:
                try:
                    buffer = buffer[os.write(writer, buffer):]
                except BlockingIOError:
                    pass
    except OSError:
        # The harness closed stdin, or our owner closed its pipe.
        pass
    finally:
        try:
            child.stdin.close()
        except OSError:
            pass
        # EOF lets the harness dispose its runtime (including tool-specific
        # process groups) before escalation. A stuck harness has a deadline.
        status = _wait(child, 0.25, 0.01)
        if status is None:
            _kill_group(child.pid, signal.SIGTERM)
            status = _wait(child, 2, 0.02)
        # Kill surviving tools too, even if the harness exited first.
        _kill_group(child.pid, signal.SIGKILL)
        if status is None:
            status = child.wait()
        for thread in relays:
            thread.join(2)
            if thread.is_alive():
                state["relay_failed"] = True
    if state["relay_failed"]:
        return 74
    return status if status >= 0 else 1


def _wait(child, seconds, step):
    deadline = time.monotonic() + seconds
    status = child.poll()
    while status is None and time.monotonic() < deadline:
        time.sleep(step)
        status = child.poll()
    return status


def _kill_group(pid, sig):
    try:
        os.killpg(pid, sig)
    except (ProcessLookupError, PermissionError):
        pass


if __name__ == "__main__":
    sys.exit(main())
