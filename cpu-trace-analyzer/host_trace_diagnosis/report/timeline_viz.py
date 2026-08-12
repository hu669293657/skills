"""
Timeline Visualizer
===================
Generates a Host/Device timeline visualization as a self-contained HTML file.

The visualization uses pure HTML/CSS/SVG (no JavaScript framework) and
shows:
- **Upper section**: Host events organized by thread.
- **Lower section**: Device kernel execution and gaps organized by stream.
- Gap regions are colored by attribution category.
- A time axis with tick marks and labels.
- A legend for attribution colors.

The timeline supports CSS-based zoom (hover to scale) and horizontal
scrolling.

Usage::

    viz = TimelineVisualizer(config)
    viz.generate(result, "reports/timeline_viz.html")
"""
from __future__ import annotations

import html as html_mod
import logging
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from ir.schema import DiagnosisResult, FeatureVector

logger = logging.getLogger("host_trace_diagnosis.report.viz")


# ---------------------------------------------------------------------------
# Color constants
# ---------------------------------------------------------------------------

# Attribution category -> color mapping for gap regions.
ATTR_COLORS: Dict[str, str] = {
    "CPU_SCHED": "#d63031",
    "DATA_LOADER": "#e17055",
    "MEMCPY": "#6c5ce7",
    "LAUNCH_GAP": "#0984e3",
    "IO_WAIT": "#fdcb6e",
    "RUNTIME_BLOCK": "#00b894",
    "OTHER": "#636e72",
    "UNKNOWN": "#b2bec3",
}

# Host event category -> color mapping.
HOST_EVENT_COLORS: Dict[str, str] = {
    "cpu_sched": "#74b9ff",
    "cpu_function": "#a29bfe",
    "memory": "#6c5ce7",
    "io": "#fdcb6e",
    "runtime": "#00cec9",
    "data_loader": "#e17055",
    "python": "#fab1a0",
    "cuda_npu_api": "#0984e3",
    "npu_kernel": "#00b894",
    "stream_sync": "#d63031",
    "unknown": "#b2bec3",
}

DEFAULT_HOST_COLOR: str = "#b2bec3"


class TimelineVisualizer:
    """Generates Host/Device timeline visualization HTML.

    Attributes:
        config: Visualization configuration dict.
        max_gaps: Maximum number of gaps to display.
    """

    def __init__(self, config: dict) -> None:
        """Initialize the timeline visualizer.

        Args:
            config: Configuration dict.  Recognised keys:
                - ``max_gaps_in_viz``: Max gaps to show (default *10*).
        """
        self.config: Dict[str, Any] = config or {}
        self.max_gaps: int = int(self.config.get("max_gaps_in_viz", 10))

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def generate(
        self, result: DiagnosisResult, output_path: str
    ) -> str:
        """Generate the timeline visualization HTML file.

        Args:
            result: DiagnosisResult (must contain a FeatureVector).
            output_path: File path to write the HTML.

        Returns:
            The output path string.
        """
        fv = result.feature_vector

        if fv is None or not fv.top_gaps:
            html_content = self._wrap_html(
                '<p class="empty-state">无gap数据可可视化</p>',
                "Host/Device 时间线可视化",
            )
            self._write_file(output_path, html_content)
            return output_path

        svg_content: str = self._build_svg(fv)
        html_content: str = self._wrap_html(svg_content, "Host/Device 时间线可视化")
        self._write_file(output_path, html_content)

        logger.info("Timeline visualization written to %s", output_path)
        return output_path

    # ------------------------------------------------------------------
    # SVG construction
    # ------------------------------------------------------------------

    def _build_svg(self, fv: FeatureVector) -> str:
        """Build the complete SVG timeline.

        Args:
            fv: FeatureVector containing gaps and host events.

        Returns:
            SVG string.
        """
        gaps: List[dict] = list(fv.top_gaps[: self.max_gaps])
        if not gaps:
            return '<text x="50" y="50" font-size="14">无gap数据</text>'

        # --- Compute time range ---
        min_ts: int = min(int(g.get("gap_start", 0)) for g in gaps)
        max_ts: int = max(int(g.get("gap_end", 0)) for g in gaps)
        time_range: int = max_ts - min_ts
        if time_range <= 0:
            time_range = 1

        # Add 5% margin on each side.
        margin: int = int(time_range * 0.05)
        min_ts -= margin
        max_ts += margin
        time_range = max_ts - min_ts

        # --- Collect host events ---
        host_events: List[dict] = self._collect_host_events(
            fv, min_ts, max_ts
        )

        # --- Determine row assignments ---
        stream_ids: List[int] = sorted(
            set(int(g.get("stream_id", 0)) for g in gaps)
        )
        thread_ids: List[int] = sorted(
            set(int(he.get("tid", -1)) for he in host_events)
        )

        stream_row: Dict[int, int] = {sid: i for i, sid in enumerate(stream_ids)}
        thread_row: Dict[int, int] = {tid: i for i, tid in enumerate(thread_ids)}

        # --- SVG layout constants ---
        label_width: int = 130
        right_margin: int = 30
        content_width: int = 1050
        svg_width: int = label_width + content_width + right_margin

        host_row_h: int = 28
        device_row_h: int = 35
        host_y: int = 50
        host_height: int = max(len(thread_ids), 1) * host_row_h
        device_y: int = host_y + host_height + 25
        device_height: int = max(len(stream_ids), 1) * device_row_h
        axis_y: int = device_y + device_height + 15
        svg_height: int = axis_y + 35

        # --- Scale functions ---
        def scale_x(ts: float) -> float:
            return label_width + (ts - min_ts) / time_range * content_width

        def scale_w(dur: float) -> float:
            w = dur / time_range * content_width
            return max(w, 2.0)

        elements: List[str] = []

        # --- Section labels ---
        elements.append(
            f'<text x="10" y="35" font-size="14" font-weight="bold" '
            f'fill="#2d3436">Host 事件流</text>'
        )
        elements.append(
            f'<text x="10" y="{device_y - 8}" font-size="14" '
            f'font-weight="bold" fill="#2d3436">Device Kernel 流</text>'
        )

        # --- Host section rows ---
        for i, tid in enumerate(thread_ids):
            y = host_y + i * host_row_h
            elements.append(
                f'<text x="10" y="{y + 18}" font-size="11" '
                f'fill="#636e72">Thread {tid}</text>'
            )
            elements.append(
                f'<line x1="{label_width}" y1="{y + host_row_h}" '
                f'x2="{svg_width - right_margin}" '
                f'y2="{y + host_row_h}" stroke="#e1e8ed" stroke-width="0.5"/>'
            )

        # If no host events, show placeholder.
        if not thread_ids:
            elements.append(
                f'<text x="{label_width + 10}" y="{host_y + 18}" '
                f'font-size="12" fill="#b2bec3">（无Host事件数据）</text>'
            )

        # --- Draw host events ---
        for he in host_events:
            x = scale_x(float(he.get("ts", 0)))
            w = scale_w(float(he.get("dur", 0)))
            row = thread_row.get(int(he.get("tid", -1)), 0)
            y = host_y + row * host_row_h + 4
            cat = str(he.get("cat", "unknown"))
            color = HOST_EVENT_COLORS.get(cat, DEFAULT_HOST_COLOR)
            name = html_mod.escape(str(he.get("name", ""))[:18])

            elements.append(
                f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" '
                f'height="20" fill="{color}" rx="2" '
                f'title="{name}"/>'
            )
            if w > 35:
                elements.append(
                    f'<text x="{x + 3:.1f}" y="{y + 14}" '
                    f'font-size="9" fill="white">{name}</text>'
                )

        # --- Device section rows ---
        for i, sid in enumerate(stream_ids):
            y = device_y + i * device_row_h
            elements.append(
                f'<text x="10" y="{y + 22}" font-size="11" '
                f'fill="#636e72">Stream {sid}</text>'
            )
            elements.append(
                f'<line x1="{label_width}" y1="{y + device_row_h}" '
                f'x2="{svg_width - right_margin}" '
                f'y2="{y + device_row_h}" stroke="#e1e8ed" '
                f'stroke-width="0.5"/>'
            )

        # --- Draw device gaps ---
        for gap in gaps:
            sid = int(gap.get("stream_id", 0))
            row = stream_row.get(sid, 0)
            x = scale_x(float(gap.get("gap_start", 0)))
            w = scale_w(float(gap.get("gap_dur", 0)))
            y = device_y + row * device_row_h + 5
            attr = str(gap.get("attribution", "UNKNOWN"))
            color = ATTR_COLORS.get(attr, ATTR_COLORS["UNKNOWN"])
            gap_dur = int(gap.get("gap_dur", 0))
            gap_ms = gap_dur / 1000.0
            prev_k = html_mod.escape(str(gap.get("prev_kernel_name", ""))[:15])
            next_k = html_mod.escape(str(gap.get("next_kernel_name", ""))[:15])

            # Draw gap rectangle.
            elements.append(
                f'<rect x="{x:.1f}" y="{y}" width="{w:.1f}" '
                f'height="25" fill="{color}" rx="2" opacity="0.88" '
                f'title="Gap: {gap_ms:.1f}ms ({attr})"/>'
            )

            # Draw prev kernel marker (small green bar before gap).
            if w > 5:
                elements.append(
                    f'<rect x="{x - 6:.1f}" y="{y + 2}" width="5" '
                    f'height="21" fill="#00b894" rx="1" '
                    f'title="Prev: {prev_k}"/>'
                )

            # Draw next kernel marker (small green bar after gap).
            if w > 5:
                elements.append(
                    f'<rect x="{x + w + 1:.1f}" y="{y + 2}" width="5" '
                    f'height="21" fill="#00b894" rx="1" '
                    f'title="Next: {next_k}"/>'
                )

            # Label inside gap if wide enough.
            if w > 50:
                elements.append(
                    f'<text x="{x + 4:.1f}" y="{y + 17}" '
                    f'font-size="10" fill="white">{attr} '
                    f'{gap_ms:.1f}ms</text>'
                )

        # --- Time axis ---
        elements.append(
            f'<line x1="{label_width}" y1="{axis_y}" '
            f'x2="{svg_width - right_margin}" y2="{axis_y}" '
            f'stroke="#636e72" stroke-width="1"/>'
        )
        for i in range(11):
            px = label_width + i * content_width / 10
            ts_val = min_ts + i * time_range / 10
            ts_ms = ts_val / 1000.0
            elements.append(
                f'<line x1="{px:.1f}" y1="{axis_y}" '
                f'x2="{px:.1f}" y2="{axis_y + 5}" '
                f'stroke="#636e72" stroke-width="1"/>'
            )
            elements.append(
                f'<text x="{px:.1f}" y="{axis_y + 18}" '
                f'font-size="10" text-anchor="middle" '
                f'fill="#636e72">{ts_ms:.1f}ms</text>'
            )

        # --- Legend ---
        legend_y: int = axis_y + 30
        legend_x: int = label_width
        elements.append(
            f'<text x="{legend_x}" y="{legend_y}" font-size="11" '
            f'fill="#636e72">归因图例:</text>'
        )
        legend_x += 60
        for attr, color in ATTR_COLORS.items():
            if attr == "UNKNOWN":
                continue
            elements.append(
                f'<rect x="{legend_x}" y="{legend_y - 9}" width="12" '
                f'height="12" fill="{color}" rx="2"/>'
            )
            elements.append(
                f'<text x="{legend_x + 16}" y="{legend_y}" '
                f'font-size="10" fill="#636e72">{attr}</text>'
            )
            legend_x += 90

        svg_tag: str = (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{svg_width}" height="{svg_height + 15}" '
            f'viewBox="0 0 {svg_width} {svg_height + 15}">'
        )
        return svg_tag + "".join(elements) + "</svg>"

    # ------------------------------------------------------------------
    # Host event collection
    # ------------------------------------------------------------------

    def _collect_host_events(
        self, fv: FeatureVector, min_ts: int, max_ts: int
    ) -> List[dict]:
        """Collect and filter host events from gap_host_op_pairs.

        Args:
            fv: FeatureVector to extract from.
            min_ts: Minimum timestamp (for filtering).
            max_ts: Maximum timestamp (for filtering).

        Returns:
            List of host event dicts with keys *ts*, *dur*, *tid*,
            *name*, *cat*.
        """
        events: List[dict] = []

        for pair in fv.gap_host_op_pairs:
            if not isinstance(pair, dict):
                continue

            # Try various structures to find host_events.
            host_events_raw: Any = pair.get("host_events", [])
            if not host_events_raw and isinstance(pair.get("gap"), dict):
                host_events_raw = pair["gap"].get("host_events", [])

            if not isinstance(host_events_raw, list):
                continue

            for he in host_events_raw:
                if not isinstance(he, dict):
                    continue
                ts = int(he.get("ts", 0))
                dur = int(he.get("dur", 0))
                tid = int(he.get("tid", -1))
                name = str(he.get("name", ""))
                cat = str(he.get("cat", "unknown"))

                # Filter to the visible time range (with small buffer).
                if ts < min_ts - 5000 or ts > max_ts + 5000:
                    continue

                events.append({
                    "ts": ts,
                    "dur": dur,
                    "tid": tid,
                    "name": name,
                    "cat": cat,
                })

        return events

    # ------------------------------------------------------------------
    # HTML wrapping
    # ------------------------------------------------------------------

    def _wrap_html(self, content: str, title: str) -> str:
        """Wrap SVG or content in a complete self-contained HTML document.

        Includes inline CSS with dark/light theme support and a
        hover-to-zoom effect for the timeline.

        Args:
            content: SVG string or HTML content.
            title: Page title.

        Returns:
            Complete HTML document string.
        """
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{html_mod.escape(title)}</title>
    <style>
        :root {{
            --bg-primary: #f0f2f5;
            --bg-card: #ffffff;
            --text-primary: #2d3436;
            --text-secondary: #636e72;
            --border-color: #e1e8ed;
            --shadow: 0 2px 12px rgba(0,0,0,0.06);
        }}
        @media (prefers-color-scheme: dark) {{
            :root {{
                --bg-primary: #0d1117;
                --bg-card: #161b22;
                --text-primary: #e6edf3;
                --text-secondary: #8b949e;
                --border-color: #30363d;
                --shadow: 0 2px 12px rgba(0,0,0,0.3);
            }}
        }}
        * {{ margin: 0; padding: 0; box-sizing: border-box; }}
        body {{
            font-family: -apple-system, BlinkMacSystemFont, "Segoe UI",
                         Roboto, sans-serif;
            background: var(--bg-primary);
            color: var(--text-primary);
            line-height: 1.6;
        }}
        .container {{
            max-width: 1300px;
            margin: 0 auto;
            padding: 20px;
        }}
        h1 {{
            font-size: 20px;
            font-weight: 600;
            margin-bottom: 16px;
        }}
        .timeline-wrapper {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 12px;
            padding: 20px;
            box-shadow: var(--shadow);
            overflow-x: auto;
        }}
        .timeline-content {{
            min-width: 1200px;
            transform-origin: top left;
            transition: transform 0.3s ease;
        }}
        .timeline-wrapper:hover .timeline-content {{
            transform: scale(1.4);
        }}
        .empty-state {{
            text-align: center;
            padding: 40px;
            color: var(--text-secondary);
            font-size: 14px;
        }}
        .info-bar {{
            background: var(--bg-card);
            border: 1px solid var(--border-color);
            border-radius: 8px;
            padding: 12px 20px;
            margin-bottom: 16px;
            font-size: 13px;
            color: var(--text-secondary);
        }}
    </style>
</head>
<body>
    <div class="container">
        <h1>{html_mod.escape(title)}</h1>
        <div class="info-bar">
            提示: 将鼠标悬停在时间线上可放大查看，使用横向滚动条浏览完整时间线。
        </div>
        <div class="timeline-wrapper">
            <div class="timeline-content">
                {content}
            </div>
        </div>
    </div>
</body>
</html>"""

    # ------------------------------------------------------------------
    # File writing
    # ------------------------------------------------------------------

    def _write_file(self, path: str, content: str) -> None:
        """Write content to a file, creating parent directories.

        Args:
            path: Output file path.
            content: HTML content to write.
        """
        file_path = Path(path)
        try:
            file_path.parent.mkdir(parents=True, exist_ok=True)
            with open(file_path, "w", encoding="utf-8") as fh:
                fh.write(content)
        except OSError as exc:
            logger.error("Failed to write timeline visualization: %s", exc)
            raise
