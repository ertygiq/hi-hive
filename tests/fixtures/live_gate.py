import pathlib
import sys
import time

root = pathlib.Path(sys.argv[1])
(root / 'started').write_text('ready')
deadline = time.monotonic() + 45
while not (root / 'release').exists():
    if time.monotonic() >= deadline:
        raise SystemExit('Gate timed out')
    time.sleep(0.05)
print('Gate released. Apply all newer instructions before answering.')
