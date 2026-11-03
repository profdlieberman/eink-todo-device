"""Fake Blinka board module for a Raspberry Pi 40-pin header (BCM numbering)."""


class Pin:
    def __init__(self, id):
        self.id = id

    def __repr__(self):
        return f"D{self.id}"


for _n in range(28):
    globals()[f"D{_n}"] = Pin(_n)
SCK, MOSI, MISO, CE0, CE1 = D11, D10, D9, D8, D7  # noqa: F821
