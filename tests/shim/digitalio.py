"""Fake Blinka digitalio: pins remember their value; tests can press buttons."""


class Direction:
    INPUT, OUTPUT = "in", "out"


class Pull:
    UP, DOWN = "up", "down"


PINS = {}        # pin number -> DigitalInOut
claimed = set()


class DigitalInOut:
    def __init__(self, pin):
        if pin.id in claimed:
            raise OSError(f"GPIO{pin.id} busy")
        claimed.add(pin.id)
        self.pin = pin
        self.direction = Direction.INPUT
        self.pull = None
        self.value = True if pin.id not in (17,) else False  # BUSY idle low
        PINS[pin.id] = self

    def switch_to_input(self, pull=None):
        self.direction, self.pull = Direction.INPUT, pull
        if pull == Pull.UP:
            self.value = True

    def switch_to_output(self, value=False):
        self.direction, self.value = Direction.OUTPUT, value
