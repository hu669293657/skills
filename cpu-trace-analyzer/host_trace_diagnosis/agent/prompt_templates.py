"""
Prompt Templates
================
LLM prompt templates for Host Trace diagnosis.

Provides:
- ``SYSTEM_PROMPT``: System role prompt defining the LLM's expertise and task.
- ``build_diagnosis_prompt()``: Builds a full diagnosis prompt from a
  FeatureVector and matched rules.
- ``build_report_prompt()``: Builds a report refinement prompt from a
  DiagnosisResult.

All prompts are plain strings that can be sent to any chat-completion API.
No external templating library is required.
"""
from __future__ import annotations

from typing import Any, Dict, List

from ir.schema import FeatureVector, DiagnosisResult

# ---------------------------------------------------------------------------
# System prompt
# ---------------------------------------------------------------------------

SYSTEM_PROMPT: str = (
    "你是一名昇腾NPU性能分析专家，擅长从Host侧trace数据中定位性能瓶颈。"
    "你的任务是根据结构化的性能特征数据，判断是否存在Host侧问题，"
    "给出根因定位和优化建议。"
)


# ---------------------------------------------------------------------------
# Internal formatting helpers
# ---------------------------------------------------------------------------

def _format_scalars(scalars: Dict[str, float]) -> str:
    """Format scalar metrics as a readable aligned table.

    Args:
        scalars: Dict of metric name to float value.

    Returns:
        Multi-line string table.
    """
    if not scalars:
        return "  (无标量指标数据)"

    lines: List[str] = []
    header = f"  {'指标':<28s} {'值':>12s}"
    lines.append(header)
    lines.append(f"  {'-' * 28} {'-' * 12}")

    for key in sorted(scalars.keys()):
        val = scalars[key]
        try:
            if isinstance(val, float):
                val_str = f"{val:.2f}"
            else:
                val_str = str(val)
        except (TypeError, ValueError):
            val_str = str(val)
        lines.append(f"  {key:<28s} {val_str:>12s}")

    return "\n".join(lines)


def _format_attribution(attribution: Dict[str, float]) -> str:
    """Format bottleneck attribution as a percentage breakdown.

    Args:
        attribution: Dict of category to ratio (0-1).

    Returns:
        Multi-line string showing each category and its percentage.
    """
    if not attribution:
        return "  (无瓶颈归因数据)"

    lines: List[str] = []
    total = sum(attribution.values())
    for key in sorted(attribution, key=lambda k: attribution[k], reverse=True):
        val = attribution[key]
        pct = val * 100 if val <= 1.0 else val
        share = f"{pct:.1f}%"
        if total > 0:
            rel = f"  (占总归因 {val / total * 100:.1f}%)"
        else:
            rel = ""
        lines.append(f"  {key:<20s} {share:>8s}{rel}")

    return "\n".join(lines)


def _format_matched_rules(matched_rules: List[dict]) -> str:
    """Format matched rules as a numbered list with confidence scores.

    Args:
        matched_rules: List of matched rule dicts.

    Returns:
        Multi-line string listing each rule.
    """
    if not matched_rules:
        return "  (无命中的规则)"

    lines: List[str] = []
    for i, rule in enumerate(matched_rules, 1):
        rule_id = rule.get("rule_id", "?")
        category = rule.get("category", "")
        severity = rule.get("severity", "")
        confidence = rule.get("confidence", 0.0)
        diagnosis = rule.get("diagnosis", "")
        lines.append(f"  {i}. [{rule_id}] {category} (严重度: {severity}, "
                     f"置信度: {confidence:.2f})")
        lines.append(f"     诊断: {diagnosis}")

        suggestions = rule.get("suggestions", [])
        if suggestions:
            lines.append("     建议:")
            for sug in suggestions:
                if isinstance(sug, dict):
                    text = sug.get("text", "")
                    priority = sug.get("priority", "")
                    lines.append(f"       [优先级 {priority}] {text}")
                else:
                    lines.append(f"       {sug}")

    return "\n".join(lines)


def _format_top_gaps(top_gaps: List[dict], max_count: int = 10) -> str:
    """Format top gaps as a table.

    Args:
        top_gaps: List of gap dicts from FeatureVector.
        max_count: Maximum number of gaps to display.

    Returns:
        Multi-line string table.
    """
    if not top_gaps:
        return "  (无显著gap数据)"

    lines: List[str] = []
    lines.append(f"  {'序号':>4s}  {'gap时长(us)':>14s}  "
                 f"{'归因':<16s}  {'前驱kernel':<20s}  {'后继kernel':<20s}")
    lines.append(f"  {'-' * 4}  {'-' * 14}  {'-' * 16}  {'-' * 20}  {'-' * 20}")

    for i, gap in enumerate(top_gaps[:max_count], 1):
        gap_dur = gap.get("gap_dur", 0)
        attr = gap.get("attribution", "UNKNOWN")
        prev_k = str(gap.get("prev_kernel_name", ""))[:20]
        next_k = str(gap.get("next_kernel_name", ""))[:20]
        lines.append(f"  {i:>4d}  {gap_dur:>14d}  {attr:<16s}  "
                     f"{prev_k:<20s}  {next_k:<20s}")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Public prompt builders
# ---------------------------------------------------------------------------

def build_diagnosis_prompt(
    fv: FeatureVector, matched_rules: List[dict]
) -> str:
    """Build a complete diagnosis prompt for the LLM.

    The prompt includes:
    1. System role definition.
    2. Scalar performance metrics (formatted table).
    3. Bottleneck attribution distribution.
    4. Matched rule list with confidence scores.
    5. Top gaps detail.
    6. Output requirements (conclusion, severity, evidence chain,
       root cause, suggestions).

    Args:
        fv: FeatureVector containing extracted performance features.
        matched_rules: List of matched rule dicts from RuleEngine.

    Returns:
        Complete prompt string ready to send to an LLM.
    """
    sections: List[str] = []

    # --- Section 1: System prompt ---
    sections.append(f"[系统提示]\n{SYSTEM_PROMPT}")

    # --- Section 2: Scalar metrics ---
    sections.append(
        "[性能指标数据]\n以下为从Host侧trace中提取的标量特征指标:\n"
        + _format_scalars(fv.scalars)
    )

    # --- Section 3: Bottleneck attribution ---
    sections.append(
        "[瓶颈归因分布]\n以下为各瓶颈类别的归因比例:\n"
        + _format_attribution(fv.bottleneck_attribution)
    )

    # Add correlation score
    sections.append(f"\n  Host-Device相关系数: {fv.correlation_score:.4f}")

    # --- Section 4: Matched rules ---
    sections.append(
        "[规则引擎命中结果]\n以下规则被规则引擎命中:\n"
        + _format_matched_rules(matched_rules)
    )

    # --- Section 5: Top gaps ---
    sections.append(
        "[Top Gaps 明细]\n以下为按时长排序的最显著的Device空闲gap:\n"
        + _format_top_gaps(fv.top_gaps)
    )

    # --- Section 6: Output requirements ---
    output_req = (
        "[输出要求]\n"
        "请基于以上数据，输出以下内容:\n"
        "1. **结论**: 是否存在Host侧问题，简要概述核心发现\n"
        "2. **严重程度**: CRITICAL / HIGH / MEDIUM / LOW / NONE\n"
        "3. **证据链**: 列出支撑结论的关键证据，每条必须引用具体的指标值"
        "（如 runqueue_avg=12.5 超过CPU核心数8）\n"
        "4. **根因分析**: 深入分析问题的根本原因，解释Host侧瓶颈如何影响NPU性能\n"
        "5. **优化建议**: 按优先级给出具体的优化措施，每条建议应包含:\n"
        "   - 优化措施描述\n"
        "   - 预期收益\n"
        "   - 实施难度（低/中/高）"
    )
    sections.append(output_req)

    return "\n\n".join(sections)


def build_report_prompt(result: DiagnosisResult) -> str:
    """Build a report refinement prompt from a DiagnosisResult.

    Used to polish the structured diagnosis result into a coherent
    natural-language report.

    Args:
        result: DiagnosisResult from the diagnosis agent.

    Returns:
        Report refinement prompt string.
    """
    sections: List[str] = []

    sections.append(f"[系统提示]\n{SYSTEM_PROMPT}")
    sections.append(
        "[任务说明]\n"
        "以下是一个已完成的Host侧性能诊断结果。请将其润色为一份结构清晰、"
        "逻辑严谨的性能分析报告。报告应使用专业但易读的语言，"
        "避免过度技术化。"
    )

    # Diagnosis summary
    summary_lines: List[str] = [
        f"[诊断摘要]",
        f"  是否存在Host问题: {'是' if result.has_host_issue else '否'}",
        f"  严重程度: {result.severity}",
        f"  主要诊断: {result.primary_diagnosis}",
    ]
    if result.secondary_diagnoses:
        summary_lines.append("  次要诊断:")
        for sd in result.secondary_diagnoses:
            summary_lines.append(f"    - {sd}")
    sections.append("\n".join(summary_lines))

    # Evidence chain
    if result.evidence:
        ev_lines: List[str] = ["[证据链]"]
        for ev in result.evidence:
            if isinstance(ev, dict):
                metric = ev.get("metric", "")
                value = ev.get("value", "")
                threshold = ev.get("threshold", "")
                conclusion = ev.get("conclusion", "")
                ev_lines.append(
                    f"  - {metric}: 值={value}, 阈值={threshold}, 结论={conclusion}"
                )
        sections.append("\n".join(ev_lines))

    # Suggestions
    if result.suggestions:
        sug_lines: List[str] = ["[优化建议]"]
        for sug in result.suggestions:
            if isinstance(sug, dict):
                text = sug.get("text", "")
                priority = sug.get("priority", "")
                sug_lines.append(f"  [优先级 {priority}] {text}")
        sections.append("\n".join(sug_lines))

    # Matched rules
    if result.matched_rules:
        rule_lines: List[str] = ["[命中规则]"]
        for rule in result.matched_rules:
            rid = rule.get("rule_id", "?")
            conf = rule.get("confidence", 0.0)
            diag = rule.get("diagnosis", "")
            rule_lines.append(f"  - {rid} (置信度 {conf:.2f}): {diag}")
        sections.append("\n".join(rule_lines))

    # Output requirements
    sections.append(
        "[输出要求]\n"
        "请生成一份完整的性能分析报告，包含以下部分:\n"
        "1. **概述**: 一段话总结诊断结论和严重程度\n"
        "2. **证据分析**: 逐条分析关键证据，解释其含义和影响\n"
        "3. **根因定位**: 深入分析问题的根本原因\n"
        "4. **优化方案**: 按优先级排列优化建议，每条包含预期收益和实施难度\n"
        "5. **风险评估**: 不修复该问题的潜在影响"
    )

    return "\n\n".join(sections)
