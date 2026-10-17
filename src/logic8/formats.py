"""Capture file formats, chosen by extension.

- ``.sr``  sigrok session (zip). Opens in PulseView; ``sigrok-cli -i`` can decode it.
- ``.vcd`` value change dump. Opens in GTKWave/PulseView/most HDL tools.
- ``.npz`` native: transition arrays, lossless and compact.
- ``.csv`` one row per transition (write only).
"""
import configparser
import io
import re
import zipfile
from pathlib import Path

import numpy as np

from .capture import Capture, Channel

SR_CHUNK = 4 << 20
SR_TRIGGER = "logic8-trigger"  # trigger sample number; sigrok's format has no field for it


def save(cap, path):
    path = Path(path)
    writer = {".sr": save_sr, ".vcd": save_vcd, ".npz": save_npz, ".csv": save_csv}.get(path.suffix.lower())
    if not writer:
        raise ValueError(f"unknown capture format {path.suffix!r} (use .sr, .vcd, .npz or .csv)")
    writer(cap, path)


def load(path):
    path = Path(path)
    reader = {".sr": load_sr, ".vcd": load_vcd, ".npz": load_npz}.get(path.suffix.lower())
    if not reader:
        raise ValueError(f"cannot read {path.suffix!r} captures (use .sr, .vcd or .npz)")
    return reader(path)


def rate_string(hz):
    for div, unit in ((1e9, "GHz"), (1e6, "MHz"), (1e3, "kHz")):
        if hz >= div and hz % div == 0:
            return f"{int(hz // div)} {unit}"
    return f"{int(hz)} Hz"


def parse_rate(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*([kKmMgG]?)(?:Hz|hz)?\s*", text)
    if not m:
        raise ValueError(f"bad sample rate {text!r}")
    return float(m.group(1)) * {"": 1, "k": 1e3, "m": 1e6, "g": 1e9}[m.group(2).lower()]


# --- sigrok session ---------------------------------------------------------

def save_sr(cap, path):
    nbits = max(cap.channels) + 1 if cap.channels else 1
    lines = ["[global]", "sigrok version=0.5.2", "", "[device 1]", "capturefile=logic-1",
             f"total probes={nbits}", f"samplerate={rate_string(cap.sample_rate)}", "total analog=0"]
    lines += [f"probe{i + 1}=D{i}" for i in range(nbits) if i in cap.channels]
    lines.append("unitsize=1")
    samples = cap.to_samples()
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("version", "2")
        z.writestr("metadata", "\n".join(lines) + "\n")
        if cap.trigger_sample is not None:
            z.writestr(SR_TRIGGER, str(cap.trigger_sample))  # extra member; sigrok ignores it
        for n, off in enumerate(range(0, max(len(samples), 1), SR_CHUNK)):
            z.writestr(f"logic-1-{n + 1}", samples[off:off + SR_CHUNK].tobytes())


def load_sr(path):
    with zipfile.ZipFile(path) as z:
        meta = configparser.ConfigParser(interpolation=None)
        meta.read_string(z.read("metadata").decode())
        dev = meta["device 1"]
        unitsize = int(dev.get("unitsize", "1"))
        base = dev.get("capturefile", "logic-1")
        names = [n for n in z.namelist() if n == base or n.startswith(base + "-")]
        names.sort(key=lambda n: int(n.rsplit("-", 1)[1]) if n != base else 0)
        raw = b"".join(z.read(n) for n in names)
        trig = int(z.read(SR_TRIGGER)) if SR_TRIGGER in z.namelist() else None
    data = np.frombuffer(raw, dtype=np.uint8).reshape(-1, unitsize)[:, 0]  # channels 0-7
    probes = [int(k[5:]) - 1 for k in dev if re.fullmatch(r"probe\d+", k)]
    return Capture.from_samples(data, parse_rate(dev["samplerate"]), [p for p in probes if p < 8], trig)


# --- VCD --------------------------------------------------------------------

def _vcd_timescale(rate):
    for unit, per_s in (("1 ns", 1e9), ("1 ps", 1e12)):
        if (per_s / rate).is_integer():
            return unit, per_s / rate
    return "1 ps", per_s / rate


def save_vcd(cap, path):
    unit, ticks = _vcd_timescale(cap.sample_rate)
    ids = {ch: chr(33 + i) for i, ch in enumerate(sorted(cap.channels))}
    events = sorted((t, ch, lvl) for ch, c in cap.channels.items() for t, lvl in c.edges())
    with open(path, "w") as f:
        trig = "" if cap.trigger_sample is None else f" trigger={cap.trigger_sample}"
        f.write(f"$comment samplerate={cap.sample_rate:g} samples={cap.num_samples}{trig} $end\n")
        f.write(f"$timescale {unit} $end\n$scope module logic8 $end\n")
        for ch, i in ids.items():
            f.write(f"$var wire 1 {i} D{ch} $end\n")
        f.write("$upscope $end\n$enddefinitions $end\n#0\n")
        for ch, i in ids.items():
            f.write(f"{cap.channels[ch].initial}{i}\n")
        last = 0
        for t, ch, lvl in events:
            if t != last:
                f.write(f"#{round(t * ticks)}\n")
                last = t
            f.write(f"{lvl}{ids[ch]}\n")
        f.write(f"#{round(cap.num_samples * ticks)}\n")


def load_vcd(path):
    text = Path(path).read_text()
    m = re.search(r"\$comment samplerate=(\S+) samples=(\d+)(?: trigger=(\d+))?", text)
    ts = re.search(r"\$timescale\s+(\d+)\s*(s|ms|us|ns|ps|fs)\s*\$end", text)
    tick_s = int(ts.group(1)) * {"s": 1, "ms": 1e-3, "us": 1e-6, "ns": 1e-9, "ps": 1e-12, "fs": 1e-15}[ts.group(2)] if ts else 1e-9
    rate = float(m.group(1)) if m else 1 / tick_s
    ids = {}
    for vid, name in re.findall(r"\$var\s+\w+\s+1\s+(\S+)\s+(\S+)", text):
        num = re.search(r"(\d+)$", name)
        ids[vid] = int(num.group(1)) if num else len(ids)
    body = text.split("$enddefinitions", 1)[1].split("$end", 1)[1]
    level = {}
    initial = {}
    trans = {ch: [] for ch in ids.values()}
    t = 0
    for tok in body.split():
        if tok.startswith("#"):
            t = round(int(tok[1:]) * tick_s * rate)
        elif tok[0] in "01xXzZ" and tok[1:] in ids:
            ch, v = ids[tok[1:]], 1 if tok[0] == "1" else 0
            if ch not in level:
                initial[ch] = level[ch] = v
            elif v != level[ch]:
                trans[ch].append(t)
                level[ch] = v
    n = int(m.group(2)) if m else t
    cap = Capture(rate, n, trigger_sample=int(m.group(3)) if m and m.group(3) else None)
    for ch in ids.values():
        cap.channels[ch] = Channel(ch, initial.get(ch, 0), np.array(trans[ch], dtype=np.int64))
    return cap


# --- native / csv -----------------------------------------------------------

def save_npz(cap, path):
    arrays = {f"ch{ch}": c.transitions for ch, c in cap.channels.items()}
    np.savez_compressed(path, sample_rate=cap.sample_rate, num_samples=cap.num_samples,
                        trigger_sample=-1 if cap.trigger_sample is None else cap.trigger_sample,
                        initial=np.array([[ch, c.initial] for ch, c in cap.channels.items()]), **arrays)


def load_npz(path):
    z = np.load(path)
    trig = int(z["trigger_sample"])
    cap = Capture(float(z["sample_rate"]), int(z["num_samples"]), trigger_sample=None if trig < 0 else trig)
    for ch, init in z["initial"]:
        cap.channels[int(ch)] = Channel(int(ch), int(init), z[f"ch{ch}"].astype(np.int64))
    return cap


def save_csv(cap, path):
    chans = sorted(cap.channels)
    level = {ch: cap.channels[ch].initial for ch in chans}
    events = sorted((t, ch) for ch, c in cap.channels.items() for t in c.transitions.tolist())
    buf = io.StringIO()
    buf.write("time_s,sample," + ",".join(f"D{c}" for c in chans) + "\n")
    buf.write("0,0," + ",".join(str(level[c]) for c in chans) + "\n")
    i = 0
    while i < len(events):
        t = events[i][0]
        while i < len(events) and events[i][0] == t:
            level[events[i][1]] ^= 1
            i += 1
        buf.write(f"{cap.time(t):.9g},{t}," + ",".join(str(level[c]) for c in chans) + "\n")
    Path(path).write_text(buf.getvalue())
