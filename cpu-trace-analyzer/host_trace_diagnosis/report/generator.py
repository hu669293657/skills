"""
Report Generator
================
Generates JSON and HTML diagnostic reports from :class:`DiagnosisResult`.

Produces:
- ``diagnosis_report.json``: Full structured result as JSON.
- ``diagnosis_report.html``: Self-contained HTML report with inline CSS.
- ``timeline_viz.html`` (optional): Host/Device timeline visualization.

The HTML report uses a template file (``templates/report_template.html``)
with ``{placeholder}`` markers that are replaced via ``str.replace``.
If the template file is missing, a minimal fallback is used.

Usage::

    gen = ReportGenerator(config)
    html_path = gen.generate(result, "reports/")
"""
from __future__ import annotations

import html as html_mod
import json
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional

from ir.schema import DiagnosisResult, FeatureVector

logger = logging.getLogger("host_trace_diagnosis.report")


class ReportGenerator:
    """Generates diagnostic reports in JSON and HTML formats.

    Attributes:
        config: Report configuration dict.
        generate_timeline_viz: Whether to generate the timeline visualization.
        max_gaps_in_viz: Maximum gaps to show in the timeline visualization.
        _template_path: Path to the HTML template file.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the report generator.

        Args:
            config: Report configuration dict.  Recognised keys:
                - ``generate_timeline_viz``: bool (default *True*).
                - ``max_gaps_in_viz``: int (default *10*).
                - ``format``: Output format -- ``json``, ``html``, or ``both``.
                - ``output_dir``: Default output directory.
        """
        self.config: Dict[str, Any] = config or {}
        self.generate_timeline_viz: bool = bool(
            self.config.get("generate_timeline_viz", True)
        )
        self.max_gaps_in_viz: int = int(self.config.get("max_gaps_in_viz", 10))
        self.output_format: str = str(self.config.get("format", "both"))
        self._template_path: Path = (
            Path(__file__).resolve().parent / "templates" / "report_template.html"
        )

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate(self, result: DiagnosisResult, output_dir: str) -> str:
        """Generate all report files and return the HTML report path.

        Args:
            result: DiagnosisResult to report on.
            output_dir: Directory to write report files into.

        Returns:
            Path to the generated HTML report file.
        """
        out_path = Path(output_dir)
        out_path.mkdir(parents=True, exist_ok=True)

        # --- JSON report ---
        json_path = out_path / "diagnosis_report.json"
        try:
            result_dict = result.to_dict()
            with open(json_path, "w", encoding="utf-8") as fh:
                json.dump(result_dict, fh, ensure_ascii=False, indent=2)
            logger.info("JSON report written to %s", json_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write JSON report: %s", exc)

        # --- HTML report ---
        html_path = out_path / "diagnosis_report.html"
        try:
            template = self._load_template()
            html_content = self._render_template(template, result)
            with open(html_path, "w", encoding="utf-8") as fh:
                fh.write(html_content)
            logger.info("HTML report written to %s", html_path)
        except Exception as exc:  # noqa: BLE001
            logger.error("Failed to write HTML report: %s", exc)

        # --- Timeline visualization ---
        if self.generate_timeline_viz:
            try:
                from report.timeline_viz import TimelineVisualizer

                viz = TimelineVisualizer(self.config)
                viz_path = out_path / "timeline_viz.html"
                viz.generate(result, str(viz_path))
                logger.info("Timeline visualization written to %s", viz_path)
            except Exception as exc:  # noqa: BLE001
                logger.warning("Failed to generate timeline visualization: %s", exc)

        return str(html_path)

    # ------------------------------------------------------------------
    # Template loading and rendering
    # ------------------------------------------------------------------

    def _load_template(self) -> str:
        """Load the HTML template from the templates directory.

        Returns:
            Template string.  Falls back to a minimal inline template
            if the file is not found.
        """
        try:
            if self._template_path.exists():
                with open(self._template_path, "r", encoding="utf-8") as fh:
                    return fh.read()
        except OSError as exc:
            logger.warning("Cannot read template %s: %s", self._template_path, exc)

        logger.info("Using fallback HTML template")
        return self._fallback_template()

    def _fallback_template(self) -> str:
        """Return a minimal inline HTML template.

        Returns:
            A minimal HTML string with the same placeholders.
        """
        return (
            '<!DOCTYPE html><html lang="zh-CN"><head><meta charset="UTF-8">'
            '<title>Host Trace 诊断报告</title></head><body>'
            '<h1>Host Trace 诊断报告</h1>'
            '<h2>诊断摘要</h2>{summary}'
            '<h2>关键指标</h2>{metrics_table}'
            '<h2>瓶颈归因分布</h2>{attribution_bars}'
            '<h2>证据链</h2>{evidence_chain}'
            '<h2>根因分析</h2>{root_cause}'
            '<h2>优化建议</h2>{suggestions}'
            '<h2>附录：Top Gaps 明细</h2>{top_gaps_table}'
            '</body></html>'
        )

    def _render_template(
        self, template: str, result: DiagnosisResult
    ) -> str:
        """Replace all placeholders in the template with generated HTML.

        Uses ``str.replace`` (not ``str.format``) so that CSS curly
        braces in the template are not affected.

        Args:
            template: HTML template string with ``{placeholder}`` markers.
            result: DiagnosisResult to render.

        Returns:
            Complete HTML string with all placeholders replaced.
        """
        replacements: Dict[str, str] = {
            "{summary}": self._generate_summary(result),
            "{metrics_table}": self._generate_metrics_table(result),
            "{attribution_bars}": self._generate_attribution_bars(result),
            "{evidence_chain}": self._generate_evidence_chain(result),
            "{root_cause}": self._generate_root_cause(result),
            "{suggestions}": self._generate_suggestions(result),
            "{top_gaps_table}": self._generate_top_gaps_table(result),
        }

        html_content: str = template
        for placeholder, value in replacements.items():
            html_content = html_content.replace(placeholder, value)

        return html_content

    # ------------------------------------------------------------------
    # Section generators
    # ------------------------------------------------------------------

    def _generate_summary(self, result: DiagnosisResult) -> str:
        """Generate HTML for the summary section.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string for the summary section.
        """
        severity_lower = result.severity.lower()
        parts: List[str] = []

        parts.append(
            f'<div class="severity-badge severity-{severity_lower}">'
            f'{html_mod.escape(result.severity)}</div>'
        )

        if result.primary_diagnosis:
            parts.append(
                f'<div class="primary-diagnosis">'
                f'{html_mod.escape(result.primary_diagnosis)}</div>'
            )

        if result.secondary_diagnoses:
            parts.append('<div class="secondary-diagnoses"><ul>')
            for sd in result.secondary_diagnoses:
                parts.append(f"<li>{html_mod.escape(str(sd))}</li>")
            parts.append("</ul></div>")

        if not result.has_host_issue:
            parts.append(
                '<p class="empty-state">未检测到明显的Host侧性能问题</p>'
            )

        return "\n".join(parts)

    def _generate_metrics_table(self, result: DiagnosisResult) -> str:
        """Generate HTML for the metrics table.

        Shows all scalar metrics with their values and anomaly status.
        Abnormal metrics (those appearing in the evidence chain) are
        highlighted in red.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string containing a ``<table>``.
        """
        fv = result.feature_vector
        if not fv or not fv.scalars:
            return '<p class="empty-state">无指标数据</p>'

        # Collect abnormal metric keys from evidence chain.
        abnormal_keys: set = set()
        evidence_map: Dict[str, dict] = {}
        for ev in result.evidence:
            if not isinstance(ev, dict):
                continue
            metric = str(ev.get("metric", ""))
            # Skip non-scalar evidence entries.
            if (
                metric.startswith("rule_")
                or metric.startswith("attribution_")
                or metric in ("correlation_score", "llm_analysis")
            ):
                continue
            abnormal_keys.add(metric)
            evidence_map[metric] = ev

        parts: List[str] = [
            "<table><thead><tr>",
            "<th>指标</th><th>值</th><th>阈值</th><th>结论</th>",
            "</tr></thead><tbody>",
        ]

        for key in sorted(fv.scalars.keys()):
            val = fv.scalars[key]
            is_abnormal: bool = key in abnormal_keys
            row_class = ' class="abnormal"' if is_abnormal else ""

            try:
                val_float = float(val)
                val_str = f"{val_float:.2f}"
            except (TypeError, ValueError):
                val_str = html_mod.escape(str(val))

            if is_abnormal:
                ev = evidence_map.get(key, {})
                threshold = ev.get("threshold", "")
                conclusion = html_mod.escape(str(ev.get("conclusion", "异常")))
                try:
                    threshold_str = f"{float(threshold):.2f}"
                except (TypeError, ValueError):
                    threshold_str = html_mod.escape(str(threshold))
            else:
                threshold_str = '<span style="color:var(--text-secondary);">-</span>'
                conclusion = (
                    '<span style="color:var(--success);">正常</span>'
                )

            parts.append(
                f"<tr{row_class}>"
                f"<td>{html_mod.escape(key)}</td>"
                f"<td>{val_str}</td>"
                f"<td>{threshold_str}</td>"
                f"<td>{conclusion}</td>"
                f"</tr>"
            )

        parts.append("</tbody></table>")
        return "\n".join(parts)

    def _generate_attribution_bars(self, result: DiagnosisResult) -> str:
        """Generate HTML for the bottleneck attribution bar chart.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string with CSS-based horizontal bar chart.
        """
        fv = result.feature_vector
        if not fv or not fv.bottleneck_attribution:
            return '<p class="empty-state">无瓶颈归因数据</p>'

        total = sum(fv.bottleneck_attribution.values())
        parts: List[str] = ['<div class="bar-chart">']

        for key in sorted(
            fv.bottleneck_attribution,
            key=lambda k: fv.bottleneck_attribution[k],
            reverse=True,
        ):
            val = float(fv.bottleneck_attribution[key])
            pct = (val / total * 100) if total > 0 else 0.0
            bar_class = f"attr-{key}"

            parts.append('<div class="bar-item">')
            parts.append(
                f'<span class="bar-label">{html_mod.escape(key)}</span>'
            )
            parts.append(
                f'<div class="bar-container">'
                f'<div class="bar-fill {bar_class}" '
                f'style="width: {pct:.1f}%;"></div></div>'
            )
            parts.append(f'<span class="bar-value">{pct:.1f}%</span>')
            parts.append("</div>")

        parts.append("</div>")
        return "\n".join(parts)

    def _generate_evidence_chain(self, result: DiagnosisResult) -> str:
        """Generate HTML for the evidence chain section.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string with evidence items.
        """
        if not result.evidence:
            return '<p class="empty-state">无证据数据</p>'

        parts: List[str] = ['<div class="evidence-list">']

        for ev in result.evidence:
            if not isinstance(ev, dict):
                continue
            metric = html_mod.escape(str(ev.get("metric", "")))
            value = ev.get("value", "")
            threshold = ev.get("threshold", "")
            conclusion = html_mod.escape(str(ev.get("conclusion", "")))

            try:
                value_str = f"{float(value):.4f}"
            except (TypeError, ValueError):
                value_str = html_mod.escape(str(value))

            parts.append('<div class="evidence-item">')
            parts.append(f'<span class="evidence-metric">{metric}</span>: ')
            parts.append(f'<span class="evidence-value">{value_str}</span>')
            if threshold and str(threshold) != "N/A":
                parts.append(f" (阈值: {html_mod.escape(str(threshold))})")
            parts.append(
                f'<div class="evidence-conclusion">{conclusion}</div>'
            )
            parts.append("</div>")

        parts.append("</div>")
        return "\n".join(parts)

    def _generate_root_cause(self, result: DiagnosisResult) -> str:
        """Generate HTML for the root cause analysis section.

        Constructs a structured root-cause narrative from the primary
        diagnosis, secondary diagnoses, and top evidence items.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string with root cause analysis.
        """
        parts: List[str] = ['<div class="root-cause-text">']

        parts.append(
            f"<strong>主要根因:</strong> "
            f"{html_mod.escape(result.primary_diagnosis)}"
        )

        if result.secondary_diagnoses:
            parts.append("<br><br><strong>次要问题:</strong><ul>")
            for sd in result.secondary_diagnoses:
                parts.append(f"<li>{html_mod.escape(str(sd))}</li>")
            parts.append("</ul>")

        # Top 5 non-rule evidence items.
        top_evidence: List[dict] = [
            ev for ev in result.evidence
            if isinstance(ev, dict)
            and not str(ev.get("metric", "")).startswith("rule_")
            and ev.get("metric") != "llm_analysis"
        ][:5]

        if top_evidence:
            parts.append("<br><strong>关键证据链:</strong><ul>")
            for ev in top_evidence:
                conclusion = html_mod.escape(str(ev.get("conclusion", "")))
                parts.append(f"<li>{conclusion}</li>")
            parts.append("</ul>")

        # Matched rules summary.
        if result.matched_rules:
            parts.append("<br><strong>命中规则:</strong><ul>")
            for rule in result.matched_rules:
                rid = html_mod.escape(str(rule.get("rule_id", "")))
                conf = rule.get("confidence", 0.0)
                diag = html_mod.escape(str(rule.get("diagnosis", "")))
                parts.append(
                    f"<li><strong>{rid}</strong> "
                    f"(置信度 {conf:.2f}): {diag}</li>"
                )
            parts.append("</ul>")

        parts.append("</div>")
        return "\n".join(parts)

    def _generate_suggestions(self, result: DiagnosisResult) -> str:
        """Generate HTML for the optimization suggestions section.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string with suggestion items.
        """
        if not result.suggestions:
            return '<p class="empty-state">无优化建议</p>'

        parts: List[str] = ['<div class="suggestion-list">']

        for sug in result.suggestions:
            if isinstance(sug, dict):
                text = html_mod.escape(str(sug.get("text", "")))
                priority = sug.get("priority", 99)
                source = sug.get("source_rule", sug.get("source", ""))
            elif isinstance(sug, str):
                text = html_mod.escape(sug)
                priority = 99
                source = ""
            else:
                continue

            parts.append('<div class="suggestion-item">')
            parts.append(
                f'<span class="suggestion-priority">{priority}</span>'
            )
            parts.append(f"<div><div>{text}</div>")
            if source:
                parts.append(
                    f'<div class="suggestion-source">'
                    f'来源: {html_mod.escape(str(source))}'
                    f"</div>"
                )
            parts.append("</div></div>")

        parts.append("</div>")
        return "\n".join(parts)

    def _generate_top_gaps_table(self, result: DiagnosisResult) -> str:
        """Generate HTML for the top gaps detail table.

        Args:
            result: DiagnosisResult.

        Returns:
            HTML string containing a ``<table>`` of top gaps.
        """
        fv = result.feature_vector
        if not fv or not fv.top_gaps:
            return '<p class="empty-state">无gap数据</p>'

        parts: List[str] = [
            "<table><thead><tr>",
            "<th>#</th><th>gap时长(μs)</th><th>gap时长(ms)</th>",
            "<th>归因</th><th>设备</th><th>Stream</th>",
            "<th>前驱kernel</th><th>后继kernel</th>",
            "</tr></thead><tbody>",
        ]

        for i, gap in enumerate(fv.top_gaps[:20], 1):
            gap_dur = gap.get("gap_dur", 0)
            gap_ms = float(gap_dur) / 1000.0
            attr = str(gap.get("attribution", "UNKNOWN"))
            device_id = gap.get("device_id", -1)
            stream_id = gap.get("stream_id", -1)
            prev_k = html_mod.escape(str(gap.get("prev_kernel_name", ""))[:30])
            next_k = html_mod.escape(str(gap.get("next_kernel_name", ""))[:30])
            attr_class = f"attr-{attr}"

            parts.append("<tr>")
            parts.append(f"<td>{i}</td>")
            parts.append(f"<td>{gap_dur}</td>")
            parts.append(f"<td>{gap_ms:.2f}</td>")
            parts.append(
                f'<td><span class="bar-fill {attr_class}" '
                f'style="display:inline-block;width:12px;height:12px;'
                f'border-radius:2px;vertical-align:middle;"></span> '
                f"{html_mod.escape(attr)}</td>"
            )
            parts.append(f"<td>{device_id}</td>")
            parts.append(f"<td>{stream_id}</td>")
            parts.append(f"<td>{prev_k}</td>")
            parts.append(f"<td>{next_k}</td>")
            parts.append("</tr>")

        parts.append("</tbody></table>")
        return "\n".join(parts)
