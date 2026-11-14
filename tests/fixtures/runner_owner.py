"""Starts a runner and then waits, so a test can SIGKILL the owning process."""

import os
import sys
import time
import uuid

from hihive.protocol import PiRpcProtocol
from hihive.runner import LiveRunner

root = sys.argv[1]
fixture = os.path.join(os.path.dirname(os.path.abspath(__file__)), "fake_harness.py")
runner = LiveRunner(protocol=PiRpcProtocol(), command=sys.executable,
                    args=[fixture, "--fake-kind", "pi", "--fake-root", root],
                    session_id=str(uuid.uuid4()), chdir=root)
runner.submit("BASE")
runner.start()
while runner.pid is None:
    time.sleep(0.01)
with open(os.path.join(root, "guardian"), "w") as file:
    file.write(str(runner.pid))
while True:
    time.sleep(60)
