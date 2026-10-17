"""Plain-text rendering of command results (the default; --json prints the dicts as-is).

Kept compact and stable for agents too: one record per line, fixed columns with
"-" for missing values, and ASCII units the CLI accepts back (us, ms, MS/s).
"""
_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1, ""), (1e-3, "m"), (1e-6, "u"), (1e-9, "n")]


def si(x, unit="", digits=4, sep=None):
    """1000 -> '1 kHz', 8.68e-6 -> '8.68 us', 2.5e6 -> '2.5M'. sep defaults to ' ' when there is a unit."""
    if x is None:
        return "-"
    sep = (" " if unit else "") if sep is None else sep
    if x == 0:
        return f"0{sep}{unit}"
    prefixes = _PREFIXES[3:] if unit == "s" else _PREFIXES  # no kiloseconds
    i = next((i for i, (scale, _) in enumerate(prefixes) if abs(x) >= scale), len(prefixes) - 1)
    v = float(f"{x / prefixes[i][0]:.{digits}g}")
    if abs(v) >= 1000 and i > 0:  # rounding carried over, e.g. 999.96k -> 1M
        i -= 1
        v = float(f"{x / prefixes[i][0]:.{digits}g}")
    return f"{v:g}{sep}{prefixes[i][1]}{unit}"


def pct(x, digits=1):
    return "-" if x is None else f"{x * 100:.{digits}f}%"


def table(rows, headers, right=""):
    """Align columns; `right` lists the header names to right-align."""
    cells = [[("-" if v is None or v == "" else str(v)) for v in r] for r in rows]
    widths = [max([len(h)] + [len(r[i]) for r in cells]) for i, h in enumerate(headers)]

    def line(vals):
        return "  ".join(v.rjust(w) if h in right else v.ljust(w)
                         for v, w, h in zip(vals, widths, headers)).rstrip()

    return "\n".join([line(headers)] + [line(r) for r in cells])


def span(lo, hi):
    lo, hi = si(lo, "s", sep=""), si(hi, "s", sep="")
    return lo if lo == hi else f"{lo}..{hi}"


def kv(obj):
    return "\n".join(f"{k}: {'-' if v is None else v}" for k, v in obj.items())


# --- per command ------------------------------------------------------------

def capture_line(d):
    s = f"{si(d['sample_rate_hz'], 'S/s')}  {si(d['samples'])} samples  {si(d['duration_s'], 's')}"
    if d.get("trigger_s") is not None:
        s += f"  trigger @ {si(d['trigger_s'], 's', 6)}"
    return s


def _notes(c):
    n = []
    if c["kind"] == "periodic":
        if c.get("period_jitter"):
            n.append(f"jitter={c['period_jitter'] * 100:.2g}%")
        return " ".join(n)
    if c["kind"] != "irregular":
        return ""
    if c.get("uart_baud_guess"):
        n.append(f"uart~{c['uart_baud_guess']}")
    n.append(f"high={pct(c['high_fraction'])}")
    if c.get("min_pulse_s") is not None:
        n.append(f"min_pulse={si(c['min_pulse_s'], 's', sep='')}")
    for key in ("high_pulse_s", "low_pulse_s"):
        if key in c:
            n.append(f"{key[:-2]}={span(c[key]['min'], c[key]['max'])}")
    n.append(f"edges@{span(c['first_edge_s'], c['last_edge_s'])}")
    n.append(f"level={c['initial']}->{c['final']}")
    return " ".join(n)


def channel_table(channels):
    rows = [[f"D{c['channel']}", c["kind"], si(c.get("frequency_hz"), "Hz"), pct(c.get("duty")),
             si(c.get("period_s"), "s"), c["transitions"], _notes(c)] for c in channels]
    return table(rows, ["ch", "kind", "freq", "duty", "period", "edges", "notes"], right="duty edges")


def summary(d):
    return capture_line(d) + "\n" + channel_table(d["channels"])


def watch(d):
    return summary(d) + (f"\nsaved {d['file']}" if d.get("file") else "")


def capture(d):
    out = f"saved {d['file']}  {capture_line(d)}"
    if "summary" in d:
        out += "\n" + channel_table(d["summary"]["channels"])
    return out


def edges(d):
    rows = d["edges"]
    head = f"D{d['channel']}: {d['total']} edges"
    if rows:
        head += f", showing {d['offset']}-{d['offset'] + len(rows) - 1}"
    elif d["total"] and d["offset"] >= d["total"]:
        head += ", none from here"
    out = [head]
    if rows:
        out.append(table([[si(r["t_s"], "s", 9), r["sample"], r["edge"]] for r in rows],
                         ["t", "sample", "edge"], right="t sample"))
    if d.get("next_from_s") is not None:
        out.append(f"more: --from {si(d['next_from_s'], 's', 9, sep='')}")
    return "\n".join(out)


def _hexes(xs):
    return " ".join(x.removeprefix("0x") for x in xs) if xs else "-"


def _uart(d):
    out = [f"text: {ascii(d['text'])}"] if "text" in d else []
    rows = [[si(f["t_s"], "s", 9), f["hex"], f["char"], f.get("error")] for f in d["frames"]]
    if rows:
        out.append(table(rows, ["t", "hex", "char", "error"], right="t"))
    return out


def _i2c(d):
    rows = []
    for f in d["frames"]:
        flags = []
        if not f.get("addr_ack", True):
            flags.append("addr-nack")
        nacks = [str(i) for i, a in enumerate(f["acks"]) if not a]
        if nacks:
            flags.append("nack@" + ",".join(nacks))
        if f["repeated_start"]:
            flags.append("rstart")
        if f.get("incomplete"):
            flags.append("incomplete")
        elif not f["stop"]:
            flags.append("no-stop")
        rows.append([si(f["t_s"], "s", 9), f["addr"], f["rw"], " ".join(flags), _hexes(f["data"])])
    return [table(rows, ["t", "addr", "rw", "flags", "data"], right="t")] if rows else []


def _spi(d):
    lines = [k for k in ("mosi", "miso") if d["frames"] and k in d["frames"][0]]
    rows = [[si(f["t_s"], "s", 9), si(f.get("end_s"), "s", 9),
             f"partial_bits={f['partial_bits']}" if "partial_bits" in f else ""] + [_hexes(f[k]) for k in lines]
            for f in d["frames"]]
    return [table(rows, ["t", "end", "flags"] + lines, right="t end")] if rows else []


def _generic(frames):
    return [" ".join(f"{k}={v}" for k, v in f.items()) for f in frames]


def decode(d):
    if d.get("via") == "sigrok-cli":
        out = list(d["lines"])
        noun = "line"
    else:
        kind = d["decoder"].partition(":")[0]
        out = {"uart": _uart, "i2c": _i2c, "spi": _spi}.get(kind, lambda d: _generic(d["frames"]))(d)
        noun = "frame"
    shown = len(d.get("frames", d.get("lines", [])))
    foot = f"{d['count']} {noun}{'' if d['count'] == 1 else 's'}"
    if d["truncated"]:
        foot += f", showing first {shown} (raise --limit)"
    return "\n".join(out + [foot])


def devices(d):
    if not d["devices"]:
        return "no Logic 8 found"
    return "\n".join(f"bus {x['bus']} addr {x['address']}  {x['manufacturer'] or '-'} {x['product'] or '-'}  "
                     f"serial={x['serial'] or '-'}  firmware={'yes' if x['firmware_loaded'] else 'no'}"
                     for x in d["devices"])


def setup(d):
    out = [f"wrote {d['output']}", f"source {d['source']}",
           f"firmware {d['firmware']['file'] if d['firmware'] else '-'}"]
    if d["bitstreams"]:
        out.append(table([[b["part"], b["size"], b["file"]] for b in d["bitstreams"]],
                         ["part", "size", "file"], right="size"))
    return "\n".join(out)


def convert(d):
    return f"{d['input']} -> {d['output']}  ({si(d['samples'])} samples)"


RENDERERS = {"summary": summary, "watch": watch, "capture": capture, "edges": edges, "decode": decode,
             "devices": devices, "setup": setup, "init": kv, "convert": convert}


def render(cmd, obj):
    return RENDERERS.get(cmd, kv)(obj)
