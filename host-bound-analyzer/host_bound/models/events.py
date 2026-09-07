# -*- coding: utf-8 -*-
"""Unified event model. Every time-series source normalizes into Events."""


class Event(object):
    """A single normalized timeline event / sample.

    ts       : seconds relative to collection window start (float) or epoch
    duration : seconds (0.0 for instant samples)
    event    : e.g. cpu_sample / thread_snapshot / ctx_switch / io_sample /
               device_sample / perf_stat / host_span / device_span / log_marker
    pid/tid/cpu/node : optional ints
    source   : relative file path inside the collection (evidence back-link)
    metrics  : dict of named values
    """

    __slots__ = ("ts", "duration", "event", "pid", "tid", "cpu", "node", "source", "metrics")

    def __init__(self, ts, event, duration=0.0, pid=None, tid=None, cpu=None,
                 node=None, source="", metrics=None):
        self.ts = ts
        self.duration = duration
        self.event = event
        self.pid = pid
        self.tid = tid
        self.cpu = cpu
        self.node = node
        self.source = source
        self.metrics = metrics or {}

    def to_dict(self):
        return {
            "ts": self.ts, "duration": self.duration, "event": self.event,
            "pid": self.pid, "tid": self.tid, "cpu": self.cpu, "node": self.node,
            "source": self.source, "metrics": self.metrics,
        }

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("ts", 0.0), d.get("event", ""), d.get("duration", 0.0),
                   d.get("pid"), d.get("tid"), d.get("cpu"), d.get("node"),
                   d.get("source", ""), d.get("metrics") or {})


class Timeline(object):
    """Ordered event list with convenience accessors."""

    def __init__(self):
        self.events = []

    def add(self, ev):
        self.events.append(ev)

    def by_kind(self, kind):
        return [e for e in self.events if e.event == kind]

    def series(self, kind, key):
        """Extract [(ts, value)] for events of `kind` carrying metrics[key]."""
        out = []
        for e in self.events:
            if e.event == kind and key in (e.metrics or {}):
                v = e.metrics[key]
                if isinstance(v, (int, float)):
                    out.append((e.ts, v))
        out.sort(key=lambda x: x[0])
        return out

    def window(self):
        """(t_min, t_max) or (None, None)."""
        if not self.events:
            return None, None
        ts = [e.ts for e in self.events]
        return min(ts), max(ts)

    def merge(self, other):
        self.events.extend(other.events)
        self.events.sort(key=lambda e: e.ts)
        return self
