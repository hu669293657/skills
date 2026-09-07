# -*- coding: utf-8 -*-
"""Diagnosis-level models: Finding / RootCauseNode / Recommendation / Diagnosis."""

SEVERITY_ORDER = {"CRITICAL": 5, "HIGH": 4, "MEDIUM": 3, "LOW": 2, "INFO": 1}

SEVERITY_FACTOR = {"CRITICAL": 1.0, "HIGH": 0.85, "MEDIUM": 0.6, "LOW": 0.35, "INFO": 0.15}

# Score bands for Host Bound Score interpretation.
SCORE_BANDS = [
    (0, 20, "基本不存在", "LOW"),
    (20, 40, "轻微", "LOW"),
    (40, 60, "疑似", "MEDIUM"),
    (60, 80, "较明显", "HIGH"),
    (80, 100, "强 Host Bound", "CRITICAL"),
]


def score_band(score):
    for lo, hi, label, sev in SCORE_BANDS:
        if lo <= score <= hi:
            return label, sev
    return "未知", "INFO"


class Recommendation(object):
    def __init__(self, level="P2", action="", expected="", verify="", certainty="建议实验验证",
                 risk=""):
        self.level = level            # P0/P1/P2/P3
        self.action = action
        self.expected = expected
        self.verify = verify
        self.certainty = certainty    # 明确建议 | 建议实验验证
        self.risk = risk

    def to_dict(self):
        return {"level": self.level, "action": self.action, "expected": self.expected,
                "verify": self.verify, "certainty": self.certainty, "risk": self.risk}


class Finding(object):
    def __init__(self, fid, rule_id, severity, confidence, title, evidence, impact,
                 recommendation, diagnosis_type=None, statement=""):
        self.id = fid
        self.rule_id = rule_id
        self.severity = severity
        self.confidence = confidence      # 0..1
        self.title = title
        self.evidence = evidence          # list[Evidence.to_dict()]
        self.impact = impact
        self.recommendation = recommendation  # Recommendation
        self.diagnosis_type = diagnosis_type
        self.statement = statement

    def sort_key(self):
        return (SEVERITY_ORDER.get(self.severity, 0), self.confidence)

    def to_dict(self):
        return {
            "id": self.id, "rule_id": self.rule_id, "severity": self.severity,
            "confidence": round(self.confidence, 3), "title": self.title,
            "evidence": self.evidence, "impact": self.impact,
            "recommendation": self.recommendation.to_dict(),
            "diagnosis_type": self.diagnosis_type, "statement": self.statement,
        }


class RootCauseNode(object):
    def __init__(self, name, score=0.0, confidence=0.0, detail="", children=None, finding_ids=None):
        self.name = name
        self.score = score            # impact score 0..100
        self.confidence = confidence
        self.detail = detail
        self.children = children or []
        self.finding_ids = finding_ids or []

    def to_dict(self):
        return {
            "name": self.name, "score": round(self.score, 1),
            "confidence": round(self.confidence, 3), "detail": self.detail,
            "children": [c.to_dict() for c in self.children],
            "finding_ids": self.finding_ids,
        }


class Diagnosis(object):
    def __init__(self):
        self.status = "INFO"                  # overall severity
        self.host_bound_score = 0.0           # 0..100
        self.score_label = "基本不存在"
        self.confidence = 0.0                 # 0..1
        self.classification = "Unknown"       # e.g. "CPU Compute Bound / DataLoader"
        self.statement = ""
        self.findings = []                    # list[Finding]
        self.root_cause_tree = None           # RootCauseNode
        self.priorities = []                  # list[Recommendation.to_dict()]
        self.risk_dimensions = {}             # radar: dim -> 0..100

    def sort_findings(self):
        self.findings.sort(key=lambda f: f.sort_key(), reverse=True)

    def overall_from_findings(self):
        if not self.findings:
            self.status = "INFO"
            return
        top = max(SEVERITY_ORDER.get(f.severity, 0) for f in self.findings)
        rev = {v: k for k, v in SEVERITY_ORDER.items()}
        self.status = rev.get(top, "INFO")

    def to_dict(self):
        return {
            "status": self.status,
            "host_bound_score": round(self.host_bound_score, 1),
            "score_label": self.score_label,
            "confidence": round(self.confidence, 3),
            "classification": self.classification,
            "statement": self.statement,
            "findings": [f.to_dict() for f in self.findings],
            "root_cause_tree": self.root_cause_tree.to_dict() if self.root_cause_tree else None,
            "priorities": self.priorities,
            "risk_dimensions": self.risk_dimensions,
        }
