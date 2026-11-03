"""
Tests for the two-button todo.py against simulated hardware:
  1. the real Adafruit SSD1680B driver code, writing to a fake SPI bus
  2. button timing (debounce, press vs hold)
  3. the whole app driven by simulated presses of the two bonnet buttons
  4. the background renderer (newest screen wins, identical screens skipped)
  5. the Speech class against fake Moonshine + sounddevice modules
  6. the battery meter against a fake UPS HAT battery chip
usage: python tests/test_todo.py [APP_DIR]   (APP_DIR: the folder with todo.py;
       by default the one above tests/)
"""
import json
import sys
import tempfile
import threading
import time
import types
from pathlib import Path

HERE = Path(__file__).resolve().parent
APP_DIR = sys.argv[1] if len(sys.argv) > 1 else str(HERE.parent)
sys.path.insert(0, str(HERE / "shim"))
sys.path.insert(1, APP_DIR)

import busio  # noqa: E402
import digitalio  # noqa: E402
import moonshine_voice  # noqa: E402
import numpy as np  # noqa: E402
import sounddevice  # noqa: E402
import todo  # noqa: E402

FAILS = []


def check(name, ok, detail=""):
    print(("PASS  " if ok else "FAIL  ") + name + (f"   [{detail}]" if detail else ""))
    if not ok:
        FAILS.append(name)


tmp = Path(tempfile.mkdtemp())

# ---- 1. Real driver, fake SPI --------------------------------------------------
disp = todo.EinkDisplay()
epd = disp.epd
check("driver is Adafruit_SSD1680B", type(epd).__name__ == "Adafruit_SSD1680B")
check("drawing area is 250x122 after rotation", (epd.width, epd.height) == (250, 122))
sample = todo.TodoList(tmp / "sample.json")
sample.add("Call the dentist about moving Thursday's appointment")
sample.toggle()
img = todo.item_screen(sample)
busio.LOG.clear()
disp.show(img)
ram, cmd, cmds = {}, None, []
for is_data, data in busio.LOG:
    if not is_data:
        cmd = data[0]
        cmds.append(cmd)
    else:
        ram.setdefault(cmd, bytearray()).extend(data)
bw = ram.get(0x24, b"")
check("black/white RAM written, refresh triggered, panel put to sleep",
      len(bw) == 4000 and 0x20 in cmds and cmds[-1] == 0x10)
bits = np.unpackbits(np.frombuffer(bytes(bw), dtype=np.uint8)).reshape(250, 128)[:, :122]
panel_black, want = bits == 0, np.array(img) < 128
wrong = min(int((np.rot90(want, k) != panel_black).sum()) for k in (1, 3))
check("every pixel (including the corner labels) reached the panel RAM", wrong == 0,
      f"{wrong} wrong, {int(want.sum())} black pixels")

# ---- 2. Button timing ------------------------------------------------------------
state = {"down": False}
b = todo.Button("x", lambda: state["down"])


def run_button(timeline, until):
    events, t, i = [], 0.0, 0
    while t <= until:
        while i < len(timeline) and timeline[i][0] <= t:
            state["down"] = timeline[i][1]
            i += 1
        events += [e for e in b.poll(t)]
        t = round(t + 0.01, 2)
    return events


check("10 ms glitch ignored", run_button([(0.1, True), (0.11, False)], 0.5) == [])
check("short press -> down, press, up (up always last)",
      run_button([(0.1, True), (0.3, False)], 0.6) == ["down", "press", "up"])
check("long press -> down, hold, up",
      run_button([(0.1, True), (1.5, False)], 2.0) == ["down", "hold", "up"])

# ---- 3. The app with the two bonnet buttons ---------------------------------------
class FakeRenderer:
    def __init__(self):
        self.shown = []

    def show(self, image):
        self.shown.append(image)


class FakeSpeech:
    def __init__(self):
        self.ready = threading.Event()
        self.ready.set()
        self.error = None
        self.capturing, self.capture_error = False, None
        self.mic_ok = True
        self.text = "buy milk."
        self.contexts = []
        self.counts = {"begin": 0, "cancel": 0, "commit": 0, "stop": 0}

    def begin(self):
        self.counts["begin"] += 1
        if self.mic_ok:
            self.capturing, self.capture_error = True, None
        else:
            self.capturing, self.capture_error = False, "PortAudioError: no microphone"

    def cancel(self):
        self.counts["cancel"] += 1
        self.capturing = False

    def commit(self, context=""):
        assert self.capturing, "commit without an open microphone"
        self.counts["commit"] += 1
        self.contexts.append(context)

    def stop(self):
        assert self.capturing
        self.counts["stop"] += 1
        self.capturing = False
        time.sleep(0.05)
        return self.text


buttons = todo.gpio_buttons()
check("buttons are the bonnet's GPIO 6 (top) and 5 (bottom)",
      [(btn.name) for btn in buttons] == ["top", "bottom"] and
      (todo.UPPER_PIN, todo.LOWER_PIN) == (6, 5) and todo.LABEL_SIDE == 'left' and
      digitalio.PINS[5].pull == digitalio.Pull.UP and digitalio.PINS[6].pull == digitalio.Pull.UP)
check("nothing else claims a GPIO for buttons", 26 not in digitalio.PINS)
pins = {"top": digitalio.PINS[todo.UPPER_PIN], "bottom": digitalio.PINS[todo.LOWER_PIN]}

words = tmp / "words.txt"
words.write_text("Ximena\nTrader Joe's\n")
lst = todo.TodoList(tmp / "todo.json")
rend, speech = FakeRenderer(), FakeSpeech()
app = todo.App(lst, rend, speech, words_file=words)
app.redraw()
clock = {"t": 1000.0}


def step(seconds):
    end = clock["t"] + seconds
    while clock["t"] < end:
        for btn in buttons:
            for e in btn.poll(clock["t"]):
                app.handle(btn.name, e, clock["t"])
        app.tick(clock["t"])
        clock["t"] += 0.02
        time.sleep(0.0005)


def press(name, seconds=0.15, after=0.2):
    pins[name].value = False
    step(seconds)
    pins[name].value = True
    step(after)


def settle(timeout=2.0):
    deadline = time.monotonic() + timeout
    while app.state == "transcribing" and time.monotonic() < deadline:
        step(0.02)


def say(text, seconds=1.5):
    speech.text = text
    press("top", seconds, 0.1)
    settle()


def saved():
    return json.loads((tmp / "todo.json").read_text())


check("starts on the empty screen", rend.shown[-1].tobytes() == todo.item_screen(lst).tobytes())
press("top")
check("short press on an empty list: mic opened then dropped, Moonshine never ran",
      speech.counts == {"begin": 1, "cancel": 1, "commit": 0, "stop": 0}, str(speech.counts))

pins["top"].value = False
step(0.5)
check("holding top: still browsing before 0.8 s, but already capturing",
      app.state == "browse" and speech.capturing)
step(0.5)
check("past 0.8 s -> listening", app.state == "listening")
pins["top"].value = True
step(0.06)
settle()
check("let go -> transcript waits for confirmation",
      app.state == "confirm_add" and app.pending == "Buy milk", f"{app.state} {app.pending!r}")
check("confirm screen labels: top cancel/retry, bottom add",
      rend.shown[-1].tobytes() == todo.compose("Buy milk", **todo.CONFIRM_ADD).tobytes())
press("bottom")
check("bottom adds it", app.state == "browse" and saved()["items"] == [{"text": "Buy milk", "done": False}])

say("call Ximena about the party")
press("bottom")
check("second item added", [i["text"] for i in lst.items] == ["Buy milk", "Call Ximena about the party"])
check("speech got list + words as context",
      "Buy milk" in speech.contexts[-1] and "Trader Joe's" in speech.contexts[-1])

before = dict(speech.counts)
press("top")
check("top press -> next item (wraps to first)", lst.index == 0)
check("...and the short press never ran Moonshine",
      speech.counts["commit"] == before["commit"] and speech.counts["cancel"] == before["cancel"] + 1)
press("bottom")
check("bottom press crosses off", lst.items[0]["done"] and saved()["items"][0]["done"])
check("crossed-off screen shown with 'undo' label",
      rend.shown[-1].tobytes() == todo.item_screen(lst).tobytes())
press("bottom")
check("pressing again un-crosses", not lst.items[0]["done"])

press("bottom", 1.0)
check("holding bottom asks before deleting", app.state == "confirm_delete")
press("top")
check("top keeps it", app.state == "browse" and len(lst.items) == 2)
press("bottom", 1.0)
press("bottom")
check("bottom confirms the delete",
      [i["text"] for i in lst.items] == ["Call Ximena about the party"])

say("pick up stamps")
press("top")
check("top press on the confirm screen cancels", app.state == "browse" and len(lst.items) == 1)

say("water the plants")
check("confirm screen shown", app.state == "confirm_add" and app.pending == "Water the plants")
say("water the ferns")
check("holding top on the confirm screen records again", app.pending == "Water the ferns")
press("bottom")
check("...and the new version is added", lst.items[-1]["text"] == "Water the ferns")

check("list has several items for the next test", len(lst.items) >= 2)
say("   ")
check("silence -> didn't catch that", app.state == "message" and app.message[0] == "Didn't catch that")
idx, begins = lst.index, speech.counts["begin"]
press("top")
check("a press only closes the message", app.state == "browse" and lst.index == idx and
      speech.counts["begin"] == begins, f"state={app.state} index {idx}->{lst.index} begins {begins}->{speech.counts['begin']}")

speech.text = "buy batteries"
pins["top"].value = False
step(todo.MAX_TALK_SECONDS + 0.5)
check("recording stops by itself at the time limit", app.state in ("transcribing", "confirm_add"))
pins["top"].value = True
settle()
check("...and still offers the transcript", app.state == "confirm_add" and app.pending == "Buy batteries")
press("top")

speech.ready.clear()
press("top", 1.0)
check("model still loading -> message stays after letting go",
      app.state == "message" and app.message[0] == "Still loading", str(app.message))
step(3.2)
check("loading message clears itself", app.state == "browse")
idx = lst.index
press("top")
check("while loading, top still moves through the list", lst.index == (idx + 1) % len(lst.items))
speech.error = "OSError: no model"
press("top", 1.0)
check("model failed -> message with the reason",
      app.message[0] == "Speech isn't working" and "no model" in app.message[1])
step(6.5)
speech.ready.set()
speech.error = None

speech.mic_ok = False
idx = lst.index
press("top")
check("no microphone: top still moves through the list", lst.index == (idx + 1) % len(lst.items))
press("top", 1.0)
check("no microphone: holding explains why", app.message[0] == "Microphone problem" and
      "no microphone" in app.message[1], str(app.message))
step(6.5)
speech.mic_ok = True

bad = tmp / "broken.json"
bad.write_text("{not json")
check("a corrupt list file is kept aside, not crashing",
      todo.TodoList(bad).items == [] and (tmp / "broken.json.bad").exists())

src = Path(APP_DIR, "todo.py").read_text().replace("ROTATION = 1 ", "ROTATION = 3 ", 1)
flipped = types.ModuleType("flipped")
exec(compile(src, "todo.py", "exec"), flipped.__dict__)
check("ROTATION = 3: buttons swap and labels move to the right side",
      (flipped.UPPER_PIN, flipped.LOWER_PIN) == (5, 6) and flipped.LABEL_SIDE == "right")


def corners(image):
    """Is each corner pixel black (a label tab sits there)?"""
    px = image.load()
    w, h = image.size
    return {"top-left": px[2, 2] < 128, "top-right": px[w - 3, 2] < 128,
            "bottom-left": px[2, h - 3] < 128, "bottom-right": px[w - 3, h - 3] < 128}


sample2 = todo.TodoList(tmp / "corners.json")
sample2.add("Buy milk")
c = corners(todo.item_screen(sample2))
check("labels drawn in the top-left and bottom-left corners, next to the buttons",
      c == {"top-left": True, "top-right": False, "bottom-left": True, "bottom-right": False}, str(c))
c = corners(flipped.item_screen(flipped.TodoList(tmp / "corners.json")))
check("upside down: labels drawn in the top-right and bottom-right corners",
      c == {"top-left": False, "top-right": True, "bottom-left": False, "bottom-right": True}, str(c))
c = corners(todo.compose("Pick up the dry cleaning", **todo.CONFIRM_ADD))
check("confirm screen: both labels on the left too", c["top-left"] and c["bottom-left"] and
      not (c["top-right"] or c["bottom-right"]), str(c))

# ---- 4. Renderer -------------------------------------------------------------------
class SlowDisplay:
    def __init__(self):
        self.count = 0

    def show(self, image):
        self.count += 1
        time.sleep(0.2)


slow = SlowDisplay()
r = todo.Renderer(slow)
r.start()
frames = []
for n in range(5):
    image, draw = todo.new_canvas()
    draw.text((10, 10), str(n), fill=0)
    frames.append(image)
    r.show(image)
    time.sleep(0.01)
r.idle.wait(3)
check("5 quick screens -> 2 refreshes (first + newest)", slow.count == 2, f"{slow.count}")
r.show(frames[-1])
time.sleep(0.05)
r.idle.wait(3)
check("same screen again -> no refresh", slow.count == 2)

# ---- 5. Speech class with fake Moonshine + sounddevice --------------------------------
moonshine_voice.CALLS.clear()
sp = todo.Speech()
check("model loads in the background", sp.ready.wait(3) and sp.error is None, str(sp.error))
check("tiny streaming model requested",
      ("get_model", "en", moonshine_voice.ModelArch.TINY_STREAMING) in moonshine_voice.CALLS)
check("USB microphone picked over HDMI", sp._device == 1)

moonshine_voice.NEXT_TEXT[:] = ["Buy milk", "and eggs."]
sp.begin()
time.sleep(0.4)            # the 0.8 s hold wait, shortened
sp.commit("Buy milk Trader Joe's")
time.sleep(0.4)
text = sp.stop()
stop_call = [c for c in moonshine_voice.CALLS if c[0] == "stop"][-1]
check("stereo-only mic: retried with 2 channels", sounddevice.OPENED[-1].channels == 2)
check("audio from before the hold was kept and sent to Moonshine",
      stop_call[2] == (48000,) and 0.65 * 48000 <= stop_call[1] <= 1.0 * 48000,
      f"{stop_call[1] / 48000:.2f} s of audio")
check("lines joined into one item", text == "Buy milk and eggs.", repr(text))
check("context passed to Moonshine", ("context", "Buy milk Trader Joe's") in moonshine_voice.CALLS)

starts = sum(1 for c in moonshine_voice.CALLS if c[0] == "start")
sp.begin()
time.sleep(0.2)
sp.cancel()
check("cancel: mic closed, Moonshine never started",
      sounddevice.OPENED[-1].closed and not sp.capturing and
      sum(1 for c in moonshine_voice.CALLS if c[0] == "start") == starts)
sp.begin()
time.sleep(0.15)
sp.commit()
time.sleep(0.15)
check("records fine after a cancel", sp.stop() == "Buy milk and eggs.")
stop_call = [c for c in moonshine_voice.CALLS if c[0] == "stop"][-1]
check("...without the cancelled audio", stop_call[1] <= 0.5 * 48000, f"{stop_call[1] / 48000:.2f} s")

sounddevice.NO_MIC = True
sp.begin()
check("no microphone: begin() reports why instead of crashing",
      not sp.capturing and "PortAudioError" in (sp.capture_error or ""), str(sp.capture_error))
sounddevice.NO_MIC = False
check("clean() tidies the text", todo.clean("  buy  milk. ") == "Buy milk")


def record(seconds=0.5):
    sp.begin()
    time.sleep(0.2)
    sp.commit()
    time.sleep(seconds)
    sp.stop()
    return [c for c in moonshine_voice.CALLS if c[0] == "stop"][-1]


sounddevice.AMPLITUDE = 0.01
call = record()
check("quiet mic (peaks 0.01) boosted to about 0.3", 0.25 <= call[3] <= 0.31, f"peak {call[3]}")
sounddevice.AMPLITUDE = 0.5
call = record()
check("a healthy level (0.5) is left alone", 0.49 <= call[3] <= 0.501, f"peak {call[3]}")

# I2S microphone on the googlevoicehat overlay: no USB mic, ALSA's virtual devices
# listed first, stereo-only card, mic on the left (SEL to ground) or right channel.
sounddevice.DEVICE_SET = "i2s"
sounddevice.AMPLITUDE = 0.008
sp2 = todo.Speech()
sp2.ready.wait(3)
check("I2S: the voiceHAT card is picked, not 'sysdefault' or 'default'", sp2._device == 2,
      f"device {sp2._device}")
sp = sp2
for side, label in ((0, "left (SEL to GND)"), (1, "right (SEL to 3.3V)")):
    sounddevice.CHANNEL = side
    call = record()
    check(f"I2S mic on the {label} channel is heard and boosted", 0.2 <= call[3] <= 0.31 and
          call[2] == (48000,), f"peak {call[3]} at {call[2]}")
sounddevice.DEVICE_SET, sounddevice.CHANNEL, sounddevice.AMPLITUDE = "usb", 0, 0.01

# ---- 6. Battery meter (Waveshare UPS HAT (C)) -------------------------------------------
import fcntl  # noqa: E402
import os  # noqa: E402


class FakeSensor:
    """Stands in for the INA219: what it would measure for a given battery voltage
    and current (negative = the battery is powering the Pi)."""

    def __init__(self, volts=3.95, amps=-0.168, ohms=0.1):
        self.volts, self.amps, self.ohms, self.fail = volts, amps, ohms, False

    def read(self):
        if self.fail:
            raise OSError(121, "Remote I/O error")
        return self.volts, self.amps * self.ohms


def samples(battery, n):
    """Let n readings happen, each when it's due."""
    for _ in range(n):
        battery.poll(battery.next_sample)


pv = todo.percent_from_volts
check("voltage -> level: full, flat, halfway, in between",
      pv(4.2) == 100 and pv(3.2) == 0 and pv(3.79) == 50 and 50 < pv(3.81) < 60,
      f"{pv(4.2)} {pv(3.2)} {pv(3.79)} {pv(3.81)}")
levels = [pv(3.0 + i * 0.005) for i in range(260)]
check("level never drops as the voltage rises", all(a <= b for a, b in zip(levels, levels[1:])))

# The INA219 class against a fake /dev/i2c-1
calls, regs, pointer = [], {0x01: 0x10000 - 1680, 0x02: 988 << 3 | 0b10}, [None]
real = (os.open, os.write, os.read, fcntl.ioctl, fcntl.flock)


def fake_write(fd, data):
    calls.append(("write", bytes(data)))
    pointer[0] = data[0]
    return len(data)


def fake_read(fd, n):
    calls.append(("read", n))
    value = regs[pointer[0]]
    return bytes([value >> 8, value & 0xFF])


os.open = lambda path, flags: calls.append(("open", path)) or 99
fcntl.ioctl = lambda fd, request, arg: calls.append(("ioctl", fd, request, arg))
fcntl.flock = lambda fd, op: calls.append(("lock" if op == fcntl.LOCK_EX else "unlock",))
os.write, os.read = fake_write, fake_read
try:
    chip = todo.INA219()
    volts, shunt = chip.read()
finally:
    os.open, os.write, os.read, fcntl.ioctl, fcntl.flock = real
check("INA219: opens /dev/i2c-1, picks address 0x43, sets 128-sample averaging",
      calls[:3] == [("open", "/dev/i2c-1"), ("ioctl", 99, 0x0703, 0x43), ("write", b"\x00\x1f\xff")],
      str(calls[:3]))
check("INA219: reads the shunt (signed) and bus voltage registers",
      abs(volts - 3.952) < 1e-9 and abs(shunt + 0.0168) < 1e-9 and
      ("write", b"\x01") in calls and ("write", b"\x02") in calls, f"{volts} {shunt}")
check("INA219: each register read is locked against a second copy of the program",
      calls[3:] == [("lock",), ("write", b"\x01"), ("read", 2), ("unlock",),
                    ("lock",), ("write", b"\x02"), ("read", 2), ("unlock",)], str(calls[3:]))

sensor = FakeSensor()
bat = todo.Battery(sensor, 0.0)
r = bat.reading
check("first reading straight away: on battery, level, power, time left",
      r.status == "battery" and r.percent == pv(3.95) == 80 and
      abs(r.watts - 3.95 * 0.168) < 0.005 and 4.0 < r.hours < 5.0, str(r))
samples(bat, 12)
check("a minute on battery settles the resistor as 0.1 ohm, as Waveshare says", bat.shunt == 0.1)
b01 = todo.Battery(FakeSensor(ohms=0.01), 0.0)
samples(b01, 12)
check("...and spots a 0.01 ohm one from readings ten times too small",
      b01.shunt == 0.01 and abs(b01.reading.watts - 3.95 * 0.168) < 0.005, f"{b01.shunt} {b01.reading}")

sensor.volts = 3.80   # a burst of hard work pulls the voltage down for a few seconds
samples(bat, 2)
sensor.volts = 3.95
samples(bat, 2)
check("a short burst of work doesn't move the level", bat.reading.percent == 80, str(bat.reading))

sensor.amps, sensor.volts = 0.45, 4.05
samples(bat, 2)
before = bat.reading.status
samples(bat, 1)
check("plugging in: ignored for two readings, then 'charging'",
      before == "battery" and bat.reading.status == "charging" and bat.reading.percent == pv(4.05),
      f"{before} -> {bat.reading}")
sensor.amps, sensor.volts = 0.2, 4.20
samples(bat, 13)
check("charging: never says 100% until it's done", bat.reading.percent == 99, str(bat.reading))
sensor.amps, sensor.volts = 0.0, 4.18
samples(bat, 13)
check("charge finished (no current): 'plugged in', 100%",
      bat.reading.status == "plugged" and bat.reading.percent == 100, str(bat.reading))
sensor.amps, sensor.volts = -0.168, 3.95
samples(bat, 2)
before = bat.reading.status
samples(bat, 1)
check("unplugging: on battery after three readings, with no old charging readings mixed in",
      before == "plugged" and bat.reading.status == "battery" and bat.reading.percent == 80,
      f"{before} -> {bat.reading}")

sensor.fail = True
samples(bat, 12)
kept = bat.reading
samples(bat, 13)
check("chip stops answering: last level kept for 2 minutes, then hidden",
      kept is not None and bat.reading is None)
sensor.fail = False
samples(bat, 1)
check("...and back as soon as it answers", bat.reading is not None and bat.reading.status == "battery")
sensor.volts = 0.0
samples(bat, 1)
check("a nonsense voltage counts as a failed reading", bat.failing and bat.reading.percent == 80)
sensor.volts = 3.95
samples(bat, 1)
check("time left reads naturally", todo.duration(4.83) == "4 h 50 min" and
      todo.duration(0.4) == "25 min" and todo.duration(2.0) == "2 h", todo.duration(4.83))


def item_args(lst):
    item = lst.items[lst.index]
    left = sum(not i["done"] for i in lst.items)
    return dict(strike=item["done"], upper="next · hold: add",
                lower=("undo" if item["done"] else "done") + " · hold: delete",
                note_bottom="done" if item["done"] else f"{left} left", checked=item["done"])


def top_gap(image):
    """White columns between the top button label and whatever is right of it."""
    px = image.load()
    inked = [any(px[x, y] < 128 for y in range(todo.BAND)) for x in range(todo.W)]
    x = 0
    while x < todo.W and inked[x]:
        x += 1
    start = x
    while x < todo.W and not inked[x]:
        x += 1
    return x - start


long_list = todo.TodoList(tmp / "long.json")
for n in range(120):
    long_list.add(f"Item {n + 1}")
gaps, all_items = {}, list(long_list.items)
for count, index in ((3, 1), (15, 11), (120, 99)):
    long_list.items = all_items[:count]
    long_list.index = index
    for badge in ((5, False), (82, False), (100, False), (100, True)):
        gaps[(count, index + 1, badge)] = top_gap(todo.item_screen(long_list, badge))
check("battery and '2 of 3' never run into the button label", min(gaps.values()) >= 6,
      str(min(gaps.items(), key=lambda kv: kv[1])))
long_list.items, long_list.index = long_list.items[:15], 11
check("when '12 of 15' won't fit next to the battery it becomes '12/15'",
      todo.item_screen(long_list, (82, False)).tobytes() ==
      todo.compose("Item 12", note_top="12/15", badge=(82, False), **item_args(long_list)).tobytes())
img = flipped.item_screen(flipped.TodoList(tmp / "corners.json"), (82, False))
px = img.load()
check("upside down: the battery goes in the top-left corner, labels stay right",
      any(px[x, y] < 128 for x in range(6, 70) for y in range(3, 17)) and px[todo.W - 3, 2] < 128)

# The app with a battery
lst3 = todo.TodoList(tmp / "battery-app.json")
for text in ("Buy milk", "Call mom", "Pay rent"):
    lst3.add(text)
lst3.index = 0
sensor = FakeSensor()
bat = todo.Battery(sensor, clock["t"])
rend, speech = FakeRenderer(), FakeSpeech()
app = todo.App(lst3, rend, speech, words_file=words, battery=bat)
app.now = clock["t"]
app.redraw()
check("item screen shows the battery level",
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (80, False)).tobytes())


def both(first, second, gap=0.1, hold=0.3):
    pins[first].value = False
    step(gap)
    pins[second].value = False
    step(hold)
    pins[first].value = True
    pins[second].value = True
    step(0.2)


def untouched():
    return lst3.index == 0 and len(lst3.items) == 3 and not any(i["done"] for i in lst3.items)


cancels = speech.counts["cancel"]
both("top", "bottom")
check("both buttons (top first) -> battery screen; the mic the top one opened is closed",
      app.state == "battery" and speech.counts["cancel"] == cancels + 1 and
      speech.counts["commit"] == 0 and
      rend.shown[-1].tobytes() == todo.battery_screen(bat.reading).tobytes(), app.state)
check("...and neither button did its usual job", untouched())
press("top")
check("a press closes the battery screen without moving on", app.state == "browse" and untouched())
both("bottom", "top")
check("both buttons (bottom first) -> battery screen, nothing crossed off or deleted",
      app.state == "battery" and untouched())
press("bottom")
check("a bottom press closes it without crossing off", app.state == "browse" and untouched())
both("top", "bottom")
both("bottom", "top", gap=0.05)
check("pressing both again just closes it", app.state == "browse" and untouched() and not app.held,
      app.state)
both("top", "bottom")
step(todo.BATTERY_SCREEN_SECONDS + 0.5)
check("the battery screen goes back to the list by itself", app.state == "browse")
press("top")
check("buttons work normally afterwards", lst3.index == 1)
lst3.index = 0

real_app = app
app = todo.App(lst3, FakeRenderer(), FakeSpeech(), words_file=words)
both("top", "bottom")
check("no UPS HAT: both buttons explain instead",
      app.state == "message" and app.message[0] == "No battery reading" and untouched(),
      str(app.message))
app = real_app

shown = len(rend.shown)
sensor.volts = 3.93                       # 80% -> 77%
step(90)
check("a small drop doesn't flash the screen", len(rend.shown) == shown,
      f"{len(rend.shown) - shown} redraws")
sensor.volts = 3.88                       # 70%
step(90)
check("a 10-point drop redraws it once", len(rend.shown) == shown + 1 and
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (70, False)).tobytes(),
      f"{len(rend.shown) - shown} redraws")
sensor.amps, sensor.volts = 0.45, 4.05    # plugged in
step(90)
check("plugging in redraws with the charging bolt",
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (91, True)).tobytes())
sensor.amps, sensor.volts = 0.2, 4.20
step(90)
sensor.amps = 0.0                         # charging finished
step(90)
check("finishing charging shows 100%",
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (100, True)).tobytes())
sensor.amps, sensor.volts = -0.168, 3.70  # unplugged at 20%
step(90)
shown = len(rend.shown)
sensor.volts = 3.68                       # 15%
step(90)
check("below 20%, every 5 points redraws (so a flat battery leaves an honest screen)",
      len(rend.shown) == shown + 1 and
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (15, False)).tobytes(),
      f"{len(rend.shown) - shown} redraws")
press("bottom", 1.0)
shown = len(rend.shown)
sensor.volts = 3.95
step(90)
check("no battery redraws while another screen is up",
      app.state == "confirm_delete" and len(rend.shown) == shown)
press("top")
check("back on the list, the level is up to date",
      rend.shown[-1].tobytes() == todo.item_screen(lst3, (80, False)).tobytes())

print()
print(f"{'ALL PASSED' if not FAILS else str(len(FAILS)) + ' FAILED: ' + ', '.join(FAILS)}")
sys.exit(1 if FAILS else 0)
