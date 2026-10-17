"""Synthetic waveform builders for tests (bit n of each sample byte = channel n)."""
import numpy as np


class Wave:
    def __init__(self, rate, idle=0xFF):
        self.rate = rate
        self.chunks = []
        self.state = idle

    def hold(self, seconds, **levels):
        for ch, v in levels.items():
            n = int(ch[1:])
            self.state = (self.state | (1 << n)) if v else (self.state & ~(1 << n))
        self.chunks.append(np.full(max(1, round(seconds * self.rate)), self.state, dtype=np.uint8))

    def samples(self):
        return np.concatenate(self.chunks)


def uart(w, ch, data, baud, parity=None):
    bit = 1 / baud
    for b in data:
        w.hold(bit, **{f"d{ch}": 0})
        bits = [(b >> i) & 1 for i in range(8)]
        if parity:
            bits.append((sum(bits) & 1) ^ (parity == "odd"))
        for v in bits:
            w.hold(bit, **{f"d{ch}": v})
        w.hold(bit * 2, **{f"d{ch}": 1})


def i2c(w, scl, sda, addr, rw, data, half=5e-6):
    S, D = f"d{scl}", f"d{sda}"
    w.hold(half, **{S: 1, D: 1})
    w.hold(half, **{D: 0})          # START
    w.hold(half, **{S: 0})
    for byte in [addr << 1 | rw] + list(data):
        for i in range(7, -1, -1):
            w.hold(half, **{D: (byte >> i) & 1})
            w.hold(half, **{S: 1})
            w.hold(half, **{S: 0})
        w.hold(half, **{D: 0})      # ACK from slave
        w.hold(half, **{S: 1})
        w.hold(half, **{S: 0})
    w.hold(half, **{D: 0})
    w.hold(half, **{S: 1})
    w.hold(half, **{D: 1})          # STOP


def spi_mode0(w, clk, mosi, cs, data, half=1e-6):
    C, M, S = f"d{clk}", f"d{mosi}", f"d{cs}"
    w.hold(half, **{S: 1, C: 0})
    w.hold(half, **{S: 0})
    for byte in data:
        for i in range(7, -1, -1):
            w.hold(half, **{M: (byte >> i) & 1})
            w.hold(half, **{C: 1})
            w.hold(half, **{C: 0})
    w.hold(half, **{S: 1})
