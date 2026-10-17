"""USB access to the Saleae Logic 8 (21a9:1004).

Boot sequence: the FX2 enumerates with only a VID/PID (no string descriptors).
We load Saleae's FX2 firmware into RAM through the standard 0xA0 vendor request;
the chip renumerates as "Saleae Inc / Logic 8" with the same VID/PID. The FPGA
(Lattice ECP5) bitstream is then bit-banged in slave-serial mode by the FX2 from
raw EP1 chunks; FPGA registers are read/written over an FX2-driven SPI link.

Sample stream (EP2 IN): blocks of one little-endian uint16 per *enabled* channel
(ascending channel order); each word holds 16 consecutive samples, oldest in bit 15.
The stream only survives if transfers are always queued: a gap lets the FPGA FIFO
overflow and the stream stops, so reading is done with several async transfers.
"""
import json
import os
import re
import time
from pathlib import Path

import numpy as np
import usb1

from . import protocol
from .capture import Capture

VID, PID = 0x21A9, 0x1004
CPUCS = 0xE600
EP_DATA = 0x82
BASE_CLOCK = 200e6
MAX_RATE = 100e6
MAX_BITS_PER_S = 200e6    # rate x channels; matches what Logic 2 offers (25 MS/s x 8, 100 MS/s x 2)
BLOB_DIR = Path(os.environ.get("LOGIC8_BLOBS", Path.home() / ".local/share/logic8"))
BOARD_FILE = BLOB_DIR / "board.json"

REG_CONTROL = 0x00        # 1 = run; 4 and 8 are reset pulses used after upload
REG_CHANNEL_MASK = 0x06
REG_DIVIDER = 0x0B        # BASE_CLOCK / rate - 1; only 8 bits work (0x0c/0x0d upper bits misbehave)
MAX_DIVIDER = 256         # slowest hardware rate 781.25 kS/s; lower rates are decimated on the host
REG_CHANNEL_COUNT = 0x0E
REG_FLUSH = 0x13          # 16-bit; Logic 2 uses 0x0400 at >= 25 MS/s and 0x5000 at 1 MS/s
REG_STATUS = 0x40
REG_SCRATCH = 0x7F
STATUS_CONFIGURED = 0xA0  # value of REG_STATUS once the Saleae design is running

# ADC (analog front end) setup Logic 2 sends through regs 3/4/5 after every upload.
ADC_INIT = [(0x00, 0x01, 0x00), (0x0F, 0x00, 0x00), (0x11, 0x77, 0x07), (0x12, 0x44, 0x44),
            (0x2A, 0x11, 0x11), (0x2B, 0x11, 0x11), (0x42, 0x40, 0x80), (0x46, 0x04, 0x82)]


class DeviceError(Exception):
    exit_code = 3


class TriggerTimeout(DeviceError):
    exit_code = 4


_ctx = None


def _context():
    global _ctx
    if _ctx is None:
        _ctx = usb1.USBContext()
        _ctx.open()
    return _ctx


def find(serial=None):
    """Return usb1 device objects for every attached Logic 8."""
    try:
        devs = [d for d in _context().getDeviceIterator(skip_on_error=True)
                if (d.getVendorID(), d.getProductID()) == (VID, PID)]
    except usb1.USBError as e:
        raise DeviceError(f"USB access failed: {e}") from e
    if serial:
        devs = [d for d in devs if describe(d).get("serial") == serial]
    return devs


def _string(fn):
    try:
        return fn() or None
    except usb1.USBError:
        return None


def describe(usbdev):
    product = _string(usbdev.getProduct)
    return {
        "bus": usbdev.getBusNumber(), "address": usbdev.getDeviceAddress(),
        "manufacturer": _string(usbdev.getManufacturer), "product": product,
        "serial": _string(usbdev.getSerialNumber),
        "firmware_loaded": product is not None,
    }


def load_ihex(path):
    """Return a list of (address, bytes) segments from an Intel-hex file."""
    segs = []
    for rec in re.findall(r":([0-9A-Fa-f]+)", Path(path).read_text()):
        b = bytes.fromhex(rec)
        if sum(b) & 0xFF:
            raise ValueError(f"bad checksum in {path}")
        if b[3] == 0:
            segs.append((b[1] << 8 | b[2], b[4:4 + b[0]]))
    return segs


def blobs():
    manifest = BLOB_DIR / "manifest.json"
    if not manifest.exists():
        raise DeviceError(f"no firmware in {BLOB_DIR}; run: logic8 setup <Logic-2.x.y-linux-x64.AppImage>")
    return json.loads(manifest.read_text())


def upload_fx2_firmware(usbdev, ihx=None):
    ihx = ihx or BLOB_DIR / blobs()["firmware"]["file"]
    h = usbdev.open()
    try:
        poke = lambda addr, data: h.controlWrite(0x40, 0xA0, addr, 0, data, 1000)
        poke(CPUCS, b"\x01")                   # hold 8051 in reset
        for addr, data in load_ihex(ihx):
            poke(addr, data)
        poke(CPUCS, b"\x00")                   # run; device renumerates
    finally:
        h.close()


def _reset_context():
    """Drop the libusb context: without udev (containers) it never learns about
    devices that appear later, so re-enumerate with a fresh one."""
    global _ctx
    if _ctx is not None:
        _ctx.close()
        _ctx = None


def wait_for(predicate, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        _reset_context()
        devs = [d for d in find() if predicate(d)]
        if devs:
            return devs[0]
        time.sleep(0.1)
    return None


class Logic8:
    """An opened, firmware-running Logic 8. Provides write/read for protocol.Channel."""

    def __init__(self, usbdev):
        self.usbdev = usbdev
        self.handle = usbdev.open()
        self.handle.claimInterface(0)
        self.ch = protocol.Channel(self)

    def describe(self):
        return describe(self.usbdev)

    def write(self, ep, data, timeout=1000):
        return self.handle.bulkWrite(ep, bytes(data), timeout)

    def read(self, ep, n, timeout=1000):
        try:
            return self.handle.bulkRead(ep, n, timeout)
        except usb1.USBErrorTimeout as e:
            raise DeviceError(f"timeout reading EP{ep & 0x7f}") from e

    # --- FPGA registers -------------------------------------------------

    def write_regs(self, pairs):
        for i in range(0, len(pairs), 28):  # 2 + 2*n bytes must fit a 64-byte packet
            part = pairs[i:i + 28]
            self.ch.command(protocol.CMD_FPGA_WRITE_REGISTER, bytes([len(part)]) + bytes(x for p in part for x in p))

    def read_reg(self, reg):
        return self.ch.command(protocol.CMD_FPGA_READ_REGISTER, bytes([1, reg]), reply_len=1)[0]

    # --- bring-up -------------------------------------------------------

    def _upload_bitstream(self, data):
        self.ch.command(protocol.CMD_FPGA_UPLOAD_INIT)
        for i in range(0, len(data), 61):
            self.ch.fpga_chunk(data[i:i + 61])
        end = time.monotonic() + 2.0
        while time.monotonic() < end:  # the design needs a moment after DONE before it answers
            if self.read_reg(REG_STATUS) == STATUS_CONFIGURED:
                return True
            time.sleep(0.05)
        return False

    def _post_upload_init(self):
        self.write_regs([(REG_SCRATCH, 0xAA)])
        if self.read_reg(REG_SCRATCH) != 0xAA:
            raise DeviceError("FPGA register scratch test failed")
        for a, b, c in ADC_INIT:
            self.write_regs([(3, a), (4, b), (5, c)])
        for pulse in (0x04, 0x08):
            self.write_regs([(REG_CONTROL, pulse), (REG_CONTROL, 0)])
            time.sleep(0.01)

    def configure_fpga(self, force=False):
        """Seed the command channel and make sure the FPGA runs the Logic 8 design.

        The board's ECP5 part is unknown up front; each bitstream checks the IDCODE, so
        we try them in turn and remember the one that works in board.json.
        """
        self.ch.seed()
        if not force and self.read_reg(REG_STATUS) == STATUS_CONFIGURED:
            return {"fpga": "already configured"}
        streams = blobs()["bitstreams"]
        known = json.loads(BOARD_FILE.read_text())["part"] if BOARD_FILE.exists() else None
        streams.sort(key=lambda b: b["part"] != known)
        for b in streams:
            t = time.monotonic()
            if self._upload_bitstream((BLOB_DIR / b["file"]).read_bytes()):
                self._post_upload_init()
                BOARD_FILE.write_text(json.dumps({"part": b["part"]}))
                return {"fpga": "configured", "part": b["part"], "upload_s": round(time.monotonic() - t, 2)}
        raise DeviceError("FPGA did not come up with any Logic 8 bitstream")

    # --- acquisition ----------------------------------------------------

    def _start(self, rate, channels):
        div = round(BASE_CLOCK / rate) - 1
        mask = sum(1 << c for c in channels)
        flush = 0x0400 if rate >= 10e6 else 0x5000
        self.write_regs([(REG_CONTROL, 0), (0x08, 0), (REG_CHANNEL_MASK, mask), (0x0A, 0), (0x1B, 0),
                         (REG_DIVIDER, div), (REG_DIVIDER + 1, 0), (REG_DIVIDER + 2, 0),
                         (REG_CHANNEL_COUNT, len(channels)), (REG_FLUSH, flush & 0xFF), (REG_FLUSH + 1, flush >> 8)])
        self.ch.command(protocol.CMD_ABORT_ACQUISITION)
        self.ch.command(protocol.CMD_START_ACQUISITION)
        self.write_regs([(REG_CONTROL, 1)])

    def _stop(self):
        self.ch.command(protocol.CMD_ABORT_ACQUISITION)
        self.write_regs([(REG_CONTROL, 0)])

    def stream(self, rate, channels, on_data, stall_timeout=1.0, n_transfers=8):
        """Run an acquisition, calling ``on_data(bytes)`` until it returns True."""
        bytes_per_s = rate * len(channels) / 8
        size = int(min(max(bytes_per_s / 30, 512), 816 * 1024)) // 512 * 512 or 512
        state = {"done": False, "last": time.monotonic(), "error": None}

        def callback(t):
            status = t.getStatus()
            if status == usb1.TRANSFER_COMPLETED:
                n = t.getActualLength()
                if n and not state["done"]:
                    state["last"] = time.monotonic()
                    try:
                        state["done"] = bool(on_data(bytes(t.getBuffer()[:n])))
                    except Exception as e:  # surface errors from the consumer in the main loop
                        state["error"], state["done"] = e, True
                if not state["done"]:
                    t.submit()
            elif status not in (usb1.TRANSFER_CANCELLED, usb1.TRANSFER_TIMED_OUT):
                state["error"], state["done"] = DeviceError(f"USB transfer failed (status {status})"), True
            elif status == usb1.TRANSFER_TIMED_OUT and not state["done"]:
                t.submit()

        transfers = []
        for _ in range(n_transfers):
            t = self.handle.getTransfer()
            t.setBulk(EP_DATA, size, callback=callback, timeout=500)
            transfers.append(t)
        for t in transfers:
            t.submit()
        ctx = _context()
        self._start(rate, channels)
        try:
            while not state["done"]:
                ctx.handleEventsTimeout(0.05)
                if time.monotonic() - state["last"] > stall_timeout + 64 * 1024 / bytes_per_s:
                    raise DeviceError("sample stream stopped (FPGA buffer overrun): "
                                      "try a lower rate or fewer channels")
        finally:
            self._stop()
            for t in transfers:
                try:
                    t.cancel()
                except usb1.USBError:
                    pass
            end = time.monotonic() + 1.0
            while any(t.isSubmitted() for t in transfers) and time.monotonic() < end:
                ctx.handleEventsTimeout(0.05)
        if state["error"]:
            raise state["error"]


def open_device(serial=None):
    """Find a Logic 8, loading FX2 firmware if needed. Returns an opened Logic8."""
    devs = find(serial)
    if not devs:
        raise DeviceError("no Saleae Logic 8 (21a9:1004) found")
    usbdev = devs[0]
    if not describe(usbdev)["firmware_loaded"]:
        old = (usbdev.getBusNumber(), usbdev.getDeviceAddress())
        upload_fx2_firmware(usbdev)
        # The renumerated device gets a new address; in a container its node can take
        # a while to appear, so wait generously.
        usbdev = wait_for(lambda d: (d.getBusNumber(), d.getDeviceAddress()) != old
                          and describe(d)["firmware_loaded"], timeout=30.0)
        if usbdev is None:
            seen = [describe(d) for d in find()]
            hint = " (device visible but not openable: check USB permissions/passthrough)" if seen else ""
            raise DeviceError(f"device did not come back after FX2 firmware upload{hint}")
    return Logic8(usbdev)


# --- sample stream decoding ------------------------------------------------

def unpack(data, channels):
    """EP2 bytes (whole blocks) -> uint8 samples with bit c = channel c."""
    n = len(channels)
    words = np.frombuffer(data, dtype=np.uint8).reshape(-1, n, 2)[:, :, ::-1]  # big-endian: oldest sample first
    bits = np.unpackbits(words, axis=2)                                          # [block, channel, 16]
    bits = bits.transpose(0, 2, 1).reshape(-1, n)                                # [sample, channel]
    out = np.zeros(len(bits), dtype=np.uint8)
    for i, c in enumerate(channels):
        out |= bits[:, i] << c
    return out


def _find_trigger(samples, prev, channel, kind):
    """Index of the first trigger match in ``samples`` (prev = sample before them, or None)."""
    bits = (samples >> channel) & 1
    if kind in ("high", "low"):
        hits = np.flatnonzero(bits == (kind == "high"))
    else:
        before = np.concatenate(([bits[0] if prev is None else (prev >> channel) & 1], bits[:-1]))
        edge = {"rising": (before == 0) & (bits == 1), "falling": (before == 1) & (bits == 0),
                "either": before != bits}[kind]
        hits = np.flatnonzero(edge)
    return int(hits[0]) if len(hits) else None


def plan_rate(rate):
    """Return (hardware divider k, decimation m) with BASE_CLOCK / (k * m) == rate."""
    d = BASE_CLOCK / rate
    if rate > MAX_RATE or d < 2 or abs(d - round(d)) > 1e-6:
        raise ValueError(f"sample rate must be 200 MHz / n (n >= 2, e.g. 100M, 50M, 25M, 10M, 1M, 10k); got {rate:g}")
    d = round(d)
    for k in range(min(d, MAX_DIVIDER), 1, -1):  # largest hardware divider = least USB traffic
        if d % k == 0:
            return k, d // k
    raise ValueError(f"cannot make {rate:g} S/s from 200 MHz with a divider <= {MAX_DIVIDER}; pick a rounder rate")


def validate(rate, channels):
    k, _ = plan_rate(rate)
    if BASE_CLOCK / k * len(channels) > MAX_BITS_PER_S:
        raise ValueError(f"{rate / 1e6:g} MS/s x {len(channels)} channels exceeds USB bandwidth "
                         f"(rate x channels <= {MAX_BITS_PER_S / 1e6:g}M)")


def capture(dev, sample_rate, num_samples, channels, trigger=None, trigger_timeout=5.0):
    """Capture ``num_samples`` samples. With a trigger, ``trigger["pre"]`` of them precede it."""
    channels = sorted(channels)
    validate(sample_rate, channels)
    if trigger and trigger["channel"] not in channels:
        raise ValueError(f"trigger channel D{trigger['channel']} is not among the captured channels")
    k, decim = plan_rate(sample_rate)
    block = 2 * len(channels)
    pre = int(num_samples * trigger["pre"]) if trigger else 0
    st = {"rest": b"", "chunks": [], "have": 0, "trig": None, "prev": None, "phase": 0, "t0": time.monotonic()}

    def on_data(data):
        data = st["rest"] + data
        cut = len(data) // block * block
        st["rest"] = data[cut:]
        if not cut:
            return False
        s = unpack(data[:cut], channels)
        if decim > 1:
            n = len(s)
            s = s[st["phase"]::decim]
            st["phase"] = (st["phase"] - n) % decim
            if not len(s):
                return False
        if trigger and st["trig"] is None:
            hit = _find_trigger(s, st["prev"], trigger["channel"], trigger["type"])
            st["prev"] = s[-1]
            if hit is None:
                st["chunks"].append(s)
                st["have"] += len(s)
                while st["chunks"] and st["have"] - len(st["chunks"][0]) >= pre:  # keep only pre-trigger history
                    st["have"] -= len(st["chunks"].pop(0))
                if time.monotonic() - st["t0"] > trigger_timeout:
                    raise TriggerTimeout(f"no {trigger['type']} on D{trigger['channel']} within {trigger_timeout:g}s")
                return False
            st["trig"] = st["have"] + hit
        st["chunks"].append(s)
        st["have"] += len(s)
        need = (st["trig"] - pre + num_samples) if trigger else num_samples
        return st["have"] >= need

    dev.stream(BASE_CLOCK / k, channels, on_data)
    samples = np.concatenate(st["chunks"]) if st["chunks"] else np.zeros(0, np.uint8)
    start = max(st["trig"] - pre, 0) if trigger else 0
    samples = samples[start:start + num_samples]
    trig = st["trig"] - start if trigger else None
    return Capture.from_samples(samples, sample_rate, channels, trigger_sample=trig)
