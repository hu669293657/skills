# -*- coding: utf-8 -*-
"""Evidence: traceable pointer back to source file + line range + snippet."""


class Evidence(object):
    __slots__ = ("metric", "value", "unit", "source", "lines", "snippet", "note")

    def __init__(self, metric, value=None, unit="", source="", lines="", snippet="", note=""):
        self.metric = metric
        self.value = value
        self.unit = unit
        self.source = source          # relative path inside collection
        self.lines = lines            # e.g. "12-62"
        self.snippet = snippet        # short raw text excerpt
        self.note = note

    def to_dict(self):
        return {"metric": self.metric, "value": self.value, "unit": self.unit,
                "source": self.source, "lines": self.lines,
                "snippet": self.snippet[:400], "note": self.note}

    @classmethod
    def from_dict(cls, d):
        return cls(d.get("metric", ""), d.get("value"), d.get("unit", ""),
                   d.get("source", ""), d.get("lines", ""), d.get("snippet", ""),
                   d.get("note", ""))
