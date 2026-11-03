"""Fake Blinka busio.SPI that logs every byte with the D/C pin level (GPIO 22)."""
import digitalio

LOG = []  # (is_data, bytes)


class SPI:
    def __init__(self, clock, MOSI=None, MISO=None):
        self.locked = False

    def try_lock(self):
        if self.locked:
            return False
        self.locked = True
        return True

    def unlock(self):
        self.locked = False

    def configure(self, baudrate=100000, polarity=0, phase=0, bits=8):
        pass

    def _log(self, data):
        dc = digitalio.PINS[22].value
        cs = digitalio.PINS[8].value
        if not cs:  # only bytes sent while the display is selected count
            LOG.append((bool(dc), bytes(data)))

    def write(self, buf, start=0, end=None):
        self._log(buf[start:end])

    def readinto(self, buf, start=0, end=None, write_value=0):
        for i in range(start, end if end is not None else len(buf)):
            buf[i] = 0

    def write_readinto(self, out_buf, in_buf, **kw):
        self._log(out_buf)
        for i in range(len(in_buf)):
            in_buf[i] = 0
