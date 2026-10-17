"""Built-in protocol decoders working directly on transition lists.

Each decoder takes a Capture plus options and returns a list of JSON-ready dicts.
Spec strings look like ``uart:rx=0,baud=115200`` (see ``parse_spec``).
"""
import numpy as np

from .analysis import channel_summary, sig


def parse_spec(spec):
    name, _, rest = spec.partition(":")
    opts = {}
    for item in filter(None, rest.split(",")):
        k, _, v = item.partition("=")
        opts[k.strip()] = v.strip()
    return name.strip().lower(), opts


def decode(cap, spec):
    name, opts = parse_spec(spec)
    fn = DECODERS.get(name)
    if not fn:
        raise ValueError(f"unknown decoder {name!r} (have {', '.join(DECODERS)})")
    return fn(cap, **opts)


def _byte_repr(b):
    return chr(b) if 32 <= b < 127 else None


def _union_times(cap, chans):
    arrays = [cap.channel(c).transitions for c in chans]
    return np.unique(np.concatenate(arrays)) if arrays else np.array([], dtype=np.int64)


# --- UART -------------------------------------------------------------------

def uart(cap, rx=None, ch=None, baud=None, bits="8", parity="none", invert="0"):
    chan = int(rx if rx is not None else ch if ch is not None else 0)
    if baud is None:
        baud = channel_summary(cap, chan).get("uart_baud_guess")
        if not baud:
            raise ValueError("could not guess baud rate; pass baud=")
    baud, nbits = float(baud), int(bits)
    inv = invert in ("1", "true", "yes")
    par = parity.lower()[0]
    c = cap.channel(chan)
    bit = cap.sample_rate / baud
    level = lambda s: c.level_at(int(s)) ^ inv
    start_level = 0
    frames = []
    trans = c.transitions
    i = 0
    while i < len(trans):
        s = int(trans[i])
        if level(s) != start_level or level(s - 1) == start_level:
            i += 1
            continue
        if level(s + bit / 2) != start_level:  # glitch, not a start bit
            i += 1
            continue
        val = 0
        for b in range(nbits):
            val |= level(s + (1.5 + b) * bit) << b
        pos = 1 + nbits
        frame = {"t_s": sig(cap.time(s), 9), "value": val, "hex": f"0x{val:02x}", "char": _byte_repr(val)}
        if par in "eo":
            pbit = level(s + (pos + 0.5) * bit)
            ones = bin(val).count("1") + pbit
            if (ones % 2 == 0) != (par == "e"):
                frame["error"] = "parity"
            pos += 1
        if level(s + (pos + 0.5) * bit) != 1 - start_level:
            frame["error"] = "framing"
        frames.append(frame)
        end = s + (pos + 0.5) * bit  # middle of the stop bit
        i = int(np.searchsorted(trans, end, side="right"))  # resync on the next start edge
    return frames


def uart_text(frames):
    return "".join(chr(f["value"]) for f in frames if f["value"] < 256 and "error" not in f)


# --- I2C --------------------------------------------------------------------

def i2c(cap, scl="0", sda="1"):
    scl, sda = int(scl), int(sda)
    C, D = cap.channel(scl), cap.channel(sda)
    out = []
    cur = None
    bits = []
    for t in _union_times(cap, [scl, sda]).tolist():
        c0, d0, c1, d1 = C.level_at(t - 1), D.level_at(t - 1), C.level_at(t), D.level_at(t)
        if c0 and c1 and d0 != d1:
            if d1 == 0:  # START or repeated START
                if cur:
                    out.append(cur)
                cur = {"t_s": sig(cap.time(t), 9), "repeated_start": cur is not None and "stop" not in cur,
                       "addr": None, "rw": None, "data": [], "acks": []}
                bits = []
            else:        # STOP
                if cur:
                    cur["stop"] = True
                    out.append(cur)
                cur, bits = None, []
            continue
        if cur is not None and not c0 and c1:  # SCL rising: sample SDA
            bits.append(d1)
            if len(bits) == 9:
                byte = int("".join(map(str, bits[:8])), 2)
                ack = bits[8] == 0
                if cur["addr"] is None:
                    cur["addr"], cur["rw"] = f"0x{byte >> 1:02x}", "R" if byte & 1 else "W"
                    cur["addr_ack"] = ack
                else:
                    cur["data"].append(f"0x{byte:02x}")
                    cur["acks"].append(ack)
                bits = []
    if cur:
        cur["incomplete"] = True
        out.append(cur)
    for tr in out:
        tr.setdefault("stop", False)
    return out


# --- SPI --------------------------------------------------------------------

def spi(cap, clk="0", mosi=None, miso=None, cs=None, mode="0", bits="8", order="msb", cs_active="0"):
    clk, nbits, mode = int(clk), int(bits), int(mode)
    cpol, cpha = mode >> 1, mode & 1
    sample_rising = cpol == cpha
    lines = {k: int(v) for k, v in (("mosi", mosi), ("miso", miso)) if v is not None}
    if not lines:
        raise ValueError("give mosi= and/or miso=")
    cs_ch = int(cs) if cs is not None else None
    act = int(cs_active)
    K = cap.channel(clk)
    S = cap.channel(cs_ch) if cs_ch is not None else None
    chans = [clk] + ([cs_ch] if cs_ch is not None else [])
    out, cur, acc, nb = [], None, {k: 0 for k in lines}, 0

    def flush(t):
        nonlocal cur, acc, nb
        if cur and (any(cur[k] for k in lines) or nb):
            if nb:
                cur["partial_bits"] = nb
            cur["end_s"] = sig(cap.time(t), 9)
            out.append(cur)
        cur, acc, nb = None, {k: 0 for k in lines}, 0

    for t in _union_times(cap, chans).tolist():
        if S is not None and S.level_at(t - 1) != S.level_at(t):
            if S.level_at(t) == act:
                cur = {"t_s": sig(cap.time(t), 9), **{k: [] for k in lines}}
            else:
                flush(t)
            continue
        k0, k1 = K.level_at(t - 1), K.level_at(t)
        if k0 == k1 or (k1 == 1) != sample_rising:
            continue
        if S is not None and S.level_at(t) != act:
            continue
        if cur is None:
            cur = {"t_s": sig(cap.time(t), 9), **{k: [] for k in lines}}
        for k, ch in lines.items():
            b = cap.channel(ch).level_at(t)
            acc[k] = (acc[k] << 1 | b) if order == "msb" else acc[k] | (b << nb)
        nb += 1
        if nb == nbits:
            for k in lines:
                cur[k].append(f"0x{acc[k]:0{(nbits + 3) // 4}x}")
            acc, nb = {k: 0 for k in lines}, 0
    flush(cap.num_samples)
    return out


DECODERS = {"uart": uart, "i2c": i2c, "spi": spi}
