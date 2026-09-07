# -*- coding: utf-8 -*-
"""Diagnosis engine: Features -> Rules -> Evidence -> Score -> Findings -> Report data.

The engine NEVER invents values: every finding's evidence pointers come from
the FeatureSet, which in turn points back to raw collection files.
"""

from ..models.diagnosis import (Diagnosis, Finding, RootCauseNode,
                                SEVERITY_FACTOR, score_band)
from ..models.case import DataQuality
from .rules import load_rules_from_dir
from .scoring import aggregate_score, evidence_confidence
from .recommendation import build_recommendation, dedupe_priorities

TYPE_LABEL = {
    "CPU_COMPUTE_BOUND": "CPU Compute Bound",
    "CPU_SCHEDULING_BOUND": "CPU Scheduling Bound",
    "THREADING_BOUND": "Threading Bound",
    "MEMORY_BOUND": "Memory Bound",
    "NUMA_BOUND": "NUMA Bound",
    "IO_BOUND": "IO Bound",
    "SYNC_BOUND": "Synchronization Bound",
    "DATA_PIPELINE_BOUND": "Data Pipeline Bound",
    "HOST_DEVICE_SYNC_BOUND": "Host-Device Synchronization Bound",
    "RUNTIME_BOUND": "Runtime Bound",
    "PYTHON_BOUND": "Python Bound",
    "DEVICE_IDLE_GAP": "Device Idle Gap",
    "SCHEDULER_CONTENTION": "Scheduler Contention",
    "THREAD_OVERSUBSCRIPTION": "Thread Oversubscription",
    "NUMA_REMOTE_ACCESS": "NUMA Remote Access",
    "MEMORY_PRESSURE": "Memory Pressure",
    "CPU_IMBALANCE": "CPU Load Imbalance",
    "IO_SATURATION": "IO Saturation",
    "UNKNOWN": "Unknown",
}

# rule group -> radar dimension
GROUP_TO_DIM = {
    "cpu": "CPU", "memory": "Memory", "numa": "NUMA", "thread": "Thread",
    "scheduler": "Scheduler", "io": "IO", "runtime": "Runtime",
    "dataloader": "Data Pipeline", "device": "Host-Device", "perf": "CPU",
}

SUBTYPE_HINTS = {
    "dataloader": "DataLoader",
    "tokenizer": "Tokenization",
    "preprocess": "Preprocess",
    "runtime": "Runtime",
    "communication": "Communication",
    "memory_ops": "Memory Ops",
}


class DiagnosisEngine(object):
    def __init__(self, rules=None, rules_dir=None):
        if rules is None:
            if rules_dir is None:
                raise ValueError("provide rules or rules_dir")
            rules = load_rules_from_dir(rules_dir)
        self.rules = rules

    # ------------------------------------------------------------------
    def run(self, features, case_info, dq=None):
        dq = dq or DataQuality()
        grade = dq.grade()
        fired = []
        skipped = []

        for rule in self.rules:
            res = rule.evaluate(features)
            if res["error"]:
                dq.warn("规则 %s 求值失败: %s" % (rule.rule_id, res["error"]))
                continue
            if res["fired"] is None:
                skipped.append((rule.rule_id, res["missing"]))
                continue
            if res["fired"]:
                conf = evidence_confidence(rule, features, grade)
                fired.append({"rule": rule, "confidence": conf})

        hb_score = aggregate_score(
            [{"weight": f["rule"].weight, "severity": f["rule"].severity,
              "confidence": f["confidence"], "contribution": f["rule"].contribution}
             for f in fired])

        findings = []
        for i, fr in enumerate(fired, 1):
            rule = fr["rule"]
            evd = []
            for key in rule.condition_features():
                evd.extend(features.evidence(key))
            findings.append(Finding(
                fid="F-%03d" % i, rule_id=rule.rule_id, severity=rule.severity,
                confidence=fr["confidence"], title=rule.name or rule.rule_id,
                evidence=evd,
                impact=self._impact_text(rule, features),
                recommendation=build_recommendation(rule, features),
                diagnosis_type=rule.diagnosis_type,
                statement=rule.statement or rule.name,
            ))
        findings.sort(key=lambda f: f.sort_key(), reverse=True)

        label, _sev = score_band(hb_score)
        diag = Diagnosis()
        diag.host_bound_score = hb_score
        diag.score_label = label
        diag.findings = findings
        diag.overall_from_findings()
        diag.classification = self._classification(findings, features)
        diag.statement = self._statement(hb_score, findings, skipped)
        diag.confidence = self._overall_confidence(findings, grade, hb_score)
        diag.root_cause_tree = self._root_cause_tree(hb_score, findings)
        diag.priorities = dedupe_priorities([f.recommendation for f in findings])
        diag.risk_dimensions = self._risk_dimensions(findings, features)
        return diag

    # ------------------------------------------------------------------
    def _impact_text(self, rule, features):
        pieces = []
        if features.has("device.util_pct"):
            pieces.append("设备利用率 %s%%" % features.get("device.util_pct"))
        if rule.statement:
            pieces.append(rule.statement)
        return "；".join(pieces) or "影响 Host 侧数据供给与设备利用效率。"

    def _classification(self, findings, features):
        if not findings:
            return "Unknown"
        top = findings[0]
        base = TYPE_LABEL.get(top.diagnosis_type, top.diagnosis_type)
        # 子类型提示对"供给缺口"与"CPU 计算瓶颈"均生效：
        # 两者都由 Host 侧热点性质决定细分标签（DataLoader / Tokenization / Runtime ...）
        if top.diagnosis_type in ("CPU_COMPUTE_BOUND", "DEVICE_IDLE_GAP"):
            cat = str(features.get("perf.hotspot_category", "") or "")
            for hint, label in SUBTYPE_HINTS.items():
                if hint in cat.lower():
                    base = base + " / " + label
                    break
            else:
                top_sym = str(features.get("perf.top_symbols", "") or "")
                if top_sym:
                    base = base + " / " + top_sym.split(";")[0][:60]
        return base

    def _statement(self, score, findings, skipped):
        if score >= 80:
            head = "存在明显 Host Bound"
        elif score >= 60:
            head = "存在较明显 Host Bound"
        elif score >= 40:
            head = "疑似存在 Host Bound，建议补充数据进一步验证"
        elif score >= 20:
            head = "存在轻微 Host Bound 迹象"
        else:
            head = "基本不存在 Host Bound"
        if findings:
            tops = "、".join(f.title for f in findings[:3])
            return "%s。主要问题：%s。" % (head, tops)
        if skipped:
            return "%s（部分规则因数据缺失未启用，建议补充采集）。" % head
        return "%s。" % head

    def _overall_confidence(self, findings, grade, score):
        if not findings:
            dq_factor = {"complete": 0.7, "partial": 0.55, "insufficient": 0.35,
                         "unavailable": 0.2}.get(grade, 0.5)
            return dq_factor
        top_conf = max(f.confidence for f in findings[:5])
        dq_factor = {"complete": 1.0, "partial": 0.92, "insufficient": 0.75,
                     "unavailable": 0.5}.get(grade, 0.9)
        # multi-source boost: distinct evidence sources across top findings
        sources = set()
        for f in findings[:5]:
            for e in f.evidence:
                if e.get("source"):
                    sources.add(e["source"])
        multi = min(0.08, 0.02 * max(0, len(sources) - 1))
        return min(0.97, (0.55 + 0.45 * top_conf) * dq_factor + multi)

    def _root_cause_tree(self, hb_score, findings):
        root = RootCauseNode("Host Bound", score=hb_score, detail="多维证据加权评分")
        groups = {}
        for f in findings:
            groups.setdefault(f.diagnosis_type, []).append(f)
        # order groups by best finding (severity, confidence)
        def group_score(fs):
            return max(SEVERITY_FACTOR.get(f.severity, 0) * f.confidence for f in fs)

        for dtype, fs in sorted(groups.items(), key=lambda kv: -group_score(kv[1])):
            label = TYPE_LABEL.get(dtype, dtype)
            node = RootCauseNode(
                label,
                score=100 * group_score(fs),
                confidence=max(f.confidence for f in fs),
                detail="；".join(sorted(set(f.statement for f in fs if f.statement))[:3]),
                children=[RootCauseNode(f.title, score=100 * SEVERITY_FACTOR.get(f.severity, 0) * f.confidence,
                                        confidence=f.confidence, detail=f.impact,
                                        finding_ids=[f.id]) for f in fs],
                finding_ids=[f.id for f in fs],
            )
            root.children.append(node)
        return root

    def _risk_dimensions(self, findings, features):
        dims = {}
        rule_group = {}
        for r in self.rules:
            rule_group[r.rule_id] = r.group
        for f in findings:
            dim = GROUP_TO_DIM.get(rule_group.get(f.rule_id, ""), None)
            if dim is None:
                dim = "Runtime"
            v = 100 * SEVERITY_FACTOR.get(f.severity, 0) * f.confidence
            dims[dim] = max(dims.get(dim, 0.0), v)
        # Host-Device dimension also driven by direct analyzer signal
        if features.has("device.util_pct"):
            util = features.get("device.util_pct")
            if isinstance(util, (int, float)) and util < 70:
                dims["Host-Device"] = max(dims.get("Host-Device", 0.0),
                                          min(100, (70 - util) * 1.5))
        # ensure all radar axes exist (fixed 9-dimension radar)
        for d in ("CPU", "Memory", "NUMA", "Thread", "Scheduler", "IO",
                  "Runtime", "Data Pipeline", "Host-Device"):
            dims.setdefault(d, 0.0)
        return dims
