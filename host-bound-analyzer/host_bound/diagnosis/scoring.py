# -*- coding: utf-8 -*-
"""Host Bound scoring: weighted, evidence-aware, 0-100."""

from ..models.diagnosis import SEVERITY_FACTOR
from ..util.stats import clamp

# Reference denominator so that a small but severe rule set can still reach
# high scores; prevents both inflation (all-weak rules) and deflation.
W_REF_MIN = 3.0

# The score answers "how bad is the worst evidence", so only the TOP-K most
# severe contributions move the score; long tails of weak rules must not
# dilute strong ones.
TOP_K = 5


def aggregate_score(fired_rules):
    """fired_rules: list of dicts {weight, severity, confidence, contribution}.

    Only rules with contribution == 'host_bound' move the Host Bound Score.
    Score = sum(top-K contributions) / max(sum(top-K weights), W_REF_MIN).
    Returns score in [0, 100].
    """
    contribs = []
    for r in fired_rules:
        if str(r.get("contribution", "host_bound")).lower() != "host_bound":
            continue
        w = float(r.get("weight", 1.0))
        f = SEVERITY_FACTOR.get(str(r.get("severity", "MEDIUM")).upper(), 0.5)
        c = float(r.get("confidence", 0.7))
        contribs.append((w * f * c, w))
    contribs.sort(key=lambda x: x[0], reverse=True)
    top = contribs[:TOP_K]
    raw = sum(cv for cv, _w in top)
    weights = sum(w for _cv, w in top)
    denom = max(weights, W_REF_MIN)
    return clamp(100.0 * raw / denom, 0.0, 100.0)


def evidence_confidence(rule, features, dq_grade):
    """Per-rule confidence from evidence source diversity + data quality.

    rule.explicit_confidence (if given) is the base; otherwise base comes from
    the number of distinct source files backing the rule's features.
    """
    src_files = set()
    for key in rule.condition_features():
        for ev in features.evidence(key):
            if ev.get("source"):
                src_files.add(ev["source"])

    if rule.explicit_confidence is not None:
        try:
            base = float(rule.explicit_confidence)
        except (TypeError, ValueError):
            base = 0.7
    else:
        base = 0.55 + min(0.25, 0.08 * len(src_files))

    dq_factor = {"complete": 1.0, "partial": 0.9, "insufficient": 0.7,
                 "unavailable": 0.45}.get(dq_grade, 0.9)
    return clamp(base * dq_factor, 0.05, 0.97)
