# -*- coding: utf-8 -*-
"""Case / collection level models: manifest, data quality."""
import os

from .. import VERSION_BLOCK


class DataQuality(object):
    """Per-source completeness tracking. Never assumes data is complete."""

    # grade levels
    COMPLETE = "complete"
    PARTIAL = "partial"
    INSUFFICIENT = "insufficient"
    UNAVAILABLE = "unavailable"

    def __init__(self):
        self.sources = {}      # name -> status: present|missing|partial|error
        self.warnings = []     # parser/analyzer warnings
        self.files = {}        # relpath -> {"type":..., "status":..., "note":...}

    def mark(self, name, status, note=""):
        self.sources[name] = {"status": status, "note": note}

    def warn(self, msg):
        self.warnings.append(str(msg))

    def grade(self):
        present = [k for k, v in self.sources.items() if v["status"] == "present"]
        missing = [k for k, v in self.sources.items() if v["status"] in ("missing", "error")]
        core = {"cpu", "mem", "process", "thread", "scheduler"}
        have_core = core.intersection(present)
        if len(present) == 0 and len(have_core) == 0:
            return self.UNAVAILABLE
        if not missing and len(have_core) >= 3:
            return self.COMPLETE
        if len(have_core) >= 2:
            return self.PARTIAL
        return self.INSUFFICIENT

    def limitations(self):
        lim = []
        for k, v in sorted(self.sources.items()):
            if v["status"] in ("missing", "error"):
                lim.append("数据源缺失: %s (%s)" % (k, v["note"] or v["status"]))
        for w in self.warnings[:20]:
            lim.append("警告: %s" % w)
        return lim

    def to_dict(self):
        return {"sources": self.sources, "warnings": self.warnings,
                "grade": self.grade(), "limitations": self.limitations()}


class CaseInfo(object):
    """Aggregated view of the analyzed collection."""

    def __init__(self):
        self.case_id = ""
        self.collection_path = ""
        self.hostname = ""
        self.os = ""
        self.kernel = ""
        self.arch = ""
        self.cpu_model = ""
        self.cpu_count = 0
        self.sockets = 0
        self.numa_nodes = 0
        self.collection_time = ""
        self.duration = 0
        self.tool_version = ""
        self.sanitized = False
        self.capabilities = {}
        self.env_keys = {}       # interesting env vars (OMP_NUM_THREADS ...)
        self.frameworks = []
        self.device = ""

    @classmethod
    def from_manifest(cls, manifest, collection_path):
        c = cls()
        c.collection_path = collection_path
        if not isinstance(manifest, dict):
            return c
        c.case_id = manifest.get("case_id") or os.path.basename(collection_path)
        c.hostname = manifest.get("hostname", "")
        c.os = manifest.get("os", "")
        c.kernel = manifest.get("kernel", "")
        c.arch = manifest.get("arch", "")
        c.cpu_model = manifest.get("cpu", manifest.get("cpu_model", ""))
        c.cpu_count = manifest.get("cpu_count", 0)
        c.sockets = manifest.get("sockets", 0)
        c.numa_nodes = manifest.get("numa_nodes", 0)
        c.collection_time = manifest.get("collection_time", "")
        c.duration = manifest.get("duration", 0)
        c.tool_version = manifest.get("tool_version", "")
        c.sanitized = manifest.get("sanitized", False)
        c.capabilities = manifest.get("capabilities", {})
        c.env_keys = manifest.get("env_keys", {})
        c.frameworks = manifest.get("frameworks", [])
        c.device = manifest.get("device", "")
        return c

    def to_dict(self):
        d = {
            "case_id": self.case_id, "collection_path": self.collection_path,
            "hostname": self.hostname, "os": self.os, "kernel": self.kernel,
            "arch": self.arch, "cpu_model": self.cpu_model,
            "cpu_count": self.cpu_count, "sockets": self.sockets,
            "numa_nodes": self.numa_nodes, "collection_time": self.collection_time,
            "duration_s": self.duration, "tool_version": self.tool_version,
            "sanitized": self.sanitized, "capabilities": self.capabilities,
            "env_keys": self.env_keys, "frameworks": self.frameworks,
            "device": self.device,
        }
        d["analysis_versions"] = dict(VERSION_BLOCK)
        return d
