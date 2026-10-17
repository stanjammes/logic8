import numpy as np
import pytest

from logic8 import device


def pack(samples, channels):
    """Inverse of device.unpack: build the EP2 byte stream for uint8 samples."""
    out = bytearray()
    for i in range(0, len(samples), 16):
        blk = samples[i:i + 16]
        for c in channels:
            word = 0
            for s in blk:
                word = word << 1 | int(s) >> c & 1
            out += word.to_bytes(2, "little")
    return bytes(out)


@pytest.mark.parametrize("channels", [list(range(8)), [1, 7], [3]])
def test_unpack_roundtrip(channels):
    rng = np.random.default_rng(3)
    mask = sum(1 << c for c in channels)
    s = rng.integers(0, 256, 16 * 50, dtype=np.uint8) & mask
    assert np.array_equal(device.unpack(pack(s, channels), channels), s)


def test_unpack_real_block():
    # Idle Teensy pattern seen on hardware: D4 and D7 low, others high.
    blk = bytes.fromhex("ffffffffffffffff0000ffffffff0000")
    assert set(device.unpack(blk, list(range(8)))) == {0b01101111}


@pytest.mark.parametrize("rate,expect", [(100e6, (2, 1)), (25e6, (8, 1)), (1e6, (200, 1)),
                                         (781250, (256, 1)), (10e3, (250, 80)), (1e3, (250, 800))])
def test_plan_rate(rate, expect):
    assert device.plan_rate(rate) == expect


@pytest.mark.parametrize("rate", [200e6, 30e6, 123457])
def test_plan_rate_rejects(rate):
    with pytest.raises(ValueError):
        device.plan_rate(rate)


def test_bandwidth_limit():
    device.validate(25e6, range(8))
    device.validate(100e6, [0, 1])
    with pytest.raises(ValueError):
        device.validate(50e6, range(8))


def test_find_trigger():
    s = np.array([0, 0, 1, 1, 0, 1], dtype=np.uint8) << 2
    assert device._find_trigger(s, None, 2, "rising") == 2
    assert device._find_trigger(s, None, 2, "falling") == 4
    assert device._find_trigger(s, np.uint8(4), 2, "falling") == 0  # edge across chunk boundary
    assert device._find_trigger(s, None, 2, "high") == 2
    assert device._find_trigger(s, None, 3, "rising") is None
