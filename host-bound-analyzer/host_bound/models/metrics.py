# -*- coding: utf-8 -*-
"""FeatureSet: the ONLY interface between analyzers and the diagnosis engine.

Every feature carries its own evidence pointers, so any diagnosis can be
traced: finding -> rule -> feature -> evidence -> source file -> raw data.
"""


class FeatureSet(object):
    def __init__(self):
        self._features = {}   # key -> {"value","unit","evidence":[Evidence],"note"}
        self.series_store = {}  # kind -> {"key":[(ts,val)]} for charts

    # -- basic -------------------------------------------------------------
    def set(self, key, value, unit="", evidence=None, note=""):
        self._features[key] = {
            "value": value,
            "unit": unit,
            "evidence": [e.to_dict() for e in (evidence or []) if e is not None],
            "note": note,
        }

    def add_evidence(self, key, evidence):
        if key in self._features and evidence is not None:
            self._features[key]["evidence"].append(evidence.to_dict())

    def get(self, key, default=None):
        f = self._features.get(key)
        return default if f is None else f["value"]

    def has(self, key):
        f = self._features.get(key)
        return f is not None and f["value"] is not None

    def unit(self, key):
        f = self._features.get(key)
        return f["unit"] if f else ""

    def evidence(self, key):
        f = self._features.get(key)
        return [e for e in (f["evidence"] if f else [])]

    def keys(self):
        return sorted(self._features.keys())

    def to_dict(self):
        return dict(self._features)

    # -- series (for charts) -----------------------------------------------
    def add_series(self, kind, key, points):
        """points: [(ts, value)]"""
        store = self.series_store.setdefault(kind, {})
        store.setdefault(key, []).extend(points)

    def get_series(self, kind, key):
        return self.series_store.get(kind, {}).get(key, [])

    def series_kinds(self):
        return sorted(self.series_store.keys())

    def series_dump(self):
        """时序扁平化导出：{"kind.key": [(ts, val), ...]}（报告/analysis.json 用）。"""
        out = {}
        for kind, keys in self.series_store.items():
            for key, pts in keys.items():
                out["%s.%s" % (kind, key)] = list(pts)
        return out

    # -- DSL evaluation support --------------------------------------------
    def eval_value(self, key):
        """Used by the rule condition DSL; missing feature -> KeyError."""
        if key not in self._features:
            raise KeyError(key)
        return self._features[key]["value"]
