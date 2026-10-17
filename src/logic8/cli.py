"""logic8: agent-friendly command line for the Saleae Logic 8.

Every command prints one JSON document on stdout. Errors print
{"error": ..., "code": n} on stderr and exit with that code:
  2 usage, 3 no device / USB error, 4 trigger timeout, 5 not implemented yet, 1 other.
"""
import argparse
import json
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

from . import __version__, analysis, decoders, device, extract, formats


class UsageError(Exception):
    exit_code = 2


def parse_si(text, unit=""):
    m = re.fullmatch(rf"\s*([\d.]+)\s*([numkKMG]?){unit}\s*", text)
    if not m:
        raise UsageError(f"bad value {text!r}")
    scale = {"": 1, "n": 1e-9, "u": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "G": 1e9}[m.group(2)]
    return float(m.group(1)) * scale


def parse_duration(text):
    m = re.fullmatch(r"\s*([\d.]+)\s*(ns|us|ms|s)\s*", text)
    if not m:
        raise UsageError(f"bad duration {text!r} (e.g. 200ms, 1.5s, 50us)")
    return float(m.group(1)) * {"ns": 1e-9, "us": 1e-6, "ms": 1e-3, "s": 1}[m.group(2)]


def parse_channels(text):
    chans = set()
    for part in text.split(","):
        a, _, b = part.partition("-")
        chans.update(range(int(a), int(b or a) + 1))
    if not chans or min(chans) < 0 or max(chans) > 7:
        raise UsageError("channels must be within 0-7")
    return sorted(chans)


def parse_trigger(text):
    m = re.fullmatch(r"(?:ch|d)?(\d):(rising|falling|either|high|low)", text.strip().lower())
    if not m:
        raise UsageError(f"bad trigger {text!r} (e.g. ch0:falling, 3:high)")
    return {"channel": int(m.group(1)), "type": m.group(2)}


def emit(obj):
    print(json.dumps(obj, indent=2 if sys.stdout.isatty() else None))


# --- commands ---------------------------------------------------------------

def cmd_devices(a):
    emit({"devices": [device.describe(d) for d in device.find()]})


def cmd_setup(a):
    emit(extract.extract(a.appimage, a.lib, a.out))


def cmd_init(a):
    dev = device.open_device(a.serial)
    fpga = dev.configure_fpga(force=a.force)
    emit(dev.describe() | fpga)


def do_capture(a):
    rate = parse_si(a.rate, "(?:S/s|Hz|S|sps)?")
    if a.samples:
        n = int(parse_si(a.samples))
    else:
        n = int(round(parse_duration(a.duration) * rate))
    channels = parse_channels(a.channels)
    try:
        device.validate(rate, channels)
    except ValueError as e:
        raise UsageError(str(e)) from e
    dev = device.open_device(a.serial)
    dev.configure_fpga()
    return device.capture(dev, rate, n, channels,
                          trigger=parse_trigger(a.trigger) | {"pre": a.pre} if a.trigger else None,
                          trigger_timeout=parse_duration(a.trigger_timeout))


def cmd_capture(a):
    cap = do_capture(a)
    formats.save(cap, a.output)
    out = {"file": str(a.output), "sample_rate_hz": cap.sample_rate, "samples": cap.num_samples,
           "duration_s": analysis.sig(cap.duration)}
    if a.summary:
        out["summary"] = analysis.summary(cap)
    emit(out)


def cmd_watch(a):
    cap = do_capture(a)
    if a.output:
        formats.save(cap, a.output)
    emit(analysis.summary(cap) | ({"file": str(a.output)} if a.output else {}))


def cmd_summary(a):
    cap = formats.load(a.file)
    s = analysis.summary(cap)
    if a.channels:
        keep = set(parse_channels(a.channels))
        s["channels"] = [c for c in s["channels"] if c["channel"] in keep]
    emit(s)


def cmd_edges(a):
    emit(analysis.edges(formats.load(a.file), a.channel, start_s=parse_duration(a.start) if a.start else 0.0,
                        limit=a.limit))


def cmd_decode(a):
    if a.sigrok:
        return sigrok_decode(a)
    cap = formats.load(a.file)
    try:
        frames = decoders.decode(cap, a.spec)
    except (ValueError, KeyError) as e:
        raise UsageError(str(e)) from e
    out = {"decoder": a.spec, "count": len(frames), "frames": frames[:a.limit], "truncated": len(frames) > a.limit}
    if decoders.parse_spec(a.spec)[0] == "uart":
        out["text"] = decoders.uart_text(frames)
    emit(out)


def sigrok_decode(a):
    exe = shutil.which("sigrok-cli")
    if not exe:
        raise UsageError("sigrok-cli not installed (only needed for --sigrok decoders)")
    src = Path(a.file)
    with tempfile.TemporaryDirectory() as tmp:
        if src.suffix != ".sr":
            conv = Path(tmp) / "cap.sr"
            formats.save(formats.load(src), conv)
            src = conv
        r = subprocess.run([exe, "-i", str(src), "-P", a.spec, "--protocol-decoder-samplenum"],
                           capture_output=True, text=True)
    if r.returncode:
        raise UsageError(r.stderr.strip() or "sigrok-cli failed")
    lines = r.stdout.splitlines()
    emit({"decoder": a.spec, "via": "sigrok-cli", "count": len(lines), "lines": lines[:a.limit],
          "truncated": len(lines) > a.limit})


def cmd_convert(a):
    cap = formats.load(a.input)
    formats.save(cap, a.output)
    emit({"input": str(a.input), "output": str(a.output), "samples": cap.num_samples})


# --- parser -----------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog="logic8", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("devices", help="list attached Logic 8 devices")
    s.set_defaults(fn=cmd_devices)

    s = sub.add_parser("setup", help="extract the firmware from a Logic 2 AppImage (once)")
    s.add_argument("appimage", nargs="?", type=Path, help="Logic-2.x.y-linux-x64.AppImage (unpacked, never run)")
    s.add_argument("--lib", type=Path, help="use an already-unpacked libgraph_server_shared.so")
    s.add_argument("--out", type=Path, help="output directory (default ~/.local/share/logic8 or $LOGIC8_BLOBS)")
    s.set_defaults(fn=cmd_setup)

    s = sub.add_parser("init", help="load FX2 firmware and FPGA bitstream")
    s.add_argument("--serial")
    s.add_argument("--force", action="store_true", help="re-upload the bitstream even if the FPGA is running")
    s.set_defaults(fn=cmd_init)

    def capture_args(s, need_output):
        s.add_argument("-r", "--rate", default="25M", help="sample rate: 200M/n, e.g. 100M, 50M, 25M, 10M, 1M, 10k (default 25M); rate x channels <= 200M")
        g = s.add_mutually_exclusive_group()
        g.add_argument("-d", "--duration", default="100ms", help="capture length, e.g. 200ms (default 100ms)")
        g.add_argument("-n", "--samples", help="capture length in samples, e.g. 1M")
        s.add_argument("-c", "--channels", default="0-7", help="e.g. 0-3 or 0,2,5 (default 0-7)")
        s.add_argument("-t", "--trigger", help="ch<N>:rising|falling|either|high|low")
        s.add_argument("--pre", type=float, default=0.1, help="pre-trigger fraction (default 0.1)")
        s.add_argument("--trigger-timeout", default="5s")
        s.add_argument("--serial")
        s.add_argument("-o", "--output", type=Path, required=need_output,
                       help="capture file: .sr (PulseView), .vcd, .npz, .csv")

    s = sub.add_parser("capture", help="capture to a file")
    capture_args(s, True)
    s.add_argument("--summary", action="store_true", help="include per-channel summary in the output")
    s.set_defaults(fn=cmd_capture)

    s = sub.add_parser("watch", help="capture and summarise in one step")
    capture_args(s, False)
    s.set_defaults(fn=cmd_watch)

    s = sub.add_parser("summary", help="per-channel statistics of a capture file")
    s.add_argument("file", type=Path)
    s.add_argument("-c", "--channels")
    s.set_defaults(fn=cmd_summary)

    s = sub.add_parser("edges", help="list transitions on one channel")
    s.add_argument("file", type=Path)
    s.add_argument("-c", "--channel", type=int, required=True)
    s.add_argument("--from", dest="start", help="start time, e.g. 1.5ms")
    s.add_argument("--limit", type=int, default=100)
    s.set_defaults(fn=cmd_edges)

    s = sub.add_parser("decode", help="decode a protocol",
                       description="Built-in: uart:rx=0,baud=115200[,parity=even,bits=8,invert=1]  "
                                   "i2c:scl=0,sda=1  spi:clk=0,mosi=1[,miso=2,cs=3,mode=0,bits=8,order=msb]. "
                                   "With --sigrok, SPEC is a sigrok-cli -P string, e.g. can:can_rx=D0:bitrate=500000")
    s.add_argument("file", type=Path)
    s.add_argument("spec")
    s.add_argument("--limit", type=int, default=500)
    s.add_argument("--sigrok", action="store_true", help="decode with sigrok-cli (any of its 100+ decoders)")
    s.set_defaults(fn=cmd_decode)

    s = sub.add_parser("convert", help="convert between capture formats")
    s.add_argument("input", type=Path)
    s.add_argument("output", type=Path)
    s.set_defaults(fn=cmd_convert)
    return p


def main(argv=None):
    p = build_parser()
    a = p.parse_args(argv)
    try:
        a.fn(a)
    except (UsageError, device.DeviceError) as e:
        msg, code = str(e), e.exit_code
    except KeyError as e:
        msg, code = str(e.args[0]), 2
    except (FileNotFoundError, ValueError) as e:
        msg, code = str(e), 2
    except Exception as e:  # unexpected: still report as JSON
        usb_err = type(e).__module__.startswith("usb")
        msg, code = (f"USB: {e}", 3) if usb_err else (f"{type(e).__name__}: {e}", 1)
    else:
        return 0
    print(json.dumps({"error": msg, "code": code}), file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
