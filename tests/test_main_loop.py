"""Run todo.run() itself (main loop, renderer thread, real driver code, fake pins and
speech), press buttons in real time from another thread, then stop it with SIGTERM
the way systemd would. usage: python tests/test_main_loop.py [APP_DIR]"""
import json
import os
import signal
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE / "shim"))
sys.path.insert(1, sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent))

import busio  # noqa: E402
import digitalio  # noqa: E402
import moonshine_voice  # noqa: E402
import todo  # noqa: E402

tmp = Path(tempfile.mkdtemp())
todo.LIST_FILE = tmp / "todo.json"
todo.WORDS_FILE = tmp / "none.txt"
moonshine_voice.NEXT_TEXT[:] = ["buy oat milk."]
log = []


class FakeINA219:
    """A UPS HAT on battery: 3.95 V, 168 mA through its 0.1 ohm resistor."""

    def read(self):
        return 3.95, -0.0168


todo.INA219 = FakeINA219


def pin(n):
    while n not in digitalio.PINS:
        time.sleep(0.05)
    return digitalio.PINS[n]


def hold(n, seconds):
    pin(n).value = False
    time.sleep(seconds)
    pin(n).value = True


def presser():
    time.sleep(1.5)                         # startup + model "load"
    refreshes_before = sum(1 for d, b in busio.LOG if not d and b[0] == 0x20)
    hold(todo.UPPER_PIN, 1.6)               # hold the top button and say something
    time.sleep(2.0)                         # transcribe + show the confirm screen
    hold(todo.LOWER_PIN, 0.2)               # bottom button: add it
    time.sleep(1.0)
    hold(todo.LOWER_PIN, 0.2)               # bottom button: cross it off
    time.sleep(1.0)
    pin(todo.UPPER_PIN).value = False       # both buttons together: battery screen
    time.sleep(0.1)
    pin(todo.LOWER_PIN).value = False
    time.sleep(0.3)
    pin(todo.UPPER_PIN).value = True
    pin(todo.LOWER_PIN).value = True
    time.sleep(1.0)
    hold(todo.LOWER_PIN, 0.2)               # closes it, without un-crossing the item
    time.sleep(1.0)
    log.append(json.loads(todo.LIST_FILE.read_text()))
    log.append(sum(1 for d, b in busio.LOG if not d and b[0] == 0x20) - refreshes_before)
    os.kill(os.getpid(), signal.SIGTERM)    # like `systemctl stop`


threading.Thread(target=presser, daemon=True).start()
t0 = time.monotonic()
try:
    todo.run()
except SystemExit as e:
    print(f"run() exited cleanly with code {e.code} after {time.monotonic() - t0:.1f}s")

saved, refreshes = log
ok = saved["items"] == [{"text": "Buy oat milk", "done": True}]
print("saved list:", saved)
print("screen refreshes during the test:", refreshes)
print("PASS" if ok else "FAIL")
sys.exit(0 if ok else 1)
