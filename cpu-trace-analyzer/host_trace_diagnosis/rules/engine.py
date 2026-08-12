"""
Rule Engine
===========
Three-layer condition evaluation engine for Host Trace diagnosis.

Evaluates YAML-defined rules against FeatureVector instances using:
1. Scalar threshold conditions (Layer 1)
2. Temporal pattern detection (Layer 2)
3. Correlation evidence matching (Layer 3)

Each rule accumulates weighted confidence; rules exceeding their
confidence_threshold are returned as matched.

Usage::

    engine = RuleEngine(config)
    matched = engine.evaluate(feature_vector)

YAML rule files are loaded from the ``rules_dir`` specified in config.
Each file may contain a single rule (top-level mapping) or multiple
rules (top-level sequence).
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ir.schema import FeatureVector

logger = logging.getLogger("host_trace_diagnosis.rules")

# ---------------------------------------------------------------------------
# YAML graceful degradation
# ---------------------------------------------------------------------------
try:
    import yaml
    HAS_YAML: bool = True
except ImportError:  # pragma: no cover
    HAS_YAML = False
    yaml = None  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Mapping from temporal pattern names to timeline keys in FeatureVector.timelines.
# Unknown pattern names fall back to using the pattern name itself as the key.
_PATTERN_TIMELINE_MAP: Dict[str, str] = {
    "runqueue_spike_during_npu_idle": "runqueue",
    "sched_latency_spike": "sched_latency",
    "cpu_util_imbalance": "cpu_util",
    "h2d_memcpy_blocking": "h2d_bandwidth",
    "d2h_memcpy_blocking": "d2h_bandwidth",
    "launch_gap_pattern": "launch_gap",
    "io_wait_blocking": "io_wait",
    "sync_wait_pattern": "sync_wait",
    "ctx_switch_burst": "ctx_switch",
    "cpu_balance_drift": "cpu_balance",
}

# Supported comparison operators for scalar conditions.
_OPS: Dict[str, Any] = {
    "gt": lambda a, b: a > b,
    "lt": lambda a, b: a < b,
    "ge": lambda a, b: a >= b,
    "le": lambda a, b: a <= b,
    "eq": lambda a, b: abs(a - b) < 1e-9,
}


class RuleEngine:
    """Three-layer rule engine for Host Trace diagnosis.

    Loads YAML rule definitions and evaluates them against FeatureVector
    instances using scalar thresholds, temporal patterns, and correlation
    evidence.  Each rule accumulates weighted confidence; rules whose
    confidence meets or exceeds their threshold are returned as matched.

    Attributes:
        config: Rule engine configuration dict.
        rules: List of loaded rule definition dicts.
        cpu_cores: Default CPU core count for ``auto`` threshold replacement.
        _default_threshold: Default confidence threshold when a rule omits one.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the rule engine and auto-load rules if *rules_dir* is configured.

        Args:
            config: Configuration dict.  Recognised keys:
                - ``rules_dir``: Directory containing YAML rule files.
                - ``default_confidence_threshold``: Default threshold (default *0.6*).
                - ``cpu_cores``: Default CPU core count for ``auto`` thresholds.
        """
        self.config: Dict[str, Any] = config or {}
        self.rules: List[Dict[str, Any]] = []
        self.cpu_cores: int = int(self.config.get("cpu_cores", 0))
        self._default_threshold: float = float(
            self.config.get("default_confidence_threshold", 0.6)
        )

        # Auto-load rules if a rules directory is configured.
        rules_dir: str = str(self.config.get("rules_dir", ""))
        if rules_dir:
            self.load_rules(rules_dir)

    # ------------------------------------------------------------------
    # Rule loading
    # ------------------------------------------------------------------

    def load_rules(self, rules_dir: str) -> None:
        """Load all YAML rule files from *rules_dir*.

        Each YAML file may contain a single rule (top-level mapping) or
        multiple rules (top-level sequence).  Files are processed in
        alphabetical order.  Invalid rules are skipped with a warning.

        Args:
            rules_dir: Path to the directory containing ``.yaml`` / ``.yml``
                rule files.  May be absolute or relative to the project root.
        """
        if not HAS_YAML:
            logger.warning(
                "PyYAML is not installed; rule loading skipped. "
                "Install with: pip install pyyaml"
            )
            return

        rules_path = Path(rules_dir)
        if not rules_path.is_absolute():
            # Resolve relative to the project root (parent of this file's package).
            project_root = Path(__file__).resolve().parent.parent
            rules_path = project_root / rules_path

        if not rules_path.exists() or not rules_path.is_dir():
            logger.warning("Rules directory not found: %s", rules_path)
            return

        yaml_files: List[Path] = sorted(
            list(rules_path.glob("*.yaml")) + list(rules_path.glob("*.yml"))
        )
        loaded_count: int = 0
        existing_ids: set = {r.get("rule_id") for r in self.rules if r.get("rule_id")}

        for yaml_file in yaml_files:
            try:
                with open(yaml_file, "r", encoding="utf-8") as fh:
                    content = yaml.safe_load(fh)
                if content is None:
                    continue

                if isinstance(content, list):
                    for rule in content:
                        if self._validate_rule(rule) and rule.get("rule_id") not in existing_ids:
                            self.rules.append(rule)
                            existing_ids.add(rule.get("rule_id"))
                            loaded_count += 1
                elif isinstance(content, dict):
                    if self._validate_rule(content) and content.get("rule_id") not in existing_ids:
                        self.rules.append(content)
                        existing_ids.add(content.get("rule_id"))
                        loaded_count += 1
                else:
                    logger.warning(
                        "Unexpected YAML structure in %s: %s",
                        yaml_file,
                        type(content).__name__,
                    )
            except yaml.YAMLError as exc:
                logger.error("YAML parse error in %s: %s", yaml_file, exc)
            except OSError as exc:
                logger.error("Cannot read rule file %s: %s", yaml_file, exc)
            except Exception as exc:  # noqa: BLE001
                logger.error("Unexpected error loading %s: %s", yaml_file, exc)

        logger.info("Loaded %d rules from %s", loaded_count, rules_path)

    def _validate_rule(self, rule: Any) -> bool:
        """Validate that *rule* is a dict with all required fields.

        Args:
            rule: Candidate rule dict.

        Returns:
            ``True`` if the rule can be evaluated, ``False`` otherwise.
        """
        if not isinstance(rule, dict):
            return False
        required_fields: List[str] = ["rule_id", "severity", "diagnosis"]
        for field_name in required_fields:
            if field_name not in rule:
                logger.warning(
                    "Rule missing required field '%s': %s", field_name, rule
                )
                return False
        return True

    # ------------------------------------------------------------------
    # Evaluation
    # ------------------------------------------------------------------

    def evaluate(self, fv: FeatureVector) -> List[dict]:
        """Evaluate all loaded rules against *fv*.

        Each rule is scored through three weighted layers:

        1. **Scalar conditions** -- compare ``fv.scalars[key]`` against
           a threshold using the specified operator.
        2. **Temporal patterns** -- basic spike detection on the
           corresponding timeline.
        3. **Correlation evidence** -- check ``fv.bottleneck_attribution``
           ratios.

        ``confidence = sum(hit_weights) / sum(total_weights)``.
        Rules with ``confidence >= threshold`` are returned.

        Args:
            fv: FeatureVector to evaluate.

        Returns:
            List of matched rule dicts, each containing *rule_id*,
            *category*, *severity*, *confidence*, *diagnosis*, and
            *suggestions*.  Sorted by confidence descending.
        """
        matched: List[dict] = []

        for rule in self.rules:
            result = self._evaluate_rule(rule, fv)
            if result is not None:
                matched.append(result)

        matched.sort(key=lambda r: r.get("confidence", 0.0), reverse=True)
        return matched

    def _evaluate_rule(
        self, rule: Dict[str, Any], fv: FeatureVector
    ) -> Optional[dict]:
        """Evaluate a single rule through all three layers.

        Args:
            rule: Rule definition dict.
            fv: FeatureVector to evaluate against.

        Returns:
            Matched rule dict if confidence >= threshold, otherwise ``None``.
        """
        total_weight: float = 0.0
        hit_weight: float = 0.0

        cpu_cores = self._resolve_cpu_cores(fv)

        # --- Layer 1: Scalar conditions ---
        scalar_conds: Dict[str, Any] = rule.get("scalar_conditions", {})
        for key, cond in scalar_conds.items():
            if not isinstance(cond, dict):
                continue
            weight: float = float(cond.get("weight", 0.0))
            if weight <= 0:
                continue

            actual = self._get_scalar_value(key, fv)
            if actual is None:
                # Metric not available; skip without counting weight.
                continue

            threshold_val = cond.get("value")
            if threshold_val == "auto":
                threshold_val = float(cpu_cores) if cpu_cores > 0 else 0.0
            else:
                try:
                    threshold_val = float(threshold_val)
                except (TypeError, ValueError):
                    logger.warning(
                        "Invalid threshold for '%s' in rule %s: %s",
                        key,
                        rule.get("rule_id"),
                        threshold_val,
                    )
                    continue

            op_name: str = str(cond.get("op", "gt"))
            op_func = _OPS.get(op_name)
            if op_func is None:
                logger.warning(
                    "Unknown operator '%s' for key '%s' in rule %s",
                    op_name,
                    key,
                    rule.get("rule_id"),
                )
                continue

            total_weight += weight
            if op_func(float(actual), threshold_val):
                hit_weight += weight

        # --- Layer 2: Temporal patterns ---
        temporal_patterns: List[Dict[str, Any]] = rule.get(
            "temporal_patterns", []
        )
        for pattern_def in temporal_patterns:
            if not isinstance(pattern_def, dict):
                continue
            weight = float(pattern_def.get("weight", 0.0))
            if weight <= 0:
                continue

            total_weight += weight
            pattern_name: str = str(pattern_def.get("pattern", ""))
            if self._check_temporal_pattern(pattern_name, fv):
                hit_weight += weight

        # --- Layer 3: Correlation evidence ---
        corr_evidence: List[Dict[str, Any]] = rule.get(
            "correlation_evidence", []
        )
        for evidence_def in corr_evidence:
            if not isinstance(evidence_def, dict):
                continue
            weight = float(evidence_def.get("weight", 0.0))
            if weight <= 0:
                continue

            total_weight += weight
            attr_key: str = str(
                evidence_def.get("gap_attribution_contains", "")
            )
            min_ratio: float = float(evidence_def.get("min_ratio", 0.0))

            attr_value: float = float(
                fv.bottleneck_attribution.get(attr_key, 0.0)
            )
            if attr_value >= min_ratio:
                hit_weight += weight

        # --- Confidence ---
        confidence: float = (
            hit_weight / total_weight if total_weight > 0 else 0.0
        )

        threshold: float = float(
            rule.get("confidence_threshold", self._default_threshold)
        )

        if confidence >= threshold:
            return {
                "rule_id": str(rule.get("rule_id", "")),
                "category": str(rule.get("category", "")),
                "severity": str(rule.get("severity", "MEDIUM")),
                "confidence": round(confidence, 4),
                "diagnosis": str(rule.get("diagnosis", "")),
                "suggestions": list(rule.get("suggestions", [])),
            }

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_scalar_value(
        self, key: str, fv: FeatureVector
    ) -> Optional[float]:
        """Retrieve a scalar metric value from *fv*.

        Handles special keys that map to direct FeatureVector fields:

        - ``correlation_score`` -- ``fv.correlation_score``
        - ``attribution_sum`` -- sum of ``fv.bottleneck_attribution`` values

        All other keys are looked up in ``fv.scalars``.

        Args:
            key: Scalar metric key.
            fv: FeatureVector to extract from.

        Returns:
            Float value if available, ``None`` if not found.
        """
        if key == "correlation_score":
            return (
                float(fv.correlation_score)
                if fv.correlation_score is not None
                else None
            )
        if key == "attribution_sum":
            if fv.bottleneck_attribution:
                return float(sum(fv.bottleneck_attribution.values()))
            return None

        val = fv.scalars.get(key)
        if val is not None:
            try:
                return float(val)
            except (TypeError, ValueError):
                return None
        return None

    def _resolve_cpu_cores(self, fv: FeatureVector) -> int:
        """Resolve the CPU core count for ``auto`` threshold replacement.

        Checks *fv.metadata* first, then falls back to the engine config.

        Args:
            fv: FeatureVector to extract metadata from.

        Returns:
            CPU core count (*0* if not available).
        """
        meta = fv.metadata if isinstance(fv.metadata, dict) else {}
        cpu_cores = meta.get("cpu_cores", 0)
        if cpu_cores and int(cpu_cores) > 0:
            return int(cpu_cores)
        return self.cpu_cores

    def _check_temporal_pattern(
        self, pattern_name: str, fv: FeatureVector
    ) -> bool:
        """Detect whether a temporal pattern (spike) exists in *fv.timelines*.

        Performs basic spike detection: verifies the corresponding timeline
        exists, is non-empty, and contains at least one value significantly
        above the mean (``value > mean + 2 * std``).

        Args:
            pattern_name: Name of the temporal pattern to check.
            fv: FeatureVector containing timelines.

        Returns:
            ``True`` if a spike is detected, ``False`` otherwise.
        """
        timeline_key: str = _PATTERN_TIMELINE_MAP.get(
            pattern_name, pattern_name
        )

        timeline = fv.timelines.get(timeline_key)
        if not timeline or len(timeline) == 0:
            return False

        # Extract numeric values from (timestamp, value) tuples.
        values: List[float] = []
        for item in timeline:
            if isinstance(item, (list, tuple)) and len(item) >= 2:
                val = item[1]
                if isinstance(val, (int, float)):
                    values.append(float(val))

        if len(values) < 2:
            return False

        n: int = len(values)
        mean_val: float = sum(values) / n
        variance: float = sum((v - mean_val) ** 2 for v in values) / n
        std_val: float = variance ** 0.5

        # Spike: any value exceeding mean + 2 * std (and positive).
        spike_threshold: float = mean_val + 2.0 * std_val
        for v in values:
            if v > spike_threshold and v > 0:
                return True

        return False
