# -*- coding: utf-8 -*-
"""Small statistical helpers (standard library only)."""

import math


def mean(values):
    vals = [v for v in values if v is not None]
    if not vals:
        return None
    return sum(vals) / float(len(vals))


def pctl(values, p):
    """Nearest-rank percentile, p in [0, 100]."""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    if len(vals) == 1:
        return vals[0]
    k = max(0, min(len(vals) - 1, int(math.ceil(len(vals) * p / 100.0)) - 1))
    return vals[k]


def stdev(values):
    vals = [v for v in values if v is not None]
    n = len(vals)
    if n < 2:
        return 0.0
    m = sum(vals) / float(n)
    var = sum((v - m) ** 2 for v in vals) / (n - 1)
    return math.sqrt(var)


def cv(values):
    """Coefficient of variation; 0 when mean is ~0."""
    m = mean(values)
    if m is None or abs(m) < 1e-9:
        return 0.0
    return stdev(values) / m


def rate(counter_series):
    """Convert an increasing counter series [(t, v), ...] into per-second rates."""
    out = []
    for i in range(1, len(counter_series)):
        t0, v0 = counter_series[i - 1]
        t1, v1 = counter_series[i]
        dt = t1 - t0
        if dt > 0 and v1 >= v0:
            out.append((t1, (v1 - v0) / float(dt)))
    return out


def clamp(x, lo, hi):
    return max(lo, min(hi, x))
