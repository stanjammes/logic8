import numpy as np
import pytest

from logic8 import analysis, decoders, formats
from logic8.capture import Capture

import synth


def pwm_capture(rate=24e6, freq=1000, duty=0.25, cycles=10):
    period = round(rate / freq)
    one = np.zeros(period, dtype=np.uint8)
    one[: round(period * duty)] = 1
    return Capture.from_samples(np.tile(one, cycles), rate, channels=[0])


def test_roundtrip_samples():
    rng = np.random.default_rng(1)
    s = rng.integers(0, 256, 5000, dtype=np.uint8)
    assert np.array_equal(Capture.from_samples(s, 1e6).to_samples(), s)


def test_pwm_summary():
    ch = analysis.summary(pwm_capture())["channels"][0]
    assert ch["kind"] == "periodic"
    assert ch["frequency_hz"] == pytest.approx(1000, rel=1e-4)
    assert ch["duty"] == pytest.approx(0.25, abs=1e-3)


def test_constant_channel():
    cap = Capture.from_samples(np.full(100, 0b10, dtype=np.uint8), 1e6, channels=[0, 1])
    kinds = [c["kind"] for c in analysis.summary(cap)["channels"]]
    assert kinds == ["constant-low", "constant-high"]


def test_edges_pagination():
    cap = pwm_capture(cycles=5)
    page = analysis.edges(cap, 0, limit=3)
    assert page["total"] == 9 and len(page["edges"]) == 3 and page["next_from_s"] is not None
    assert page["edges"][0]["edge"] == "falling"
    rest = analysis.edges(cap, 0, start_s=page["next_from_s"], limit=100)
    assert rest["offset"] == 3 and rest["next_from_s"] is None


@pytest.mark.parametrize("parity", [None, "even"])
def test_uart(parity):
    w = synth.Wave(10e6)
    w.hold(1e-4)
    synth.uart(w, 0, b"hello\x00\xff", 115200, parity)
    w.hold(1e-4)
    cap = Capture.from_samples(w.samples(), 10e6)
    spec = "uart:rx=0,baud=115200" + (",parity=even" if parity else "")
    frames = decoders.decode(cap, spec)
    assert [f["value"] for f in frames] == list(b"hello\x00\xff")
    assert not any("error" in f for f in frames)


def test_uart_autobaud():
    w = synth.Wave(10e6)
    w.hold(1e-4)
    synth.uart(w, 2, b"U\x55ok", 9600)
    w.hold(1e-3)
    cap = Capture.from_samples(w.samples(), 10e6)
    assert analysis.summary(cap)["channels"][2]["uart_baud_guess"] == 9600
    assert decoders.uart_text(decoders.decode(cap, "uart:rx=2")) == "UUok"


def test_i2c():
    w = synth.Wave(10e6)
    synth.i2c(w, 0, 1, 0x50, 0, [0x12, 0xAB])
    w.hold(1e-5)
    synth.i2c(w, 0, 1, 0x68, 1, [0x01])
    w.hold(1e-5)
    tr = decoders.decode(Capture.from_samples(w.samples(), 10e6), "i2c:scl=0,sda=1")
    assert [(t["addr"], t["rw"], t["data"]) for t in tr] == [("0x50", "W", ["0x12", "0xab"]), ("0x68", "R", ["0x01"])]
    assert all(t["stop"] and t["addr_ack"] for t in tr)


def test_spi():
    w = synth.Wave(10e6, idle=0b100)
    synth.spi_mode0(w, 0, 1, 2, [0xA5, 0x3C])
    synth.spi_mode0(w, 0, 1, 2, [0x01])
    tr = decoders.decode(Capture.from_samples(w.samples(), 10e6), "spi:clk=0,mosi=1,cs=2")
    assert [t["mosi"] for t in tr] == [["0xa5", "0x3c"], ["0x01"]]


@pytest.mark.parametrize("ext", [".sr", ".vcd", ".npz"])
def test_format_roundtrip(tmp_path, ext):
    rng = np.random.default_rng(2)
    s = np.repeat(rng.integers(0, 256, 300, dtype=np.uint8), 7)
    cap = Capture.from_samples(s, 24e6, channels=[0, 3, 7])
    p = tmp_path / f"cap{ext}"
    formats.save(cap, p)
    back = formats.load(p)
    assert back.sample_rate == cap.sample_rate and back.num_samples == cap.num_samples
    assert sorted(back.channels) == [0, 3, 7]
    assert np.array_equal(back.to_samples(), cap.to_samples())


def test_csv(tmp_path):
    p = tmp_path / "c.csv"
    formats.save(pwm_capture(cycles=2), p)
    lines = p.read_text().splitlines()
    assert lines[0] == "time_s,sample,D0" and len(lines) == 2 + 3
