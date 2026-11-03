#!/usr/bin/env python3
"""
Voice to-do list for a Raspberry Pi Zero 2 W with Adafruit's 2.13" monochrome
e-ink bonnet (product 4687) and a microphone: an I2S MEMS mic on GPIO 18-20, or
a USB mic. It uses only the bonnet's two buttons, which sit one above the other
at the left edge of the screen; their current jobs are shown in the screen
corners next to them.

    top button (GPIO 6)       press: next item       hold: add an item by voice
    bottom button (GPIO 5)    press: cross off/undo  hold: delete (asks first)

To add an item, hold the top button, say it, and let go. Then the bottom
button adds what it heard, and the top one cancels (or hold it to try again).

With a Waveshare UPS HAT (C) underneath, the battery level shows in the top
corner. Press both buttons together for the battery screen: level, power use
and time left.

Speech becomes text on the Pi itself with Moonshine; after the first run has
downloaded the model, no internet is needed. The list is saved in ~/todo.json.

    python todo.py                  run the device
    python todo.py --download       fetch the speech model once (needs internet)
    python todo.py --battery        print battery readings every 2 seconds
    python todo.py --preview DIR    draw every screen to PNG files (no hardware needed)
"""

import argparse
import collections
import functools
import importlib
import json
import os
import queue
import signal
import statistics
import sys
import threading
import time
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont

# Blinka offers to install its PWM and NeoPixel extras at startup; this app uses neither.
os.environ.setdefault("BLINKA_SKIP_DEPENDENCY_CHECK", "1")

# ---- Settings ---------------------------------------------------------------
LIST_FILE = Path.home() / "todo.json"
WORDS_FILE = Path.home() / "todo_words.txt"  # optional: names and places you say often

HOLD_SECONDS = 0.8    # a press longer than this is a "hold"
MIC_DEVICE = None     # None: a USB mic if there is one, otherwise the first other
                      # microphone (such as an I2S mic). Or a name/number from
                      # `python -m sounddevice`
AUTO_GAIN = True      # boost quiet microphones; I2S MEMS mics are very quiet
MAX_GAIN = 30         # most it will boost by
MAX_TALK_SECONDS = 15

# Battery meter for a Waveshare UPS HAT (C). Needs I2C: sudo raspi-config nonint do_i2c 0
BATTERY_ADDRESS = 0x43  # I2C address of the HAT's INA219 battery-monitor chip
BATTERY_MAH = 1000      # battery size, for the time-left estimate
SHUNT_OHMS = None       # the chip's current-sense resistor. None works it out (Waveshare
                        # says 0.1 but its demo code uses 0.01); or set 0.1 or 0.01
BATTERY_SCREEN_SECONDS = 30  # the battery screen goes back to the list after this

# Boards sold since August 2024 have the GDEY0213B74 panel and need this driver.
DISPLAY_DRIVER = "adafruit_epd.ssd1680b.Adafruit_SSD1680B"
# Older bonnets: "adafruit_epd.ssd1680_legacy.Adafruit_SSD1680_Legacy" (one ribbon
# tab, 2021 to mid-2024) or "adafruit_epd.ssd1675.Adafruit_SSD1675" (two tabs).
ROTATION = 1          # 3 turns the picture upside down; the button labels follow

# The buttons sit one above the other along one side of the picture: the left
# side with GPIO 6 on top normally, the right side with GPIO 5 on top when upside
# down. If the labels describe the wrong buttons, swap the 5s and 6s here.
UPPER_PIN, LOWER_PIN = (6, 5) if ROTATION == 1 else (5, 6)
LABEL_SIDE = "left" if ROTATION == 1 else "right"

# ---- Drawing ----------------------------------------------------------------
W, H = 250, 122
BAND = 19             # height of the label strips along the top and bottom
FONT_DIR = Path("/usr/share/fonts/truetype/dejavu")
BODY_SIZES = (28, 24, 20, 17, 14, 12)


@functools.lru_cache(maxsize=None)
def font(size, bold=False):
    path = FONT_DIR / ("DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf")
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        return ImageFont.load_default(size)


def new_canvas():
    image = Image.new("L", (W, H), 255)
    draw = ImageDraw.Draw(image)
    draw.fontmode = "1"  # crisp unsmoothed text for a black-and-white screen
    return image, draw


def wrap(draw, text, fnt, max_width):
    """Split text into lines that fit max_width, breaking very long words."""
    lines, line = [], ""
    for word in text.split():
        while draw.textlength(word, font=fnt) > max_width:
            cut = len(word) - 1
            while cut > 1 and draw.textlength(word[:cut], font=fnt) > max_width:
                cut -= 1
            if line:
                lines.append(line)
                line = ""
            lines.append(word[:cut])
            word = word[cut:]
        candidate = f"{line} {word}".strip()
        if draw.textlength(candidate, font=fnt) <= max_width:
            line = candidate
        else:
            lines.append(line)
            line = word
    if line:
        lines.append(line)
    return lines


def fit_text(draw, text, width, height, sizes=BODY_SIZES, bold=False):
    """Largest font size at which the text fits the box; the smallest size is
    cut off with an ellipsis if even that is too long."""
    for size in sizes:
        fnt, line_h = font(size, bold), round(size * 1.2)
        lines = wrap(draw, text, fnt, width)
        if len(lines) * line_h <= height:
            return fnt, size, lines, line_h
    max_lines = max(1, height // line_h)
    if len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        while last and draw.textlength(last + "…", font=fnt) > width:
            last = last[:-1]
        lines[-1] = last.rstrip() + "…"
    return fnt, size, lines, line_h


def draw_block(draw, text, top, bottom, sizes=BODY_SIZES, bold=False, strike=False):
    """Centered, wrapped text in the band between top and bottom."""
    fnt, size, lines, line_h = fit_text(draw, text, W - 14, bottom - top, sizes, bold)
    y = top + (bottom - top - line_h * len(lines)) / 2 + line_h / 2
    for line in lines:
        draw.text((W / 2, y), line, font=fnt, fill=0, anchor="mm")
        if strike:
            half = draw.textlength(line, font=fnt) / 2 + 3
            mid = round(y + size * 0.08)
            draw.line((W / 2 - half, mid, W / 2 + half, mid), fill=0, width=max(2, size // 10))
        y += line_h


def draw_check(draw, x, y, size=12):
    draw.rectangle((x, y, x + size, y + size), outline=0)
    draw.line((x + 3, y + 6, x + 5, y + 9, x + size - 3, y + 3), fill=0, width=2)


def draw_tab(draw, text, row, side):
    """A black label tucked into a corner (row "top"/"bottom", side "left"/"right")."""
    fnt = font(12, True)
    width = draw.textlength(text, font=fnt) + 14
    y0, y1 = (-8, BAND) if row == "top" else (H - BAND, H + 8)
    y = BAND / 2 if row == "top" else H - BAND / 2
    if side == "left":
        draw.rounded_rectangle((-8, y0, width, y1), radius=6, fill=0)
        draw.text((7, y), text, font=fnt, fill=255, anchor="lm")
    else:
        draw.rounded_rectangle((W - width, y0, W + 8, y1), radius=6, fill=0)
        draw.text((W - 7, y), text, font=fnt, fill=255, anchor="rm")


def draw_note(draw, text, row, side, checked=False):
    """Small text in a corner, with an optional check box in front of it."""
    fnt = font(12, True)
    y = BAND / 2 if row == "top" else H - BAND / 2
    width = draw.textlength(text, font=fnt) + (18 if checked else 0)
    x = 6 if side == "left" else W - 6 - width
    if checked:
        draw_check(draw, int(x), int(y) - 6)
        x += 18
    draw.text((x, y), text, font=fnt, fill=0, anchor="lm")


def draw_bolt(draw, cx, cy, height, fill=0):
    """A lightning bolt, the sign for "plugged in", centred on (cx, cy)."""
    s = height / 10
    points = ((1.2, -5), (-3, 0.8), (-0.3, 0.8), (-1.2, 5), (3, -0.8), (0.3, -0.8))
    draw.polygon([(round(cx + x * s), round(cy + y * s)) for x, y in points], fill=fill)


def draw_battery(draw, x, y, width, height, percent, line=2, gap=1):
    """The classic battery symbol with its top-left corner at (x, y): an outline
    with a tip on the right, filled from the left in proportion to the charge."""
    tip_w, tip_h = max(2, round(width / 10)), max(4, height // 2)
    draw.rectangle((x, y, x + width - 1, y + height - 1), outline=0, width=line)
    tip_y = y + (height - tip_h) // 2
    draw.rectangle((x + width, tip_y, x + width + tip_w - 1, tip_y + tip_h - 1), fill=0)
    inset = line + gap
    fill_w = round((width - 2 * inset) * min(max(percent, 0), 100) / 100)
    if percent > 0:
        fill_w = max(fill_w, 1)  # a sliver, so "nearly empty" doesn't look like "empty"
    if fill_w:
        draw.rectangle((x + inset, y + inset, x + inset + fill_w - 1, y + height - 1 - inset),
                       fill=0)


def badge_width(draw, badge):
    percent, plugged_in = badge
    return (9 if plugged_in else 0) + draw.textlength(f"{percent}%", font=font(12, True)) + 29


def draw_badge(draw, x, y, badge):
    """The small battery reading for a corner, from x along, centred on y: a bolt
    if plugged in, the percentage, then the battery symbol. badge is
    (percent, plugged in)."""
    percent, plugged_in = badge
    if plugged_in:
        draw_bolt(draw, x + 3, y, 11)
        x += 9
    text = f"{percent}%"
    draw.text((x, y), text, font=font(12, True), fill=0, anchor="lm")
    x += draw.textlength(text, font=font(12, True)) + 3
    draw_battery(draw, round(x), round(y) - 6, 24, 12, percent)


def draw_top_corner(draw, notes, badge, side, beside=""):
    """The top corner opposite the button labels: the battery badge right in the
    corner, then a note next to it. notes can list several wordings, longest
    first; the first that fits before the button label `beside` is used."""
    fnt = font(12, True)
    used = 6                                    # from the screen edge
    if badge:
        width = badge_width(draw, badge)
        draw_badge(draw, W - used - width if side == "right" else used, BAND / 2, badge)
        used += width + 10
    reach = W - (draw.textlength(beside, font=fnt) + 22 if beside else 6)
    for note in ([notes] if isinstance(notes, str) else notes):
        width = draw.textlength(note, font=fnt)
        if note and used + width <= reach:
            x = W - used - width if side == "right" else used
            draw.text((x, BAND / 2), note, font=fnt, fill=0, anchor="lm")
            return


def compose(text, *, upper="", lower="", note_top="", note_bottom="",
            checked=False, strike=False, detail="", sizes=BODY_SIZES, bold=False, badge=None):
    """One screen: big text in the middle, the two button labels in the corners
    on the buttons' side (upper = top button, lower = bottom button), and small
    notes in the corners on the other side. note_top can list several wordings
    (the first that fits is used); badge (percent, plugged in) adds the battery
    level in the top corner on that side."""
    image, draw = new_canvas()
    top = BAND + 2 if (upper or note_top or badge) else 4
    bottom = H - BAND - 2 if (lower or note_bottom) else H - 4
    if detail:
        split = top + round((bottom - top) * 0.5)
        draw_block(draw, text, top, split, sizes=(24, 20, 17), bold=True)
        draw_block(draw, detail, split, bottom, sizes=(16, 14, 12))
    else:
        draw_block(draw, text, top, bottom, sizes=sizes, bold=bold, strike=strike)
    other_side = "right" if LABEL_SIDE == "left" else "left"
    if note_top or badge:
        draw_top_corner(draw, note_top, badge, other_side, beside=upper)
    if note_bottom:
        draw_note(draw, note_bottom, "bottom", other_side, checked)
    if upper:
        draw_tab(draw, upper, "top", LABEL_SIDE)
    if lower:
        draw_tab(draw, lower, "bottom", LABEL_SIDE)
    return image


def item_screen(todo, badge=None):
    if not todo.items:
        return compose("Nothing to do. Hold the top button and say a new item.",
                       upper="hold: add", note_top="all clear", sizes=(18, 16, 14), badge=badge)
    item = todo.items[todo.index]
    left = sum(not i["done"] for i in todo.items)
    number, total = todo.index + 1, len(todo.items)
    return compose(
        item["text"], strike=item["done"],
        upper="next · hold: add",
        lower=("undo" if item["done"] else "done") + " · hold: delete",
        note_top=(f"{number} of {total}", f"{number}/{total}"),
        note_bottom="done" if item["done"] else f"{left} left", checked=item["done"],
        badge=badge,
    )


LISTENING = dict(upper="let go when done")
CONFIRM_ADD = dict(upper="cancel · hold: retry", lower="add", note_top="Add this?")
CONFIRM_DELETE = dict(upper="keep", lower="delete", note_top="Delete this?")
STATUS_NOTE = {"battery": "on battery", "charging": "charging", "plugged": "plugged in"}


def duration(hours):
    """4.83 -> "4 h 50 min" (to 10 minutes); under an hour, to 5 minutes."""
    minutes = round(hours * 6) * 10 if hours >= 1 else max(5, round(hours * 12) * 5)
    h, m = divmod(minutes, 60)
    return " ".join(part for part in (f"{h} h" if h else "", f"{m} min" if m else "") if part)


def battery_lines(reading):
    """The two lines under the big battery symbol."""
    if reading.status == "battery":
        left = f"about {duration(reading.hours)} left · " if reading.hours else ""
        return f"Using {reading.watts:.2f} W", f"{left}{reading.volts:.2f} V"
    if reading.status == "charging":
        return f"Charging at {reading.watts:.1f} W", f"{reading.volts:.2f} V · reads high while charging"
    return "Fully charged", f"{reading.volts:.2f} V"


def battery_screen(reading):
    """Battery details (both buttons): the symbol and level big, then power use."""
    image, draw = new_canvas()
    draw_tab(draw, "back", "top", LABEL_SIDE)
    low = reading.status == "battery" and reading.percent <= 10
    draw_top_corner(draw, "battery low" if low else STATUS_NOTE[reading.status], None,
                    "right" if LABEL_SIDE == "left" else "left", beside="back")
    text, fnt = f"{reading.percent}%", font(30, True)
    x, y = round((W - 85 - draw.textlength(text, font=fnt)) / 2), 26
    draw_battery(draw, x, y, 66, 30, reading.percent, line=3, gap=2)
    if reading.status != "battery":  # a bolt across it, light on the dark part and dark on the light
        mask = Image.new("1", image.size, 0)
        draw_bolt(ImageDraw.Draw(mask), x + 33, y + 15, 20, fill=1)
        image.paste(ImageChops.invert(image), (0, 0), mask)
    draw.text((x + 85, y + 15), text, font=fnt, fill=0, anchor="lm")
    first, second = battery_lines(reading)
    draw_block(draw, first, 62, 84, sizes=(16, 14), bold=True)
    draw_block(draw, second, 84, 116, sizes=(13, 12, 11))
    return image


# ---- The list ---------------------------------------------------------------
class TodoList:
    def __init__(self, path):
        self.path = Path(path)
        self.items = []
        self.index = 0
        self.load()

    def load(self):
        try:
            data = json.loads(self.path.read_text())
            items = [{"text": str(i["text"]).strip(), "done": bool(i.get("done"))}
                     for i in data.get("items", [])]
            self.items = [i for i in items if i["text"]]
            self.index = min(max(int(data.get("index", 0)), 0), max(len(self.items) - 1, 0))
        except FileNotFoundError:
            pass
        except (OSError, ValueError, KeyError, TypeError, AttributeError) as e:
            backup = self.path.with_name(self.path.name + ".bad")
            print(f"Couldn't read {self.path} ({e}); kept it as {backup}", flush=True)
            try:
                self.path.replace(backup)
            except OSError:
                pass

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps({"items": self.items, "index": self.index}, indent=1))
        os.replace(tmp, self.path)  # never leaves a half-written file

    def step(self, delta):
        if self.items:
            self.index = (self.index + delta) % len(self.items)
            self.save()

    def toggle(self):
        if self.items:
            self.items[self.index]["done"] = not self.items[self.index]["done"]
            self.save()

    def delete(self):
        if self.items:
            del self.items[self.index]
            self.index = min(self.index, max(len(self.items) - 1, 0))
            self.save()

    def add(self, text):
        self.items.append({"text": text, "done": False})
        self.index = len(self.items) - 1
        self.save()


# ---- Buttons ----------------------------------------------------------------
class Button:
    """Turns raw readings into "down", "press" (short), "hold" and "up" events.
    "up" always comes last, so it marks the end of a press either way."""

    DEBOUNCE = 0.03

    def __init__(self, name, is_pressed, hold_seconds=HOLD_SECONDS):
        self.name, self.is_pressed, self.hold_seconds = name, is_pressed, hold_seconds
        self.pressed = False
        self._raw, self._raw_since = False, 0.0
        self._down_at, self._held = 0.0, False

    def poll(self, now):
        events = []
        raw = self.is_pressed()
        if raw != self._raw:
            self._raw, self._raw_since = raw, now
        if raw != self.pressed and now - self._raw_since >= self.DEBOUNCE:
            self.pressed = raw
            if raw:
                self._down_at, self._held = now, False
                events.append("down")
            else:
                if not self._held:
                    events.append("press")
                events.append("up")
        if self.pressed and not self._held and now - self._down_at >= self.hold_seconds:
            self._held = True
            events.append("hold")
        return events


def gpio_buttons():
    import board
    import digitalio

    buttons = []
    for name, pin in (("top", UPPER_PIN), ("bottom", LOWER_PIN)):
        io = digitalio.DigitalInOut(getattr(board, f"D{pin}"))
        io.switch_to_input(pull=digitalio.Pull.UP)
        buttons.append(Button(name, lambda io=io: not io.value))  # pressed pulls it low
    return buttons


# ---- Display ----------------------------------------------------------------
class EinkDisplay:
    def __init__(self):
        import board
        import busio
        import digitalio

        module_name, class_name = DISPLAY_DRIVER.rsplit(".", 1)
        driver = getattr(importlib.import_module(module_name), class_name)
        pin = digitalio.DigitalInOut
        self.epd = driver(
            122, 250, busio.SPI(board.SCK, MOSI=board.MOSI, MISO=board.MISO),
            cs_pin=pin(board.CE0), dc_pin=pin(board.D22), sramcs_pin=None,
            rst_pin=pin(board.D27), busy_pin=pin(board.D17),
        )
        self.epd.rotation = ROTATION

    def show(self, image):
        self.epd.image(image)
        self.epd.display()     # full refresh: the screen flashes, then holds the image
        self.epd.power_down()  # the panel should sleep between updates


class Renderer(threading.Thread):
    """Updates the e-ink screen in the background so buttons stay responsive.
    If several screens are requested during one refresh, only the newest is drawn."""

    def __init__(self, display):
        super().__init__(daemon=True)
        self.display = display
        self._cond = threading.Condition()
        self._pending = None
        self._last = None
        self.idle = threading.Event()
        self.idle.set()

    def show(self, image):
        with self._cond:
            self._pending = image
            self.idle.clear()
            self._cond.notify()

    def run(self):
        while True:
            with self._cond:
                while self._pending is None:
                    self.idle.set()
                    self._cond.wait()
                image, self._pending = self._pending, None
            data = image.tobytes()
            if data != self._last:
                try:
                    self.display.show(image)
                    self._last = data
                except Exception as e:  # keep running; the next change retries
                    print(f"Display update failed: {e}", flush=True)


# ---- Speech -----------------------------------------------------------------
def pick_input_device(sd):
    """A USB microphone if there is one, otherwise the first real sound-card
    input, such as an I2S mic ("(hw:1,0)" in its name). None: PortAudio's default."""
    if MIC_DEVICE is not None:
        return MIC_DEVICE
    inputs = [(number, device["name"]) for number, device in enumerate(sd.query_devices())
              if device["max_input_channels"] > 0]
    for number, name in inputs:
        if "usb" in name.lower():
            return number
    for number, name in inputs:
        if "(hw:" in name:
            return number
    return None


def load_model():
    from moonshine_voice import ModelArch, get_model_for_language

    # Tiny Streaming: 34 M parameters, small enough for the Zero 2 W's 512 MB.
    return get_model_for_language("en", ModelArch.TINY_STREAMING)


class Speech:
    """Turns what the microphone hears into text with Moonshine.

    The top button does two jobs, so recording starts as soon as it goes
    down (begin) and the audio is just kept. If the press turns into a hold,
    that audio and everything after it goes to Moonshine (commit); if it was a
    short press, the audio is thrown away (cancel) and Moonshine never runs.
    The model loads in the background, so the list works right away."""

    def __init__(self):
        self.ready = threading.Event()
        self.error = None            # why speech can't work at all
        self.capturing = False       # microphone open since the last begin()
        self.capture_error = None    # why the last begin() couldn't open the mic
        self._chunks = queue.Queue()
        self._mic = self._feeder = None
        threading.Thread(target=self._load, daemon=True).start()

    def _load(self):
        try:
            import sounddevice
            from moonshine_voice import Transcriber

            import numpy

            path, arch = load_model()
            self._transcriber = Transcriber(model_path=path, model_arch=arch)
            self._stream = self._transcriber.create_stream(update_interval=0.5)
            self._sd, self._np = sounddevice, numpy
            self._device = pick_input_device(sounddevice)
            try:
                info = sounddevice.query_devices(self._device, "input")
                self._rate = int(info["default_samplerate"])
                print(f"Speech ready: '{info['name']}' at {self._rate} Hz", flush=True)
            except Exception as e:  # no mic yet: the list still works
                self._rate = 16000
                print(f"Speech ready, but no microphone found yet ({e})", flush=True)
            self.ready.set()
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
            print(f"Speech unavailable: {self.error}", flush=True)

    def _open_mic(self):
        if self._device is None:  # a mic plugged in after startup
            self._device = pick_input_device(self._sd)
        info = self._sd.query_devices(self._device, "input")
        self._rate = int(info["default_samplerate"])

        def callback(indata, frames, time_info, status):
            # A mono mic fills one side of a stereo stream (an I2S mic with SEL
            # to ground uses the left); take whichever side has the sound.
            side = int(self._np.abs(indata).max(axis=0).argmax()) if indata.shape[1] > 1 else 0
            self._chunks.put(indata[:, side].copy())

        error = None
        for channels in (1, 2):  # some microphones only offer stereo
            try:
                return self._sd.InputStream(
                    device=self._device, channels=channels, samplerate=self._rate,
                    dtype="float32", blocksize=self._rate // 10, callback=callback)
            except Exception as e:
                error = e
        raise error

    def _drain(self):
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                return

    def begin(self):
        """Open the microphone and keep what it hears (top button down)."""
        self._drain()
        self.capturing, self.capture_error = False, None
        try:
            mic = self._open_mic()
            mic.start()
        except Exception as e:
            self.capture_error = f"{type(e).__name__}: {e}"
            return
        self._mic, self.capturing = mic, True

    def cancel(self):
        """It was a short press: throw the audio away."""
        if self._mic:
            self._mic.abort()
            self._mic.close()
            self._mic = None
        self.capturing = False
        self._drain()

    def commit(self, context=""):
        """The press became a hold: transcribe from the moment it started."""
        if context:
            try:  # nudges the model toward words already on your list
                self._transcriber.set_context(context)
            except Exception as e:
                print(f"Couldn't set context: {e}", flush=True)
        self._stream.start()
        self._feeder = threading.Thread(target=self._feed, daemon=True)
        self._feeder.start()

    def _feed(self):
        # Streaming transcription works on the audio while you are still talking.
        np, level = self._np, 0.0
        self._stats = {"seconds": 0.0, "peak": 0.0, "gain": 1.0}
        while (chunk := self._chunks.get()) is not None:
            peak = float(np.abs(chunk).max()) if len(chunk) else 0.0
            self._stats["seconds"] += len(chunk) / self._rate
            self._stats["peak"] = max(self._stats["peak"], peak)
            if AUTO_GAIN:
                # Follow the loudest recent sound up at once and down slowly, and
                # raise quiet audio so its peaks reach about 0.3 (never lower it).
                level = max(peak, level * 0.85)
                gain = min(MAX_GAIN, max(1.0, 0.3 / max(level, 1e-6)))
                self._stats["gain"] = max(self._stats["gain"], gain)
                chunk = np.clip(chunk * gain, -1.0, 1.0)
            self._stream.add_audio(chunk.tolist(), self._rate)

    def stop(self):
        """Stop recording and return what was said. Blocks while the last words
        are transcribed, so call it from a worker thread."""
        self._mic.stop()
        self._mic.close()
        self._mic, self.capturing = None, False
        self._chunks.put(None)
        self._feeder.join()
        s = self._stats  # in the log, so a silent or too-quiet mic is easy to spot
        print(f"Recorded {s['seconds']:.1f} s; loudest sound {s['peak']:.3f} of 1.0; "
              f"boosted up to {s['gain']:.0f}x", flush=True)
        transcript = self._stream.stop()
        lines = transcript.lines if transcript else []
        return " ".join(line.text.strip() for line in lines if line.text.strip())


def clean(text):
    text = " ".join(text.split()).strip(" .")
    return text[:1].upper() + text[1:]


# ---- Battery (Waveshare UPS HAT (C)) ------------------------------------------
class INA219:
    """The UPS HAT's battery-monitor chip, read through Linux's own I2C driver
    (no extra Python packages needed)."""

    I2C_SLAVE = 0x0703  # ioctl that picks which chip on the bus to talk to

    def __init__(self, address=BATTERY_ADDRESS, bus=1):
        import fcntl

        self._fcntl = fcntl
        self.fd = os.open(f"/dev/i2c-{bus}", os.O_RDWR)
        try:
            fcntl.ioctl(self.fd, self.I2C_SLAVE, address)
            # 16 V range, up to ±320 mV across the resistor, every reading averaged
            # over 128 samples (68 ms) to smooth out the Pi's spiky draw, continuous.
            self._write(0x00, 0x1FFF)
        except OSError:
            os.close(self.fd)
            raise

    def _write(self, register, value):
        os.write(self.fd, bytes([register, value >> 8, value & 0xFF]))

    def _read(self, register):
        # Choosing the register and reading it are two steps; the lock stops a
        # second copy of this program (todo.py --battery) from slipping in between.
        self._fcntl.flock(self.fd, self._fcntl.LOCK_EX)
        try:
            os.write(self.fd, bytes([register]))
            high, low = os.read(self.fd, 2)
        finally:
            self._fcntl.flock(self.fd, self._fcntl.LOCK_UN)
        return high << 8 | low

    def read(self):
        """(battery volts, volts across the current-sense resistor). The second is
        negative while the battery powers the Pi, positive while it charges."""
        shunt = self._read(0x01)
        if shunt & 0x8000:
            shunt -= 0x10000
        return (self._read(0x02) >> 3) * 0.004, shunt * 0.00001


# Charge left in a lithium-polymer cell at a given voltage, under a light load
# like the Pi's. Lithium cells hold close to 3.7-3.9 V for most of their charge,
# then drop fast, so a straight line from 3.0 to 4.2 V (Waveshare's demo) says
# 50% when the battery is nearly flat.
LIPO_CURVE = ((3.30, 0), (3.50, 2), (3.60, 5), (3.66, 10), (3.70, 20), (3.73, 30), (3.76, 40),
              (3.79, 50), (3.83, 60), (3.88, 70), (3.95, 80), (4.04, 90), (4.13, 100))


def percent_from_volts(volts):
    if volts <= LIPO_CURVE[0][0]:
        return 0
    for (v0, p0), (v1, p1) in zip(LIPO_CURVE, LIPO_CURVE[1:]):
        if volts <= v1:
            return round(p0 + (p1 - p0) * (volts - v0) / (v1 - v0))
    return 100


# percent; status "battery", "charging" or "plugged" (on the charger and full);
# volts; watts in or out of the battery; hours left on battery (or None)
Reading = collections.namedtuple("Reading", "percent status volts watts hours")


def describe(reading):
    text = f"Battery {reading.percent}% ({reading.volts:.2f} V), "
    if reading.status == "battery":
        left = f", about {duration(reading.hours)} left" if reading.hours else ""
        return f"{text}on battery, using {reading.watts:.2f} W{left}"
    if reading.status == "charging":
        return f"{text}charging at {reading.watts:.2f} W"
    return f"{text}plugged in and full"


class Battery:
    """Charge level and power use from the UPS HAT's INA219 chip.

    The level is estimated from the battery's voltage, so it's approximate, and
    it reads high while charging (Waveshare says the same of its own demo). The
    chip is read every few seconds; the level uses the middle voltage of the last
    minute and the power the average, so a burst of work such as transcribing
    doesn't make the numbers jump around."""

    SAMPLE_SECONDS = 5
    WINDOW_SECONDS = 60
    SETTLE_SAMPLES = 3     # readings in a row before "plugged in" changes
    QUIET_VOLTS = 0.0005   # under 0.5 mV across the resistor: hardly any current
    LOG_SECONDS = 900      # how often the level goes in the log

    def __init__(self, sensor, now, sample_seconds=SAMPLE_SECONDS):
        self.sensor, self.sample_seconds = sensor, sample_seconds
        self.shunt = SHUNT_OHMS
        self.samples = []      # (time, volts, shunt volts) since plugging in or out
        self.plugged = None    # running on the charger rather than the battery
        self._flip = []        # recent readings that disagree with self.plugged
        self.reading = None
        self.last = None       # the newest (volts, shunt volts)
        self.good_at, self.failing = now, False
        self.next_sample, self.next_log = now, now + self.WINDOW_SECONDS
        self.poll(now)

    def poll(self, now):
        """Read the chip if it's time. Returns True if it tried."""
        if now < self.next_sample:
            return False
        self.next_sample = now + self.sample_seconds
        try:
            volts, shunt = self.sensor.read()
            if not 2.5 <= volts <= 4.6:
                raise ValueError(f"the battery reads {volts:.2f} V")
        except Exception as e:
            if not self.failing:
                print(f"Battery reading failed: {e}", flush=True)
                self.failing = True
            if now - self.good_at > 2 * self.WINDOW_SECONDS:
                self.reading = None  # hide the meter rather than show an old level
            return True
        if self.failing:
            print("Battery readings are back", flush=True)
        self.failing, self.good_at, self.last = False, now, (volts, shunt)
        self._add((now, volts, shunt))
        self._check_shunt()
        self.reading = self._summary()
        if now >= self.next_log and self.reading:
            self.next_log = now + self.LOG_SECONDS
            print(describe(self.reading), flush=True)
        return True

    def _add(self, sample):
        plugged = sample[2] > -self.QUIET_VOLTS  # not draining the battery
        if self.plugged is None:
            self.plugged = plugged
        if plugged == self.plugged:
            self._flip = []
            self.samples.append(sample)
        else:
            self._flip.append(sample)
            if len(self._flip) >= self.SETTLE_SAMPLES:
                print("Charger plugged in" if plugged else "Charger unplugged", flush=True)
                self.plugged, self.samples, self._flip = plugged, self._flip, []
        self.samples = [s for s in self.samples if sample[0] - s[0] <= self.WINDOW_SECONDS]

    def _check_shunt(self):
        """Waveshare's FAQ says the current-sense resistor is 0.1 ohm, but its demo
        code is written for 0.01 ohm. A running Pi Zero 2 W draws well over 0.3 W,
        so a minute on battery shows which it is: with 0.01 ohm, the readings
        worked out for 0.1 ohm come out ten times too small."""
        if self.shunt is not None or self.plugged or len(self.samples) < 10:
            return
        watts = sorted(-volts * shunt / 0.1 for _, volts, shunt in self.samples)
        self.shunt = 0.01 if watts[len(watts) // 4] < 0.15 else 0.1
        print(f"Battery current-sense resistor: {self.shunt} ohm", flush=True)

    def _summary(self):
        if not self.samples:
            return self.reading
        volts = statistics.median(v for _, v, _ in self.samples)
        shunt = statistics.median(s for _, _, s in self.samples)
        watts = abs(statistics.fmean(v * s for _, v, s in self.samples)) / (self.shunt or 0.1)
        percent = percent_from_volts(volts)
        if not self.plugged:
            status = "battery"
        elif shunt > self.QUIET_VOLTS:
            status, percent = "charging", min(percent, 99)  # 100% only once it's done
        else:
            status = "plugged"
        hours = None
        if status == "battery" and watts >= 0.1:
            hours = percent / 100 * BATTERY_MAH / 1000 * 3.7 / watts
        return Reading(percent, status, volts, watts, hours)


def open_battery(now, sample_seconds=Battery.SAMPLE_SECONDS):
    """The battery meter, or None (saying why) if there's no UPS HAT to read."""
    try:
        battery = Battery(INA219(), now, sample_seconds)
    except FileNotFoundError:
        why = "I2C is switched off. Turn it on: sudo raspi-config nonint do_i2c 0"
    except PermissionError:
        why = "no permission to use I2C. Run: sudo usermod -aG i2c $USER, then reboot"
    except OSError as e:
        why = (f"no UPS HAT answered at I2C address {BATTERY_ADDRESS:#04x} "
               f"({e.strerror or e}). Check it with: sudo i2cdetect -y 1")
    else:
        print("Battery meter on" + (f": {describe(battery.reading)}" if battery.reading else ""),
              flush=True)
        return battery
    print(f"Battery meter off: {why}", flush=True)
    return None


# ---- The app ----------------------------------------------------------------
class App:
    """Screens and button logic. Buttons are "top" (GPIO 6 normally) and "bottom"
    (GPIO 5 normally); events are "down", "press", "hold" and "up"."""

    def __init__(self, todo, renderer, speech, words_file=WORDS_FILE, battery=None):
        self.todo, self.renderer, self.speech = todo, renderer, speech
        self.words_file = Path(words_file)
        self.battery = battery
        self.state = "browse"
        self.pending = ""                  # transcript waiting for confirmation
        self.message = ("", "")
        self.message_until = 0.0
        self.talk_started = 0.0
        self.now = 0.0
        self.held = set()                  # buttons down right now
        self.swallow = set()               # buttons whose current press is used up
        self.results = queue.Queue()
        self.badge_shown, self.badge_shown_at = None, float("-inf")  # battery as last drawn

    def badge(self):
        """The battery for the item screen: (percent, plugged in), or None."""
        reading = self.battery.reading if self.battery else None
        return (reading.percent, reading.status != "battery") if reading else None

    def redraw(self):
        if self.state == "browse":
            self.badge_shown, self.badge_shown_at = self.badge(), self.now
            image = item_screen(self.todo, self.badge_shown)
        elif self.state == "battery":
            reading = self.battery.reading if self.battery else None
            image = battery_screen(reading) if reading else compose("No battery reading", bold=True)
        elif self.state == "listening":
            image = compose("Listening…", bold=True, sizes=(28, 24), **LISTENING)
        elif self.state == "transcribing":
            image = compose("Writing it down…", bold=True, sizes=(28, 24, 20))
        elif self.state == "confirm_add":
            image = compose(self.pending, **CONFIRM_ADD)
        elif self.state == "confirm_delete":
            image = compose(self.todo.items[self.todo.index]["text"], **CONFIRM_DELETE)
        else:
            title, detail = self.message
            image = compose(title, detail=detail, bold=True, sizes=(26, 22, 18))
        self.renderer.show(image)

    def show_message(self, title, detail="", seconds=3.0):
        self.state, self.message = "message", (title, detail)
        self.message_until = self.now + seconds
        self.redraw()

    def handle(self, button, event, now):
        self.now = now
        if event == "down":
            self.held.add(button)
        elif event == "up":
            self.held.discard(button)
        if button in self.swallow:  # rest of a press that already did its job
            if event == "up":
                self.swallow.discard(button)
            return
        state, todo = self.state, self.todo

        other = "bottom" if button == "top" else "top"
        if event == "down" and other in self.held:  # both buttons at once
            if other in self.swallow:   # the other one just closed a screen, so this
                self.swallow.add(button)  # press is part of that and does nothing else
                return
            if state == "browse":
                return self.show_battery()

        if state in ("message", "battery"):  # any new press just closes the screen
            if event == "down":
                self.state = "browse"
                self.swallow.add(button)
                self.redraw()
            return

        if state in ("browse", "confirm_add") and button == "top":
            if event == "down":       # might become a hold: start listening now
                self.talk_started = now
                if self.speech.ready.is_set():
                    self.speech.begin()
            elif event == "press":    # short press after all
                if self.speech.capturing:
                    self.speech.cancel()
                if state == "browse":
                    todo.step(1)
                self.state = "browse"  # (on the confirm screen: cancel)
                self.redraw()
            elif event == "hold":
                self.start_talking()
            return

        if state == "browse" and button == "bottom":
            if event == "press":
                todo.toggle()
            elif event == "hold" and todo.items:
                self.state = "confirm_delete"
            else:
                return
            self.redraw()
        elif state == "confirm_add" and button == "bottom" and event == "press":
            todo.add(self.pending)
            self.state = "browse"
            self.redraw()
        elif state == "confirm_delete" and event == "press":
            if button == "bottom":
                todo.delete()
            self.state = "browse"      # top button: keep it
            self.redraw()
        elif state == "listening" and button == "top" and event == "up":
            self.finish_talking()

    def show_battery(self):
        """Both buttons pressed together: the battery screen."""
        if self.speech.capturing:  # the top button went down first and opened the mic
            self.speech.cancel()
        self.swallow.update(("top", "bottom"))
        if not (self.battery and self.battery.reading):
            return self.show_message("No battery reading",
                                     "Is I2C on, and the UPS HAT fitted? See the README", 6)
        self.state, self.message_until = "battery", self.now + BATTERY_SCREEN_SECONDS
        self.redraw()

    def check_battery(self, now):
        """Redraw the item screen when its battery level is out of date enough to
        matter. Each refresh flashes the screen, so only: on plugging in or out,
        every 10 points, every 5 below 20% (so a screen left behind by a flat
        battery says so), and on reaching 100% on the charger. At most once a
        minute; any button press shows the latest level anyway."""
        if self.state != "browse":
            return
        new, old = self.badge(), self.badge_shown
        if new == old or now - self.badge_shown_at < 60:
            return
        if new is None or old is None or new[1] != old[1]:
            return self.redraw()
        change = abs(new[0] - old[0])
        if change >= 10 or (min(new[0], old[0]) <= 20 and change >= 5) or (
                new[1] and new[0] == 100 > old[0]):
            self.redraw()

    def context(self):
        words = ""
        try:
            words = self.words_file.read_text()
        except OSError:
            pass
        return " ".join([i["text"] for i in self.todo.items] + [words]).strip()

    def start_talking(self):
        speech = self.speech
        if not speech.ready.is_set():
            self.swallow.add("top")  # so letting go doesn't close the message
            if speech.error:
                return self.show_message("Speech isn't working", speech.error, 6)
            return self.show_message("Still loading", "Speech will be ready in a moment")
        if not speech.capturing and speech.capture_error is None:
            speech.begin()  # the model finished loading during this press
        if not speech.capturing:
            self.swallow.add("top")
            return self.show_message("Microphone problem",
                                     speech.capture_error or "No microphone found", 6)
        try:
            speech.commit(self.context())
        except Exception as e:
            speech.cancel()
            self.swallow.add("top")
            return self.show_message("Speech error", str(e), 6)
        self.state = "listening"
        self.redraw()

    def finish_talking(self):
        self.state = "transcribing"
        self.redraw()
        threading.Thread(target=self._transcribe, daemon=True).start()

    def _transcribe(self):
        try:
            self.results.put(("text", clean(self.speech.stop())))
        except Exception as e:
            self.results.put(("error", str(e)))

    def tick(self, now):
        self.now = now
        if self.battery and self.battery.poll(now):
            self.check_battery(now)
        try:
            kind, value = self.results.get_nowait()
        except queue.Empty:
            pass
        else:
            if kind == "text" and value:
                self.pending, self.state = value, "confirm_add"
                self.redraw()
            elif kind == "text":
                self.show_message("Didn't catch that", "Hold the top button while you speak")
            else:
                self.show_message("Speech error", value, 6)
        if self.state == "listening" and now - self.talk_started >= MAX_TALK_SECONDS:
            self.finish_talking()
        if self.state in ("message", "battery") and now >= self.message_until:
            self.state = "browse"
            self.redraw()


# ---- Entry points -----------------------------------------------------------
def hardware_hint(error):
    text = str(error).lower()
    if "busy" in text:
        return ("A pin is already in use. If the app is already running as a service, "
                "stop it first: sudo systemctl stop todo. Otherwise add the line "
                "dtoverlay=spi0-0cs to /boot/firmware/config.txt and reboot (see README).")
    if "spidev" in text or "no such file" in text:
        return "SPI looks switched off: sudo raspi-config nonint do_spi 0, then reboot."
    return ""


def run():
    todo = TodoList(LIST_FILE)
    try:
        display = EinkDisplay()
        buttons = gpio_buttons()
    except Exception as e:
        print(f"Couldn't start the display or buttons: {e}", flush=True)
        if hint := hardware_hint(e):
            print(hint, flush=True)
        sys.exit(1)
    renderer = Renderer(display)
    renderer.start()
    app = App(todo, renderer, Speech(), battery=open_battery(time.monotonic()))
    app.redraw()
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))  # systemd stop
    print(f"Running with {len(todo.items)} item(s) from {LIST_FILE}", flush=True)
    try:
        while True:
            now = time.monotonic()
            for button in buttons:
                for event in button.poll(now):
                    app.handle(button.name, event, now)
            app.tick(now)
            time.sleep(0.02)
    finally:
        renderer.idle.wait(timeout=15)  # let a refresh in progress finish


def preview(folder):
    """Draw every screen into PNG files, for checking the layout on a computer."""
    import tempfile

    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    todo = TodoList(Path(tempfile.mkdtemp()) / "todo.json")
    for text in ("Buy milk", "Call the dentist about moving Thursday's appointment",
                 "Pay the electric bill"):
        todo.add(text)
    todo.index = 1
    shots = {"01-item": item_screen(todo, (82, False))}
    todo.toggle()
    shots["02-crossed-off"] = item_screen(todo, (100, True))
    shots["03-listening"] = compose("Listening…", bold=True, sizes=(28, 24), **LISTENING)
    shots["04-writing"] = compose("Writing it down…", bold=True, sizes=(28, 24, 20))
    shots["05-confirm-add"] = compose("Pick up the dry cleaning", **CONFIRM_ADD)
    shots["06-confirm-delete"] = compose("Pay the electric bill", **CONFIRM_DELETE)
    shots["07-didnt-catch"] = compose("Didn't catch that", bold=True, sizes=(26, 22, 18),
                                      detail="Hold the top button while you speak")
    shots["08-empty"] = item_screen(TodoList(Path(tempfile.mkdtemp()) / "todo.json"), (45, False))
    shots["09-battery"] = battery_screen(Reading(82, "battery", 3.97, 0.62, 4.9))
    shots["10-charging"] = battery_screen(Reading(64, "charging", 4.08, 1.9, None))
    shots["11-battery-low"] = battery_screen(Reading(6, "battery", 3.62, 0.71, 0.31))
    for name, image in shots.items():
        image.save(folder / f"{name}.png")
    print(f"Saved {len(shots)} screens in {folder}")


def watch_battery():
    """Print the battery readings every 2 seconds, to see what a change saves."""
    battery = open_battery(time.monotonic(), sample_seconds=2)
    if not battery:
        sys.exit(1)
    print("Each line: the reading right now | the last minute's average. Ctrl-C stops.",
          flush=True)
    try:
        while True:
            if battery.poll(time.monotonic()) and battery.reading and not battery.failing:
                volts, shunt = battery.last
                amps = shunt / (battery.shunt or 0.1)
                print(f"{volts:.3f} V {amps * 1000:+6.0f} mA {abs(volts * amps):5.2f} W | "
                      f"{describe(battery.reading)}", flush=True)
            time.sleep(0.2)
    except KeyboardInterrupt:
        pass


def main():
    parser = argparse.ArgumentParser(description="Voice to-do list for an e-ink Pi")
    parser.add_argument("--download", action="store_true", help="download the speech model and exit")
    parser.add_argument("--battery", action="store_true",
                        help="print battery readings every 2 seconds (works alongside the app)")
    parser.add_argument("--preview", metavar="DIR", help="save every screen as a PNG and exit")
    args = parser.parse_args()
    if args.preview:
        preview(args.preview)
    elif args.battery:
        watch_battery()
    elif args.download:
        path, _ = load_model()
        print(f"Speech model ready in {path}")
    else:
        try:
            run()
        except KeyboardInterrupt:
            print("Stopped", flush=True)


if __name__ == "__main__":
    main()
