"""Per-channel statistics: the compact view of a capture that an agent reads first."""
import numpy as np

STANDARD_BAUDS = [300, 1200, 2400, 4800, 9600, 14400, 19200, 28800, 38400, 57600, 76800,
                  115200, 230400, 250000, 460800, 500000, 921600, 1000000, 1500000, 2000000, 3000000]


def sig(x, digits=6):
    """Round to significant digits so JSON stays readable."""
    return float(f"{x:.{digits}g}") if x is not None else None


def guess_baud(min_pulse_s):
    if not min_pulse_s:
        return None
    est = 1 / min_pulse_s
    best = min(STANDARD_BAUDS, key=lambda b: abs(b - est) / b)
    return best if abs(best - est) / best < 0.06 else None


def channel_summary(cap, ch):
    c = cap.channel(ch)
    t = c.transitions
    n = len(t)
    final = c.initial ^ (n & 1)
    out = {"channel": ch, "initial": c.initial, "final": final, "transitions": n}
    if n == 0:
        out["kind"] = f"constant-{'high' if c.initial else 'low'}"
        out["high_fraction"] = float(c.initial)
        return out

    # Segment lengths between transitions (including the partial first/last).
    bounds = np.concatenate(([0], t, [cap.num_samples]))
    widths = np.diff(bounds)
    levels = (c.initial + np.arange(len(widths))) & 1
    out["high_fraction"] = sig(widths[levels == 1].sum() / cap.num_samples, 4)
    out["first_edge_s"] = sig(cap.time(int(t[0])))
    out["last_edge_s"] = sig(cap.time(int(t[-1])))

    inner = widths[1:-1]  # complete pulses only
    inner_lv = levels[1:-1]
    if len(inner):
        hi, lo = inner[inner_lv == 1], inner[inner_lv == 0]
        out["min_pulse_s"] = sig(inner.min() / cap.sample_rate)
        if len(hi):
            out["high_pulse_s"] = {"min": sig(hi.min() / cap.sample_rate), "max": sig(hi.max() / cap.sample_rate)}
        if len(lo):
            out["low_pulse_s"] = {"min": sig(lo.min() / cap.sample_rate), "max": sig(lo.max() / cap.sample_rate)}

    rising = t[(c.initial + np.arange(1, n + 1)) & 1 == 1]
    if len(rising) >= 3:
        periods = np.diff(rising)
        med = float(np.median(periods))
        spread = (periods.max() - periods.min()) / med
        quantised = periods.max() - periods.min() <= 2  # +-1 sample of jitter from sampling alone
        if spread < 0.05 or quantised:
            # Duty from the high time following each rising edge; averages beat the
            # median when the period is only a few samples long.
            highs = [w for w, lv in zip(inner, inner_lv) if lv == 1]
            avg = (lambda x: float(np.mean(x))) if quantised else (lambda x: float(np.median(x)))
            med = avg(periods)
            out["kind"] = "periodic"
            out["frequency_hz"] = sig(cap.sample_rate / med)
            out["period_s"] = sig(med / cap.sample_rate)
            out["duty"] = sig(avg(highs) / med, 4) if highs else None
            out["period_jitter"] = sig(spread, 3)
            return out
    out["kind"] = "irregular"
    baud = guess_baud(out.get("min_pulse_s"))
    if baud and n >= 4:
        out["uart_baud_guess"] = baud
    return out


def summary(cap):
    return {
        "sample_rate_hz": cap.sample_rate,
        "samples": cap.num_samples,
        "duration_s": sig(cap.duration),
        "trigger_s": sig(cap.time(cap.trigger_sample)) if cap.trigger_sample is not None else None,
        "channels": [channel_summary(cap, ch) for ch in sorted(cap.channels)],
    }


def edges(cap, ch, start_s=0.0, limit=100):
    """Transitions on one channel from ``start_s``, paginated."""
    c = cap.channel(ch)
    first = int(np.searchsorted(c.transitions, round(start_s * cap.sample_rate)))
    rows = []
    level = c.initial ^ (first & 1)
    for t in c.transitions[first:first + limit]:
        level ^= 1
        rows.append({"t_s": sig(cap.time(int(t)), 9), "sample": int(t), "edge": "rising" if level else "falling"})
    more = first + limit < len(c.transitions)
    return {"channel": ch, "total": len(c.transitions), "offset": first, "edges": rows,
            "next_from_s": sig(cap.time(int(c.transitions[first + limit])), 9) if more else None}
