# -*- coding: utf-8 -*-
"""Rule engine: YAML rule loading + condition DSL evaluation.

Condition DSL: identifiers are FeatureSet keys; operators: > >= < <= == != ;
logic: and / or / not ; parentheses allowed; numeric and string literals allowed.

Missing features do NOT falsify a rule — the rule is SKIPPED and the missing
keys are reported (fail-safe against incomplete collections).
"""

import os
import re

from . import miniyaml


class ConditionError(ValueError):
    pass


class MissingFeatures(Exception):
    def __init__(self, keys):
        super(MissingFeatures, self).__init__(",".join(keys))
        self.keys = keys


# --------------------------------------------------------------------------
# Tokenizer
# --------------------------------------------------------------------------
_TOKEN_RE = re.compile(r"""
    \s*(?:
        (?P<num>\d+\.\d+|\.\d+|\d+)
      | (?P<str>'[^']*'|"[^"]*")
      | (?P<op>>=|<=|==|!=|>|<|\(|\))
      | (?P<ident>[A-Za-z_][A-Za-z0-9_\.]*)
    )""", re.VERBOSE)

_OPS = {">", ">=", "<", "<=", "==", "!="}


def tokenize(cond):
    tokens = []
    pos = 0
    while pos < len(cond):
        m = _TOKEN_RE.match(cond, pos)
        if not m:
            if cond[pos:].strip() == "":
                break
            raise ConditionError("bad token at: %r" % cond[pos:pos + 20])
        pos = m.end()
        if m.group("num") is not None:
            v = m.group("num")
            tokens.append(("num", float(v) if "." in v else int(v)))
        elif m.group("str") is not None:
            tokens.append(("str", m.group("str")[1:-1]))
        elif m.group("op") is not None:
            tokens.append(("op", m.group("op")))
        else:
            ident = m.group("ident")
            low = ident.lower()
            if low in ("and", "or", "not"):
                tokens.append(("logic", low))
            elif low in ("true", "false"):
                tokens.append(("bool", low == "true"))
            else:
                tokens.append(("ident", ident))
    return tokens


# --------------------------------------------------------------------------
# AST evaluation
# --------------------------------------------------------------------------
def _eval(node, features, missing):
    kind = node[0]
    if kind == "or":
        left_ok = True
        # evaluate both sides to collect all missing keys
        lv = _eval(node[1], features, missing)
        rv = _eval(node[2], features, missing)
        return lv or rv
    if kind == "and":
        lv = _eval(node[1], features, missing)
        rv = _eval(node[2], features, missing)
        return lv and rv
    if kind == "not":
        return not _eval(node[1], features, missing)
    if kind == "cmp":
        lv = _eval(node[1], features, missing)
        rv = _eval(node[2], features, missing)
        op = node[3]
        if lv is None or rv is None:
            return False
        try:
            if op == ">":
                return lv > rv
            if op == ">=":
                return lv >= rv
            if op == "<":
                return lv < rv
            if op == "<=":
                return lv <= rv
            if op == "==":
                return lv == rv
            if op == "!=":
                return lv != rv
        except TypeError:
            return False
        return False
    if kind == "ident":
        key = node[1]
        try:
            return features.eval_value(key)
        except KeyError:
            missing.add(key)
            return None
    if kind in ("num", "str", "bool"):
        return node[1]
    raise ConditionError("unknown AST node: %r" % (node,))


def _parse_or(tokens, i):
    node, i = _parse_and(tokens, i)
    while i < len(tokens) and tokens[i] == ("logic", "or"):
        rhs, i = _parse_and(tokens, i + 1)
        node = ("or", node, rhs)
    return node, i


def _parse_and(tokens, i):
    node, i = _parse_not(tokens, i)
    while i < len(tokens) and tokens[i] == ("logic", "and"):
        rhs, i = _parse_not(tokens, i + 1)
        node = ("and", node, rhs)
    return node, i


def _parse_not(tokens, i):
    if i < len(tokens) and tokens[i] == ("logic", "not"):
        inner, i = _parse_not(tokens, i + 1)
        return ("not", inner), i
    return _parse_cmp(tokens, i)


def _parse_cmp(tokens, i):
    left, i = _parse_primary(tokens, i)
    if i < len(tokens) and tokens[i][0] == "op" and tokens[i][1] in _OPS:
        op = tokens[i][1]
        right, i = _parse_primary(tokens, i + 1)
        return ("cmp", left, right, op), i
    return left, i


def _parse_primary(tokens, i):
    if i >= len(tokens):
        raise ConditionError("unexpected end of condition")
    kind, val = tokens[i]
    if kind == "op" and val == "(":
        node, i = _parse_or(tokens, i + 1)
        if i >= len(tokens) or tokens[i] != ("op", ")"):
            raise ConditionError("missing closing parenthesis")
        return node, i + 1
    if kind in ("num", "str", "bool"):
        return (kind, val), i + 1
    if kind == "ident":
        return ("ident", val), i + 1
    raise ConditionError("unexpected token: %r" % ((kind, val),))


def parse_condition(cond):
    if not isinstance(cond, str) or not cond.strip():
        raise ConditionError("condition must be a non-empty string")
    tokens = tokenize(cond)
    if not tokens:
        raise ConditionError("empty condition")
    node, i = _parse_or(tokens, 0)
    if i != len(tokens):
        raise ConditionError("trailing tokens in condition: %r" % (tokens[i:],))
    return node


def evaluate_condition(ast, features):
    """Returns (fired: bool|None, missing: set). None means 'cannot evaluate'."""
    missing = set()
    try:
        val = _eval(ast, features, missing)
    except ConditionError:
        raise
    if missing:
        return None, sorted(missing)
    return bool(val), []


# --------------------------------------------------------------------------
# Rule model and loading
# --------------------------------------------------------------------------
class Rule(object):
    def __init__(self, raw, source=""):
        self.raw = raw
        self.source = source
        self.rule_id = str(raw.get("rule_id", "")).strip()
        self.name = str(raw.get("name", "")).strip()
        self.group = str(raw.get("group", "other")).strip()
        self.condition_text = raw.get("condition", "")
        self.weight = float(raw.get("weight", 1.0))
        self.severity = str(raw.get("severity", "MEDIUM")).upper()
        self.contribution = str(raw.get("contribution", "host_bound")).lower()
        diag = raw.get("diagnosis") or {}
        self.diagnosis_type = str(diag.get("type", "UNKNOWN"))
        self.statement = str(diag.get("statement", ""))
        rec = raw.get("recommendation") or {}
        self.recommendation = {
            "level": str(rec.get("level", "P2")),
            "certainty": str(rec.get("certainty", "建议实验验证")),
            "action": str(rec.get("action", "")),
            "expected": str(rec.get("expected", "")),
            "verify": str(rec.get("verify", "")),
            "risk": str(rec.get("risk", "")),
        }
        self.impact_hint = float(raw.get("impact_hint", 0.0))  # optional for root-cause ordering
        self.explicit_confidence = raw.get("confidence", None)  # optional 0..1
        self._ast = parse_condition(self.condition_text)
        if self.severity not in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO"):
            raise ConditionError("invalid severity: %s" % self.severity)

    def condition_features(self):
        """Collect all feature keys referenced by the condition AST."""
        keys = set()

        def walk(node):
            if not isinstance(node, tuple):
                return
            if node[0] == "ident":
                keys.add(node[1])
            elif node[0] in ("or", "and"):
                walk(node[1]); walk(node[2])
            elif node[0] == "not":
                walk(node[1])
            elif node[0] == "cmp":
                walk(node[1]); walk(node[2])

        walk(self._ast)
        return sorted(keys)

    def evaluate(self, features):
        """Returns dict with fired / missing / error."""
        try:
            fired, missing = evaluate_condition(self._ast, features)
        except ConditionError as e:
            return {"fired": None, "missing": [], "error": str(e)}
        return {"fired": fired, "missing": missing, "error": None}


def load_rules_from_dir(rules_dir):
    """Load all *.yaml rule files. Invalid files raise; caller decides fallback."""
    rules = []
    if not os.path.isdir(rules_dir):
        return rules
    for fn in sorted(os.listdir(rules_dir)):
        if not fn.endswith((".yaml", ".yml")):
            continue
        path = os.path.join(rules_dir, fn)
        data = miniyaml.parse_file(path)
        if isinstance(data, dict) and data.get("rule_id"):
            rules.append(Rule(data, source=fn))
        elif isinstance(data, list):
            for item in data:
                if isinstance(item, dict) and item.get("rule_id"):
                    rules.append(Rule(item, source=fn))
    return rules


def check_rules(rules):
    """Validate all rules parse; returns list of error strings."""
    errs = []
    for r in rules:
        try:
            parse_condition(r.condition_text)
        except ConditionError as e:
            errs.append("%s(%s): %s" % (r.rule_id, r.source, e))
    return errs
