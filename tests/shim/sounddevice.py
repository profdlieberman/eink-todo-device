"""Fake sounddevice. DEVICE_SET picks what's plugged in:
  "usb"  - HDMI output plus a stereo-only USB microphone at 48 kHz
  "i2s"  - HDMI output, ALSA's virtual devices, and the googlevoicehat I2S card
  "none" - no microphone
AMPLITUDE and CHANNEL set the fake signal's level and which side it's on."""
import threading
import time

import numpy as np

HDMI = {"name": "vc4-hdmi: MAI PCM i2s-hifi-0 (hw:0,0)", "max_input_channels": 0,
        "default_samplerate": 48000.0}
SETS = {
    "usb": [HDMI, {"name": "USB PnP Sound Device: Audio (hw:1,0)", "max_input_channels": 2,
                   "default_samplerate": 48000.0}],
    "i2s": [HDMI,
            {"name": "sysdefault", "max_input_channels": 128, "default_samplerate": 44100.0},
            {"name": "snd_rpi_googlevoicehat_soundcard: Google voiceHAT SoundCard HiFi "
                     "voicehat-hifi-0 (hw:1,0)", "max_input_channels": 2,
             "default_samplerate": 48000.0},
            {"name": "default", "max_input_channels": 128, "default_samplerate": 44100.0}],
    "none": [HDMI],
}
DEVICE_SET = "usb"
AMPLITUDE = 0.01
CHANNEL = 0
OPENED = []
NO_MIC = False  # older tests: same as DEVICE_SET = "none"


class PortAudioError(Exception):
    pass


def _devices():
    return SETS["none" if NO_MIC else DEVICE_SET]


def query_devices(device=None, kind=None):
    devices = _devices()
    if device is None and kind is None:
        return devices
    if device is None:
        raise PortAudioError("Error querying device -1")
    if device >= len(devices) or devices[device]["max_input_channels"] == 0:
        raise PortAudioError(f"Error querying device {device}")
    return devices[device]


class InputStream:
    def __init__(self, device=None, channels=1, samplerate=None, dtype="float32",
                 blocksize=0, callback=None):
        devices = _devices()
        if device is None or device >= len(devices) or not devices[device]["max_input_channels"]:
            raise PortAudioError("Error opening InputStream: Invalid device")
        if channels == 1:  # these mics only offer stereo, like I2S cards and cheap USB mics
            raise PortAudioError("Invalid number of channels")
        self.device, self.channels, self.blocksize = device, channels, blocksize
        self.callback, self.samplerate = callback, samplerate
        self._run = False
        self.closed = False
        OPENED.append(self)

    def start(self):
        self._run = True
        self._thread = threading.Thread(target=self._pump, daemon=True)
        self._thread.start()

    def _pump(self):
        t = 0
        while self._run:
            block = np.zeros((self.blocksize, self.channels), dtype=np.float32)
            n = np.arange(t, t + self.blocksize)
            block[:, CHANNEL] = AMPLITUDE * np.sin(2 * np.pi * 300 * n / self.samplerate)
            t += self.blocksize
            self.callback(block, self.blocksize, None, None)
            time.sleep(self.blocksize / self.samplerate)

    def stop(self):
        self._run = False
        self._thread.join()

    def abort(self):
        self.stop()

    def close(self):
        self.closed = True
