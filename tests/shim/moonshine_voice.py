"""Fake moonshine_voice with the same calls the app uses (checked against v0.1.5)."""
from dataclasses import dataclass
from enum import IntEnum

CALLS = []
NEXT_TEXT = ["buy milk."]


class ModelArch(IntEnum):
    TINY = 0
    BASE = 1
    TINY_STREAMING = 2


def get_model_for_language(wanted_language="en", wanted_model_arch=None, **kw):
    CALLS.append(("get_model", wanted_language, wanted_model_arch))
    return "/fake/models/tiny-streaming-en", wanted_model_arch


@dataclass
class TranscriptLine:
    text: str


@dataclass
class Transcript:
    lines: list


class Stream:
    def __init__(self, update_interval):
        self.update_interval = update_interval
        self.started = False
        self.samples = 0
        self.rates = set()
        self.peak = 0.0

    def start(self):
        assert not self.started, "stream started twice"
        self.started = True
        self.samples = 0
        self.peak = 0.0
        CALLS.append(("start",))

    def add_audio(self, audio_data, sample_rate=16000):
        assert self.started, "audio added to a stopped stream"
        assert isinstance(audio_data, list) and all(isinstance(v, float) for v in audio_data[:5])
        self.samples += len(audio_data)
        self.rates.add(sample_rate)
        self.peak = max([self.peak] + [abs(v) for v in audio_data])

    def stop(self):
        assert self.started
        self.started = False
        CALLS.append(("stop", self.samples, tuple(self.rates), round(self.peak, 4)))
        return Transcript([TranscriptLine(t) for t in NEXT_TEXT])


class Transcriber:
    def __init__(self, model_path, model_arch=ModelArch.BASE, update_interval=0.5, options=None):
        CALLS.append(("Transcriber", model_path, model_arch))
        self.stream = None

    def create_stream(self, update_interval=None, flags=0, transcribe_flags=0):
        self.stream = Stream(update_interval)
        return self.stream

    def set_context(self, context, max_terms=0):
        CALLS.append(("context", context))
