"""
Diagnosis Agent
===============
Combines rule engine results with optional LLM reasoning to produce
a comprehensive :class:`DiagnosisResult`.

Workflow:
1. Determine overall severity from matched rules.
2. Select primary and secondary diagnoses.
3. Build an evidence chain from anomalous scalar metrics.
4. Merge and deduplicate suggestions from all matched rules.
5. Optionally enhance with knowledge-base suggestions.
6. If an LLM API key is configured, generate a natural-language report;
   otherwise, return the structured result directly.

Usage::

    agent = DiagnosisAgent(config)
    result = agent.diagnose(feature_vector, matched_rules)
"""
from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional, Tuple

from ir.schema import FeatureVector, DiagnosisResult
from agent.prompt_templates import build_diagnosis_prompt, build_report_prompt
from agent.knowledge_base import KnowledgeBase

logger = logging.getLogger("host_trace_diagnosis.agent")


# ---------------------------------------------------------------------------
# Metric thresholds for anomaly detection
# ---------------------------------------------------------------------------

# Each entry: metric_key -> (max_normal, min_normal, description, direction)
# direction: "high" means high is bad, "low" means low is bad.
_METRIC_THRESHOLDS: Dict[str, Tuple[float, float, str, str]] = {
    "cpu_util_avg":     (90.0,  10.0,  "CPU平均利用率",       "high"),
    "cpu_util_max":     (95.0,   0.0,  "CPU峰值利用率",       "high"),
    "cpu_idle_avg":     (50.0,   0.0,  "CPU平均空闲率",       "high"),
    "runqueue_avg":     (4.0,    0.0,  "平均运行队列长度",     "high"),
    "runqueue_max":     (16.0,   0.0,  "峰值运行队列长度",     "high"),
    "ctx_switch_rate":  (30000.0, 0.0, "上下文切换速率(次/秒)", "high"),
    "sched_latency_avg_us":   (5000.0,  0.0, "平均调度延迟(μs)",    "high"),
    "sched_latency_p99_us":   (10000.0, 0.0, "P99调度延迟(μs)",     "high"),
    "sched_latency_max_us":   (50000.0, 0.0, "最大调度延迟(μs)",    "high"),
    "cpu_balance_score":      (float("inf"), 0.5, "CPU负载均衡度",   "low"),
    "h2d_bandwidth_mbs":      (float("inf"), 500.0, "H2D拷贝带宽(MB/s)", "low"),
    "d2h_bandwidth_mbs":      (float("inf"), 500.0, "D2H拷贝带宽(MB/s)", "low"),
    "memcpy_total_us":        (500000.0, 0.0, "内存拷贝总耗时(μs)",  "high"),
    "launch_avg_gap_us":      (500.0,   0.0, "平均launch间隙(μs)",  "high"),
}


class DiagnosisAgent:
    """Diagnosis agent that combines rule results with optional LLM reasoning.

    Attributes:
        config: Agent configuration dict.
        llm_api_key: API key for LLM (empty string if not configured).
        llm_model: LLM model name (empty string if not configured).
        llm_base_url: Base URL for LLM API (empty string if not configured).
        max_tokens: Maximum tokens for LLM response.
        temperature: Temperature for LLM reasoning.
        knowledge_base: KnowledgeBase instance for case matching.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the diagnosis agent.

        Args:
            config: Agent configuration dict.  Recognised keys:
                - ``api_key``: LLM API key (enables LLM-enhanced reports).
                - ``model``: LLM model name.
                - ``base_url``: LLM API base URL.
                - ``max_tokens``: Max response tokens (default *4096*).
                - ``temperature``: LLM temperature (default *0.1*).
                - ``case_dir``: Path to knowledge base case directory.
        """
        self.config: Dict[str, Any] = config or {}
        self.llm_api_key: str = str(self.config.get("api_key", ""))
        self.llm_model: str = str(self.config.get("model", ""))
        self.llm_base_url: str = str(self.config.get("base_url", ""))
        self.max_tokens: int = int(self.config.get("max_tokens", 4096))
        self.temperature: float = float(self.config.get("temperature", 0.1))
        self.knowledge_base: KnowledgeBase = KnowledgeBase(
            str(self.config.get("case_dir", ""))
        )

    # ------------------------------------------------------------------
    # Main diagnosis entry point
    # ------------------------------------------------------------------

    def diagnose(
        self, fv: FeatureVector, matched_rules: List[dict]
    ) -> DiagnosisResult:
        """Produce a DiagnosisResult from the feature vector and matched rules.

        Args:
            fv: FeatureVector containing extracted features.
            matched_rules: List of matched rule dicts from RuleEngine.

        Returns:
            A fully populated DiagnosisResult.
        """
        # --- Step 1: Early exit if no issues detected ---
        if not matched_rules and fv.correlation_score < 0.3:
            return DiagnosisResult(
                matched_rules=[],
                has_host_issue=False,
                severity="NONE",
                primary_diagnosis="未发现明显Host侧性能问题",
                secondary_diagnoses=[],
                evidence=self._build_evidence(fv),
                suggestions=[],
                feature_vector=fv,
            )

        # --- Step 2: Determine overall severity ---
        severity: str = self._determine_severity(matched_rules)

        # --- Step 3: Select primary and secondary diagnoses ---
        primary_diagnosis: str = ""
        secondary_diagnoses: List[str] = []

        if matched_rules:
            # Sort by confidence descending (defensive copy)
            sorted_rules = sorted(
                matched_rules, key=lambda r: r.get("confidence", 0.0), reverse=True
            )
            primary_diagnosis = sorted_rules[0].get("diagnosis", "")
            secondary_diagnoses = [
                r.get("diagnosis", "")
                for r in sorted_rules[1:]
                if r.get("diagnosis", "")
            ]
        else:
            # No rules matched but correlation is significant
            primary_diagnosis = (
                "检测到Host-Device相关性较高，但未命中具体规则，"
                "建议人工进一步分析"
            )

        # --- Step 4: Build evidence chain ---
        evidence: List[Dict[str, Any]] = self._build_evidence(fv)

        # Add rule-based evidence
        for rule in matched_rules:
            evidence.append({
                "metric": f"rule_{rule.get('rule_id', '')}",
                "value": rule.get("confidence", 0.0),
                "threshold": rule.get("confidence_threshold",
                                      self.config.get("default_confidence_threshold",
                                                      0.6)),
                "conclusion": f"规则命中: {rule.get('diagnosis', '')}",
            })

        # --- Step 5: Merge suggestions ---
        suggestions: List[Dict[str, Any]] = self._merge_suggestions(matched_rules)

        # Enhance with knowledge base
        kb_suggestion = self.knowledge_base.get_suggestion(primary_diagnosis)
        if kb_suggestion:
            # Check if already present
            existing_texts = {
                s.get("text", "") for s in suggestions if isinstance(s, dict)
            }
            if kb_suggestion not in existing_texts:
                suggestions.append({
                    "text": kb_suggestion,
                    "priority": 99,
                    "source": "knowledge_base",
                })

        # --- Step 6: Optional LLM enhancement ---
        has_issue: bool = severity != "NONE"

        if self.llm_api_key:
            llm_report = self._call_llm(fv, matched_rules)
            if llm_report:
                # Append LLM report as a special evidence entry
                evidence.append({
                    "metric": "llm_analysis",
                    "value": "N/A",
                    "threshold": "N/A",
                    "conclusion": llm_report,
                })

        # --- Step 7: Assemble result ---
        result = DiagnosisResult(
            matched_rules=matched_rules,
            has_host_issue=has_issue,
            severity=severity,
            primary_diagnosis=primary_diagnosis,
            secondary_diagnoses=secondary_diagnoses,
            evidence=evidence,
            suggestions=suggestions,
            feature_vector=fv,
        )

        logger.info(
            "Diagnosis complete: severity=%s, primary=%s, evidence=%d, suggestions=%d",
            severity,
            primary_diagnosis[:60],
            len(evidence),
            len(suggestions),
        )

        return result

    # ------------------------------------------------------------------
    # Severity determination
    # ------------------------------------------------------------------

    def _determine_severity(self, matched_rules: List[dict]) -> str:
        """Determine overall severity from matched rules.

        Severity priority:
        - CRITICAL if any rule is CRITICAL.
        - HIGH if any HIGH rule has confidence > 0.7.
        - MEDIUM if any HIGH rule exists.
        - LOW if any MEDIUM rule exists.
        - NONE otherwise.

        Args:
            matched_rules: List of matched rule dicts.

        Returns:
            Severity string: CRITICAL / HIGH / MEDIUM / LOW / NONE.
        """
        if not matched_rules:
            return "NONE"

        has_critical: bool = False
        has_high: bool = False
        has_high_confident: bool = False
        has_medium: bool = False

        for rule in matched_rules:
            sev = str(rule.get("severity", "")).upper()
            conf = float(rule.get("confidence", 0.0))

            if sev == "CRITICAL":
                has_critical = True
            elif sev == "HIGH":
                has_high = True
                if conf > 0.7:
                    has_high_confident = True
            elif sev == "MEDIUM":
                has_medium = True

        if has_critical:
            return "CRITICAL"
        if has_high_confident:
            return "HIGH"
        if has_high:
            return "MEDIUM"
        if has_medium:
            return "LOW"
        return "NONE"

    # ------------------------------------------------------------------
    # Evidence chain construction
    # ------------------------------------------------------------------

    def _build_evidence(
        self, fv: FeatureVector
    ) -> List[Dict[str, Any]]:
        """Build an evidence chain from anomalous scalar metrics.

        For each metric in ``_METRIC_THRESHOLDS``, checks whether the
        value in ``fv.scalars`` exceeds the normal range.  If so, adds
        an evidence entry.

        Args:
            fv: FeatureVector to extract metrics from.

        Returns:
            List of evidence dicts, each containing *metric*, *value*,
            *threshold*, and *conclusion*.
        """
        evidence: List[Dict[str, Any]] = []

        for key, (max_val, min_val, desc, direction) in _METRIC_THRESHOLDS.items():
            actual = fv.scalars.get(key)
            if actual is None:
                continue

            try:
                actual_float = float(actual)
            except (TypeError, ValueError):
                continue

            if direction == "high" and actual_float > max_val:
                threshold_str = f"<= {max_val}"
                conclusion = (
                    f"{desc}={actual_float:.2f}，超过正常阈值{max_val}，"
                    f"存在异常"
                )
                evidence.append({
                    "metric": key,
                    "value": round(actual_float, 2),
                    "threshold": max_val,
                    "threshold_str": threshold_str,
                    "conclusion": conclusion,
                })
            elif direction == "low" and actual_float < min_val:
                threshold_str = f">= {min_val}"
                conclusion = (
                    f"{desc}={actual_float:.2f}，低于正常阈值{min_val}，"
                    f"存在异常"
                )
                evidence.append({
                    "metric": key,
                    "value": round(actual_float, 2),
                    "threshold": min_val,
                    "threshold_str": threshold_str,
                    "conclusion": conclusion,
                })

        # Add correlation score evidence if significant
        if fv.correlation_score > 0.6:
            evidence.append({
                "metric": "correlation_score",
                "value": round(float(fv.correlation_score), 4),
                "threshold": 0.6,
                "threshold_str": "<= 0.6",
                "conclusion": (
                    f"Host-Device相关系数={fv.correlation_score:.4f}，"
                    f"表明Host侧事件与Device gap存在强相关性"
                ),
            })

        # Add bottleneck attribution evidence
        for attr_key, attr_val in fv.bottleneck_attribution.items():
            if attr_val >= 0.20:
                evidence.append({
                    "metric": f"attribution_{attr_key}",
                    "value": round(float(attr_val), 4),
                    "threshold": 0.20,
                    "threshold_str": "<= 0.20",
                    "conclusion": (
                        f"瓶颈归因{attr_key}={attr_val:.2%}，"
                        f"占比显著（>= 20%）"
                    ),
                })

        return evidence

    # ------------------------------------------------------------------
    # Suggestion merging
    # ------------------------------------------------------------------

    def _merge_suggestions(
        self, matched_rules: List[dict]
    ) -> List[Dict[str, Any]]:
        """Merge suggestions from all matched rules.

        Deduplicates by suggestion text and sorts by priority.

        Args:
            matched_rules: List of matched rule dicts.

        Returns:
            Merged and sorted suggestion list.
        """
        seen_texts: set = set()
        merged: List[Dict[str, Any]] = []

        for rule in matched_rules:
            rule_id = rule.get("rule_id", "")
            for sug in rule.get("suggestions", []):
                if isinstance(sug, dict):
                    text = str(sug.get("text", ""))
                    priority = sug.get("priority", 99)
                elif isinstance(sug, str):
                    text = sug
                    priority = 99
                else:
                    continue

                if text and text not in seen_texts:
                    seen_texts.add(text)
                    merged.append({
                        "text": text,
                        "priority": priority,
                        "source_rule": rule_id,
                    })

        # Sort by priority (ascending = higher priority first)
        merged.sort(key=lambda s: s.get("priority", 99))

        return merged

    # ------------------------------------------------------------------
    # LLM interface (deferred implementation)
    # ------------------------------------------------------------------

    def _call_llm(
        self, fv: FeatureVector, matched_rules: List[dict]
    ) -> Optional[str]:
        """Call the LLM to generate a natural-language diagnosis report.

        This method builds the full diagnosis prompt and sends it to the
        configured LLM API.  The actual HTTP call is deferred -- if the
        ``requests`` library is unavailable or the API endpoint is not
        reachable, this method returns ``None`` and the caller falls
        back to the structured result.

        Args:
            fv: FeatureVector for context.
            matched_rules: Matched rules for context.

        Returns:
            LLM-generated report string, or ``None`` if unavailable.
        """
        if not self.llm_api_key:
            return None

        prompt: str = build_diagnosis_prompt(fv, matched_rules)

        try:
            import requests  # type: ignore[import-untyped]
        except ImportError:
            logger.warning(
                "requests library not available; LLM call skipped. "
                "Install with: pip install requests"
            )
            return None

        # --- Deferred LLM API call ---
        # The actual implementation depends on the specific LLM provider.
        # Below is the interface skeleton; customize as needed.
        try:
            headers: Dict[str, str] = {
                "Content-Type": "application/json",
                "Authorization": f"Bearer {self.llm_api_key}",
            }
            payload: Dict[str, Any] = {
                "model": self.llm_model,
                "messages": [
                    {"role": "system", "content": (
                        "你是一名昇腾NPU性能分析专家，擅长从Host侧trace数据"
                        "中定位性能瓶颈。"
                    )},
                    {"role": "user", "content": prompt},
                ],
                "max_tokens": self.max_tokens,
                "temperature": self.temperature,
            }

            # Uncomment and customize for your LLM provider:
            # response = requests.post(
            #     f"{self.llm_base_url}/v1/chat/completions",
            #     headers=headers,
            #     json=payload,
            #     timeout=30,
            # )
            # response.raise_for_status()
            # data = response.json()
            # return data["choices"][0]["message"]["content"]

            logger.info(
                "LLM API call interface ready but not yet activated. "
                "Configure base_url and uncomment the HTTP call to enable."
            )
            return None

        except Exception as exc:  # noqa: BLE001
            logger.warning("LLM API call failed: %s", exc)
            return None

    # ------------------------------------------------------------------
    # Report prompt (for external LLM callers)
    # ------------------------------------------------------------------

    def build_report_prompt(self, result: DiagnosisResult) -> str:
        """Build a report refinement prompt for the given result.

        This is a convenience wrapper around
        :func:`agent.prompt_templates.build_report_prompt`.

        Args:
            result: DiagnosisResult to build a prompt for.

        Returns:
            Report refinement prompt string.
        """
        return build_report_prompt(result)
