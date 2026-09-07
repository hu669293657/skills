# -*- coding: utf-8 -*-
"""固定 18 章节 HTML 模板（DESIGN.md §10）。

原则：
- 章节顺序永不变化；无数据的章节必须显式渲染"数据缺失"占位说明，绝不编造；
- 零 JS、零外部资源：样式内联 CSS、图形全部为内联 SVG（charts.py 产出）；
- 输入是 AnalysisBundle.to_dict() 的结构（diagnosis/sections/parse_stats/
  derived_features/dq/case_info/series），对任何键缺失都做缺省处理。
"""

import json

from . import charts

# 9 轴雷达固定轴序（与规则引擎 risk_dimensions 口径一致）
RADAR_AXES = ("CPU", "Memory", "NUMA", "Thread", "Scheduler",
              "IO", "Runtime", "Data Pipeline", "Host-Device")

SEV_CLASS = {"CRITICAL": "sev-critical", "HIGH": "sev-high",
             "MEDIUM": "sev-medium", "LOW": "sev-low", "INFO": "sev-info"}

LV_LABEL = {"ok": "正常", "warn": "警告", "bad": "异常",
            "info": "信息", "na": "缺失"}

SRC_CLASS = {"present": "lv-ok", "partial": "lv-warn",
             "missing": "lv-na", "error": "lv-bad"}
SRC_LABEL = {"present": "已解析", "partial": "部分", "missing": "缺失", "error": "出错"}

_PRI_CLASS = {"P0": "sev-critical", "P1": "sev-high", "P2": "sev-low", "P3": "sev-info"}


# ---------------------------------------------------------------------------
# 基础工具
# ---------------------------------------------------------------------------

def _esc(s):
    """HTML 文本转义。"""
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;"))


def _fmtval(v):
    """指标值展示：bool 是/否，浮点去尾零，None -> '--'。"""
    if v is None:
        return "--"
    if isinstance(v, bool):
        return "是" if v else "否"
    if isinstance(v, float):
        if v == int(v) and abs(v) < 1e15:
            return str(int(v))
        return ("%.4f" % v).rstrip("0").rstrip(".")
    return str(v)


def _pct(conf):
    """0..1 置信度 -> 百分比文本。"""
    try:
        return "%.0f%%" % (float(conf) * 100.0)
    except (TypeError, ValueError):
        return "--"


def _risk_color(v):
    """0-100 风险分 -> 绿黄红渐变色（与 charts.heatmap 同口径）。"""
    try:
        v = max(0.0, min(100.0, float(v)))
    except (TypeError, ValueError):
        return "#9e9e9e"
    lo, mid, hi = (46, 125, 50), (249, 168, 37), (211, 47, 47)
    if v <= 50:
        t, a, b = v / 50.0, lo, mid
    else:
        t, a, b = (v - 50.0) / 50.0, mid, hi
    rgb = tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))
    return "#%02x%02x%02x" % rgb


def _score_level(score):
    """Host Bound Score -> gauge 弧色档位。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "na"
    if s >= 60:
        return "bad"
    if s >= 40:
        return "warn"
    return "ok"


def _risk_level(v):
    """0-100 风险分 -> gauge 弧色档位。"""
    try:
        x = float(v)
    except (TypeError, ValueError):
        return "na"
    if x >= 60:
        return "bad"
    if x >= 30:
        return "warn"
    return "ok"


def _sev_badge(sev):
    return '<span class="badge %s">%s</span>' % (
        SEV_CLASS.get(sev, "sev-info"), _esc(sev))


def _lv_badge(lv):
    return '<span class="badge lv-%s">%s</span>' % (
        _esc(lv), LV_LABEL.get(lv, lv))


def _src_badge(status):
    return '<span class="badge %s">%s</span>' % (
        SRC_CLASS.get(status, "lv-na"), SRC_LABEL.get(status, status))


def _pri_badge(level):
    return '<span class="badge %s">%s</span>' % (
        _PRI_CLASS.get(level, "sev-info"), _esc(level or "P?"))


def _kv(label, value, mono=False):
    """报告头/信息条的 键值 对。"""
    cls = ' class="mono"' if mono else ""
    return '<div class="kv"><span class="kv-k">%s</span>' \
           '<span class="kv-v"%s>%s</span></div>' % (_esc(label), cls, value)


# ---------------------------------------------------------------------------
# 数据访问辅助（对缺失键全部缺省）
# ---------------------------------------------------------------------------

def _diag(data):
    d = data.get("diagnosis")
    return d if isinstance(d, dict) else {}


def _risk(d):
    r = d.get("risk_dimensions")
    return r if isinstance(r, dict) else {}


def _sec_by_dim(sections, dim):
    for s in (sections or []):
        if isinstance(s, dict) and s.get("dim") == dim:
            return s
    return None


def _metric_map(sections):
    """所有 section 指标扁平索引：feature key -> metric dict。"""
    out = {}
    for s in (sections or []):
        if not isinstance(s, dict):
            continue
        for m in (s.get("metrics") or []):
            if isinstance(m, dict) and m.get("key"):
                out[m["key"]] = m
    return out


def _metric_value(mmap, key):
    m = mmap.get(key)
    if not m:
        return None
    return m.get("value")


# ---------------------------------------------------------------------------
# 章节骨架
# ---------------------------------------------------------------------------

def _section(num, title, inner, note=""):
    """固定章节骨架：<section id="sec-XX">。"""
    head = ('<div class="sec-head"><span class="sec-no">%02d</span>'
            '<h2>%s</h2>%s</div>'
            % (num, _esc(title),
               ('<span class="sec-note">%s</span>' % _esc(note)) if note else ""))
    return '<section class="card" id="sec-%02d">%s<div class="sec-body">%s</div></section>' \
           % (num, head, inner)


def _empty_box(msg):
    return '<div class="empty-box">%s</div>' % _esc(msg)


def _table(headers, rows, cls=""):
    """通用表格：rows 为 list[list[html]]（内容需已转义/或受控 HTML）。"""
    th = "".join("<th>%s</th>" % h for h in headers)
    trs = []
    for r in rows:
        trs.append("<tr>%s</tr>" % "".join("<td>%s</td>" % c for c in r))
    if not trs:
        return _empty_box("表格无数据行")
    return '<table class="tbl %s"><thead><tr>%s</tr></thead><tbody>%s</tbody></table>' \
           % (cls, th, "".join(trs))


def _pre(obj):
    return '<pre class="code">%s</pre>' % _esc(
        json.dumps(obj, ensure_ascii=False, indent=2, sort_keys=True,
                   default=str))


# ---------------------------------------------------------------------------
# 01 Executive Summary
# ---------------------------------------------------------------------------

def _gauge_cell(label, value, level, unit=""):
    return '<div class="gauge-cell">%s</div>' % charts.gauge(
        label, value, level=level, unit=unit)


def _render_summary(data):
    d = _diag(data)
    risk = _risk(d)
    mmap = _metric_map(data.get("sections"))
    score = d.get("host_bound_score")

    gauges = [
        _gauge_cell("Host Bound Score", score, _score_level(score), "分"),
        _gauge_cell("CPU 利用率", _metric_value(mmap, "cpu.util_pct"),
                    "info" if _metric_value(mmap, "cpu.util_pct") is not None else "na", "%"),
        _gauge_cell("设备利用率", _metric_value(mmap, "device.util_pct"),
                    "info" if _metric_value(mmap, "device.util_pct") is not None else "na", "%"),
        _gauge_cell("CPU 风险", risk.get("CPU"), _risk_level(risk.get("CPU"))),
        _gauge_cell("内存压力", risk.get("Memory"), _risk_level(risk.get("Memory"))),
        _gauge_cell("NUMA 风险", risk.get("NUMA"), _risk_level(risk.get("NUMA"))),
        _gauge_cell("线程风险", risk.get("Thread"), _risk_level(risk.get("Thread"))),
        _gauge_cell("I/O 风险", risk.get("IO"), _risk_level(risk.get("IO"))),
        _gauge_cell("调度风险", risk.get("Scheduler"), _risk_level(risk.get("Scheduler"))),
    ]
    gauge_row = '<div class="gauge-row">%s</div>' % "".join(gauges)

    if risk:
        radar = charts.radar_chart(risk, axes=RADAR_AXES)
    else:
        radar = charts.placeholder("风险维度数据缺失（无有效诊断发现），雷达图不可用")

    concl = (
        '<div class="concl">'
        '<div class="concl-line">%s <b>%s</b>（评分 %s / 置信度 %s）</div>'
        '<div class="concl-class">判定分类：%s</div>'
        '<p class="concl-stmt">%s</p>'
        '<div class="concl-tip">图表说明：仪表盘弧色仅表达分档（绿=低/黄=中/红=高），'
        'CPU 与设备利用率为实测展示；数据缺失项显示 "--"，绝不以估计值替代。</div>'
        '</div>'
        % (_sev_badge(d.get("status", "INFO")),
           _esc(d.get("score_label", "未知")),
           _fmtval(score), _pct(d.get("confidence")),
           _esc(d.get("classification", "Unknown")),
           _esc(d.get("statement") or "诊断引擎未产出结论语句（无有效发现）。")))
    grid = '<div class="two-col"><div class="radar-box"><h3>九维风险雷达</h3>%s</div>%s</div>' \
           % (radar, concl)
    return gauge_row + grid


# ---------------------------------------------------------------------------
# 02 Overall Diagnosis / 14 Evidence 共用 findings 数据
# ---------------------------------------------------------------------------

def _render_findings_table(findings):
    rows = []
    for f in (findings or []):
        rec = f.get("recommendation") or {}
        rows.append([
            _sev_badge(f.get("severity", "INFO")),
            '<code>%s</code>' % _esc(f.get("rule_id", "-")),
            '<b>%s</b><div class="sub">%s</div>' % (
                _esc(f.get("title", "")), _esc(f.get("diagnosis_type") or "")),
            _pct(f.get("confidence")),
            '<div class="impact">%s</div>' % _esc(f.get("impact", "")),
            '%s %s' % (_pri_badge(rec.get("level", "")),
                       _esc(rec.get("action", ""))),
        ])
    return _table(["级别", "规则", "发现", "置信度", "影响", "建议动作"], rows)


def _render_diagnosis(data):
    d = _diag(data)
    findings = d.get("findings") or []
    head = (
        '<div class="diag-head">'
        '<div>%s <b>%s</b>　置信度 <b>%s</b>　分类 <code>%s</code></div>'
        '<p>%s</p></div>'
        % (_sev_badge(d.get("status", "INFO")),
           _esc(d.get("score_label", "未知")), _pct(d.get("confidence")),
           _esc(d.get("classification", "Unknown")),
           _esc(d.get("statement") or "无结论语句。")))
    if findings:
        tbl = _render_findings_table(findings)
        tip = '<div class="note">共 %d 条发现，按严重级别与置信度降序；证据明细见 §14。</div>' \
              % len(findings)
    else:
        tbl = _empty_box("无规则命中：未检测到 Host Bound 相关异常（或数据不足以判定）。")
        tip = ""
    return head + tbl + tip


# ---------------------------------------------------------------------------
# 03 Host Bound Assessment
# ---------------------------------------------------------------------------

def _render_assessment(data):
    d = _diag(data)
    risk = _risk(d)
    score = d.get("host_bound_score")
    band, sev = _band_of(score)

    big = ('<div class="score-line">'
           '<span class="score-big" style="color:%s">%s</span>'
           '<span class="score-desc">/ 100　%s　置信度 %s</span>'
           '</div>'
           % (_risk_color(score), _fmtval(score), _esc(band),
              _pct(d.get("confidence"))))
    cls_line = '<div class="note">判定分类：<b>%s</b>；%s</div>' % (
        _esc(d.get("classification", "Unknown")),
        _esc(d.get("statement") or ""))

    if risk:
        vals = [risk.get(a) for a in RADAR_AXES]
        heat = charts.heatmap_strip(vals, labels=list(RADAR_AXES))
        rows = []
        for a in RADAR_AXES:
            v = risk.get(a)
            rows.append([
                _esc(a),
                '<span class="heat-chip" style="background:%s">%s</span>'
                % (_risk_color(v), _fmtval(v)),
                _lv_badge(_risk_level(v)) if v is not None else _lv_badge("na"),
            ])
        tbl = _table(["风险维度", "风险分（0-100）", "档位"], rows)
        heat_block = ('<h3>九维风险热力条</h3>%s%s' % (heat, tbl))
    else:
        heat_block = _empty_box("风险维度数据缺失（无有效发现），无法展示九维热力。")

    return big + cls_line + heat_block


def _band_of(score):
    """复现 SCORE_BANDS 的分档文案（与 diagnosis.score_band 一致）。"""
    try:
        s = float(score)
    except (TypeError, ValueError):
        return "未知", "INFO"
    if s <= 20:
        return "基本不存在", "LOW"
    if s <= 40:
        return "轻微", "LOW"
    if s <= 60:
        return "疑似", "MEDIUM"
    if s <= 80:
        return "较明显", "HIGH"
    return "强 Host Bound", "CRITICAL"


# ---------------------------------------------------------------------------
# 通用维度章节（04-09, 12）
# ---------------------------------------------------------------------------

def _render_dim_section(data, dim, fallback_title):
    sec = _sec_by_dim(data.get("sections"), dim)
    if sec is None:
        return _empty_box(
            "本章节固定保留。当前采集/解析未产出“%s”维度分析视图"
            "（对应数据缺失或该维度分析器未运行），不作任何推测。" % fallback_title)

    parts = []
    if sec.get("verdict"):
        parts.append('<p class="verdict"><b>维度结论：</b>%s</div></p>'
                     .replace("</div>", "") % _esc(sec["verdict"]))
    rows = []
    for m in (sec.get("metrics") or []):
        rows.append([
            '<code>%s</code>' % _esc(m.get("key", "")),
            _esc(m.get("label", "")),
            '<b>%s</b>' % _fmtval(m.get("value")),
            _esc(m.get("unit", "")),
            _lv_badge(m.get("level", "na")),
            _esc(m.get("note", "")),
        ])
    parts.append(_table(["特征 key", "指标", "值", "单位", "级别", "说明"], rows))

    missing = sec.get("missing") or []
    if missing:
        parts.append('<div class="warn-box"><b>数据缺口（%d 项，相关判定已降级/跳过）：</b><ul>%s</ul></div>'
                     % (len(missing),
                        "".join("<li>%s</li>" % _esc(x) for x in missing)))
    files = sec.get("files") or []
    if files:
        parts.append('<div class="files">证据来源：%s</div>'
                     % "".join('<code>%s</code>' % _esc(x) for x in files))
    return "".join(parts)


# ---------------------------------------------------------------------------
# 10 Host-Device Timeline
# ---------------------------------------------------------------------------

def _render_timeline(data):
    series = data.get("series") or {}
    pts_dev = series.get("device.util") or []
    pts_cpu = series.get("cpu.util") or []
    chart = charts.line_chart(
        "设备利用率 vs CPU 利用率（横轴：采集相对秒）",
        [{"name": "设备利用率", "color": "#1976d2", "points": pts_dev},
         {"name": "CPU 利用率", "color": "#e65100", "points": pts_cpu}],
        y_max=100)
    notes = []
    if pts_dev:
        notes.append("device.util 共 %d 点" % len(pts_dev))
    if pts_cpu:
        notes.append("cpu.util 共 %d 点" % len(pts_cpu))
    note = ('<div class="note">判读要点：CPU 利用率持续高位而设备利用率同步低迷，'
            '是典型 Host Bound 形态；两条曲线均为 0-100%%。%s</div>'
            % ("；".join(notes) if notes else ""))
    return chart + note


# ---------------------------------------------------------------------------
# 11 Framework Analysis
# ---------------------------------------------------------------------------

def _render_framework(data):
    ci = data.get("case_info") or {}
    mmap = _metric_map(data.get("sections"))
    rows = []

    fw = ci.get("framework")
    if not fw and isinstance(ci.get("frameworks"), list) and ci["frameworks"]:
        try:
            fw = ", ".join(str(x) for x in ci["frameworks"])
        except Exception:
            fw = None
    rows.append(["框架", _esc(fw) if fw else "--"])
    rows.append(["框架版本", _esc(ci.get("framework_version") or "--")])

    for key in ("thread.dataloader_workers", "thread.omp_threads_observed"):
        m = mmap.get(key)
        if m:
            rows.append([_esc(m.get("label", key)),
                         "%s %s" % (_fmtval(m.get("value")),
                                    _esc(m.get("unit", "")))])
    has_any = bool(fw) or ci.get("framework_version") or any(
        mmap.get(k) for k in ("thread.dataloader_workers",
                              "thread.omp_threads_observed"))
    tbl = _table(["项目", "取值"], rows)
    if has_any:
        note = ('<div class="note">框架信息来源于采集清单/框架日志/进程命令行；'
                '缺失项显示 "--"。</div>')
    else:
        note = _empty_box(
            "未采集到框架信息（缺少 target_summary/framework 日志或进程命令行"
            "无法识别训练框架），本章节仅保留占位，不作推测。")
    return tbl + note


# ---------------------------------------------------------------------------
# 13 Root Cause
# ---------------------------------------------------------------------------

def _render_root_cause(data):
    d = _diag(data)
    tree = d.get("root_cause_tree")
    if not tree:
        return _empty_box("根因树未生成（无有效发现时不构建根因树）。")
    return charts.tree_svg(tree) + _root_cause_legend(tree)


def _root_cause_legend(root):
    """树的文字版明细（含 finding 关联），保证信息不因 SVG 截断而丢失。"""
    lines = []

    def walk(node, depth):
        if not isinstance(node, dict):
            return
        lines.append('<div class="rc-line" style="margin-left:%dpx">'
                     '<span class="heat-chip" style="background:%s">%s</span> '
                     '<b>%s</b>（置信度 %s）%s</div>'
                     % (depth * 20, _risk_color(node.get("score")),
                        _fmtval(node.get("score")), _esc(node.get("name", "")),
                        _pct(node.get("confidence")),
                        ('　<code>%s</code>' % _esc(",".join(node.get("finding_ids") or [])))
                        if node.get("finding_ids") else ""))
        for c in (node.get("children") or []):
            walk(c, depth + 1)

    walk(root, 0)
    return ('<h3>根因树明细</h3><div class="rc-list">%s</div>' % "".join(lines))


# ---------------------------------------------------------------------------
# 14 Evidence
# ---------------------------------------------------------------------------

def _render_evidence(data):
    findings = _diag(data).get("findings") or []
    if not findings:
        return _empty_box("无发现，无证据可展示。")
    parts = []
    n_ev = 0
    for f in findings:
        evs = f.get("evidence") or []
        if not evs:
            continue
        items = []
        for e in evs:
            n_ev += 1
            val = _fmtval(e.get("value"))
            unit = e.get("unit") or ""
            summary = "%s:%s　%s=%s%s" % (
                e.get("source") or "未知来源", e.get("lines") or "-",
                e.get("metric") or "-", val, (" " + unit) if unit else "")
            body = []
            if e.get("snippet"):
                body.append('<pre class="code">%s</pre>' % _esc(e["snippet"]))
            meta = ["来源: %s" % (e.get("source") or "-"),
                    "行: %s" % (e.get("lines") or "-"),
                    "值: %s %s" % (val, unit)]
            if e.get("note"):
                meta.append("备注: %s" % e["note"])
            body.append('<div class="ev-meta">%s</div>'
                        % "　|　".join(_esc(x) for x in meta))
            items.append('<details><summary>%s</summary>%s</details>'
                         % (_esc(summary), "".join(body)))
        parts.append('<h4>%s %s</h4><div class="ev-group">%s</div>'
                     % (_sev_badge(f.get("severity", "INFO")),
                        _esc(f.get("title", "")), "".join(items)))
    if n_ev == 0:
        return _empty_box("发现存在但均未附证据（异常情况，请检查规则定义）。")
    tip = '<div class="note">共 %d 条发现、%d 条证据。证据为采集产物原文摘录（截断至 400 字符），可回溯到具体文件与行号。</div>' \
          % (len(findings), n_ev)
    return tip + "".join(parts)


# ---------------------------------------------------------------------------
# 15 Optimization Suggestions
# ---------------------------------------------------------------------------

def _render_suggestions(data):
    pris = _diag(data).get("priorities") or []
    if not pris:
        return _empty_box("无优化建议（无有效发现时不生成建议）。")
    rows = []
    for i, p in enumerate(pris, 1):
        rows.append([
            str(i),
            _pri_badge(p.get("level", "")),
            '<b>%s</b>' % _esc(p.get("action", "")),
            _esc(p.get("expected", "")),
            _esc(p.get("verify", "")),
            _esc(p.get("certainty", "")),
            _esc(p.get("risk", "")),
        ])
    tbl = _table(["#", "优先级", "动作", "预期收益", "验证方法", "确定性", "风险"], rows)
    note = ('<div class="note">P0 立即执行 / P1 高优 / P2 常规 / P3 观察；'
            "“建议实验验证”项请先在测试环境验证后再上线。</div>")
    return tbl + note


# ---------------------------------------------------------------------------
# 16 Risk and Impact
# ---------------------------------------------------------------------------

def _render_risk(data):
    d = _diag(data)
    risk = _risk(d)
    if not risk:
        return _empty_box("风险维度数据缺失（无有效发现）。")
    rows = []
    for a in RADAR_AXES:
        v = risk.get(a)
        if v is None:
            continue
        rows.append([_esc(a),
                     '<span class="heat-chip" style="background:%s">%s</span>'
                     % (_risk_color(v), _fmtval(v)),
                     _lv_badge(_risk_level(v))])
    tbl = _table(["风险维度", "风险分", "档位"], rows)

    impacts = []
    for f in (d.get("findings") or [])[:3]:
        if f.get("impact"):
            impacts.append('<li><b>%s</b>：%s</li>'
                           % (_esc(f.get("title", "")), _esc(f["impact"])))
    imp_html = ('<h3>主要影响（Top 发现）</h3><ul class="impact-list">%s</ul>'
                % "".join(impacts)) if impacts else ""
    return tbl + imp_html


# ---------------------------------------------------------------------------
# 17 Collection Information
# ---------------------------------------------------------------------------

def _render_collection(data):
    dq = data.get("dq") or {}
    ci = data.get("case_info") or {}
    parts = []

    grade = dq.get("grade") or "unavailable"
    grade_badge = '<span class="badge %s">%s</span>' % (
        SRC_CLASS.get({"complete": "present", "partial": "partial",
                       "insufficient": "missing",
                       "unavailable": "error"}.get(grade, "error")),
        {"complete": "完整", "partial": "部分完整", "insufficient": "不足",
         "unavailable": "不可用"}.get(grade, grade))
    parts.append('<div class="dq-line"><b>数据质量等级：</b>%s</div>' % grade_badge)

    sources = dq.get("sources") or {}
    if sources:
        rows = []
        for name in sorted(sources):
            info = sources.get(name) or {}
            rows.append([_esc(name), _src_badge(info.get("status", "missing")),
                         _esc(info.get("note", ""))])
        parts.append(_table(["数据源", "状态", "说明"], rows))
    else:
        parts.append(_empty_box("无数据源记录。"))

    lims = dq.get("limitations") or []
    if lims:
        parts.append('<div class="warn-box"><b>结论适用性限制（%d 条）：</b><ul>%s</ul></div>'
                     % (len(lims), "".join("<li>%s</li>" % _esc(x) for x in lims)))
    warns = dq.get("warnings") or []
    if warns:
        parts.append('<details class="warn-details"><summary>解析/分析警告（%d 条）</summary>'
                     '<ul>%s</ul></details>'
                     % (len(warns), "".join("<li>%s</li>" % _esc(x) for x in warns)))

    parts.append('<h3>解析统计（parse_stats）</h3>' + _pre(data.get("parse_stats") or {}))

    env_rows = [
        [_esc("collection_path"), _esc(ci.get("collection_path", "--"))],
        [_esc("duration_s"), _fmtval(ci.get("duration_s"))],
        [_esc("sanitized"), "是" if ci.get("sanitized") else ("否" if "sanitized" in ci else "--")],
        [_esc("tool_version"), _esc(ci.get("tool_version") or "--")],
    ]
    parts.append('<h3>采集环境</h3>' + _table(["项", "值"], env_rows))
    return "".join(parts)


# ---------------------------------------------------------------------------
# 18 Raw Data Appendix
# ---------------------------------------------------------------------------

def _render_appendix(data):
    parts = []
    derived = data.get("derived_features") or []
    if derived:
        chips = "".join('<code>%s</code>' % _esc(x) for x in derived)
        parts.append('<h3>特征工程派生项（%d）</h3><div class="chips">%s</div>'
                     % (len(derived), chips))
    else:
        parts.append(_empty_box("无特征工程派生项。"))

    series = data.get("series") or {}
    if series:
        rows = []
        for k in sorted(series):
            pts = series.get(k) or []
            if pts:
                try:
                    span = "%s ~ %s" % (_fmtval(pts[0][0]), _fmtval(pts[-1][0]))
                except (IndexError, TypeError, ValueError):
                    span = "-"
            else:
                span = "-"
            rows.append([_esc(k), str(len(pts)), _esc(span)])
        parts.append('<h3>时序序列清单（%d）</h3>' % len(series)
                     + _table(["kind.key", "点数", "时间范围"], rows))
    else:
        parts.append(_empty_box("无时序序列（未采集周期性数据）。"))

    ci = data.get("case_info") or {}
    extra = {}
    for k in ("capabilities", "env_keys", "device", "os", "cpu_model",
              "cpu_count", "numa_nodes", "sockets", "collection_time"):
        if k in ci:
            extra[k] = ci[k]
    parts.append('<h3>采集清单元数据（case_info 摘要）</h3>' + _pre(extra or {}))
    return "".join(parts)


# ---------------------------------------------------------------------------
# 组装
# ---------------------------------------------------------------------------

_SECTIONS = [
    (1, "Executive Summary", None),
    (2, "Overall Diagnosis", None),
    (3, "Host Bound Assessment", None),
    (4, "CPU Analysis", ("cpu", "CPU 分析")),
    (5, "Thread Analysis", ("thread", "线程分析")),
    (6, "Scheduler Analysis", ("scheduler", "调度器分析")),
    (7, "NUMA Analysis", ("numa", "NUMA 分析")),
    (8, "Memory Analysis", ("memory", "内存分析")),
    (9, "I/O Analysis", ("io", "IO 分析")),
    (10, "Host-Device Timeline", None),
    (11, "Framework Analysis", None),
    (12, "Profiler Analysis", ("perf", "Perf 微架构分析")),
    (13, "Root Cause", None),
    (14, "Evidence", None),
    (15, "Optimization Suggestions", None),
    (16, "Risk and Impact", None),
    (17, "Collection Information", None),
    (18, "Raw Data Appendix", None),
]


def _render_header(data):
    ci = data.get("case_info") or {}
    d = _diag(data)
    dq = data.get("dq") or {}
    meta = "".join([
        _kv("Case ID", _esc(ci.get("case_id") or "--"), mono=True),
        _kv("主机", _esc(ci.get("hostname") or ci.get("host.hostname") or "--")),
        _kv("OS / 内核", _esc("%s / %s" % (ci.get("os") or "--",
                                          ci.get("kernel") or "--"))),
        _kv("架构", _esc(ci.get("arch") or "--")),
        _kv("CPU", _esc(ci.get("cpu_model") or "--")),
        _kv("逻辑核", _fmtval(ci.get("cpu_count"))),
        _kv("采集时间", _esc(ci.get("collection_time") or "--")),
        _kv("数据质量", {"complete": "完整", "partial": "部分完整",
                    "insufficient": "不足", "unavailable": "不可用"
                   }.get(dq.get("grade"), dq.get("grade") or "--")),
        _kv("综合判定", "%s（%s / 置信度 %s）" % (
            _esc(d.get("classification", "Unknown")),
            _fmtval(d.get("host_bound_score")), _pct(d.get("confidence")))),
    ])
    return ('<header class="hero"><h1>CPU / 深度学习 Host Bound 性能诊断报告</h1>'
            '<div class="meta-grid">%s</div></header>' % meta)


def _render_toc():
    items = "".join('<a href="#sec-%02d">%02d %s</a>' % (n, n, _esc(t))
                    for n, t, _ in _SECTIONS)
    return '<nav class="toc">%s</nav>' % items


def _render_footer(data):
    versions = data.get("_versions") or {}
    items = "".join("<span>%s：v%s</span>" % (_esc(k), _esc(v))
                    for k, v in sorted(versions.items()))
    return ('<footer class="foot">本报告由 host-bound-analyzer 离线生成（零依赖、'
            '纯内联 SVG、无外部资源）。%s</footer>' % items)


_CSS = """
:root { --blue:#1976d2; --ink:#212121; --muted:#616161; --line:#e0e0e0;
        --bg:#f4f6f9; --card:#ffffff; --warn-bg:#fff8e1; --bad:#d32f2f; }
* { box-sizing: border-box; }
body { margin:0; background:var(--bg); color:var(--ink);
       font:14px/1.65 "Segoe UI","Microsoft YaHei","PingFang SC",sans-serif; }
.hero { background:#1e2530; color:#fff; padding:26px 36px 20px; }
.hero h1 { margin:0 0 14px; font-size:21px; letter-spacing:.5px; }
.meta-grid { display:flex; flex-wrap:wrap; gap:6px 28px; }
.kv { font-size:12.5px; color:#cfd8e3; }
.kv-k { color:#8fa1b8; margin-right:8px; }
.kv-v { color:#fff; font-weight:600; }
.kv-v.mono { font-family:Consolas,monospace; }
.toc { background:#fff; border-bottom:1px solid var(--line);
       padding:8px 36px; display:flex; flex-wrap:wrap; gap:4px 14px;
       position:sticky; top:0; z-index:9; }
.toc a { color:var(--blue); text-decoration:none; font-size:12px;
         white-space:nowrap; }
.toc a:hover { text-decoration:underline; }
main { max-width:1080px; margin:18px auto 40px; padding:0 16px;
       display:flex; flex-direction:column; gap:16px; }
.card { background:var(--card); border:1px solid var(--line); border-radius:8px;
        padding:18px 22px; box-shadow:0 1px 3px rgba(0,0,0,.05); }
.sec-head { display:flex; align-items:baseline; gap:10px;
            border-bottom:2px solid #eef1f5; padding-bottom:8px; margin-bottom:14px; }
.sec-no { font-size:12px; font-weight:700; color:#fff; background:var(--blue);
          border-radius:4px; padding:1px 7px; }
.sec-head h2 { margin:0; font-size:16.5px; }
.sec-note { font-size:12px; color:var(--muted); }
.sec-body h3 { font-size:14px; margin:16px 0 8px; }
.sec-body h4 { font-size:13.5px; margin:14px 0 6px; }
table.tbl { width:100%; border-collapse:collapse; font-size:12.8px; }
.tbl th { background:#f6f8fa; text-align:left; padding:7px 9px;
          border:1px solid var(--line); white-space:nowrap; }
.tbl td { padding:6px 9px; border:1px solid var(--line);
          vertical-align:top; }
.tbl tr:nth-child(even) td { background:#fafbfc; }
code { font-family:Consolas,Menlo,monospace; font-size:12px;
       background:#f0f3f7; border-radius:3px; padding:1px 5px; color:#37474f; }
pre.code { background:#263238; color:#eceff1; padding:10px 12px; border-radius:6px;
           overflow:auto; max-height:320px; font-size:12px; line-height:1.5; }
.badge { display:inline-block; font-size:11px; font-weight:700; color:#fff;
         border-radius:3px; padding:1px 7px; white-space:nowrap; }
.sev-critical { background:#d32f2f; } .sev-high { background:#e65100; }
.sev-medium { background:#f9a825; color:#5d4037; }
.sev-low { background:#1976d2; } .sev-info { background:#616161; }
.lv-ok { background:#2e7d32; } .lv-warn { background:#f9a825; color:#5d4037; }
.lv-bad { background:#d32f2f; } .lv-info { background:#1976d2; }
.lv-na { background:#9e9e9e; }
.gauge-row { display:flex; flex-wrap:wrap; gap:6px; justify-content:space-between;
             margin-bottom:14px; }
.gauge-cell { flex:1 1 130px; min-width:130px; display:flex;
              justify-content:center; }
.two-col { display:flex; gap:18px; align-items:flex-start; flex-wrap:wrap; }
.radar-box { flex:0 0 460px; max-width:100%; }
.concl { flex:1 1 320px; background:#f6f9ff; border:1px solid #dbe7fb;
         border-radius:8px; padding:12px 16px; }
.concl-line { font-size:15px; margin-bottom:6px; }
.concl-class { margin-bottom:8px; color:var(--muted); }
.concl-stmt { margin:6px 0; }
.concl-tip { font-size:11.5px; color:#90a4ae; margin-top:10px;
             border-top:1px dashed #cfd8e3; padding-top:8px; }
.score-line { display:flex; align-items:baseline; gap:12px; margin-bottom:6px; }
.score-big { font-size:44px; font-weight:800; }
.score-desc { color:var(--muted); }
.heat-chip { display:inline-block; min-width:44px; text-align:center;
             color:#fff; border-radius:3px; padding:1px 6px;
             font-weight:700; font-size:12px; }
.verdict { background:#eef6ee; border-left:4px solid #2e7d32;
           padding:8px 12px; border-radius:0 6px 6px 0; }
.warn-box { background:var(--warn-bg); border:1px solid #ffe082;
            border-radius:6px; padding:10px 14px; font-size:12.8px; margin-top:10px; }
.warn-box ul, .impact-list, .warn-details ul { margin:6px 0 0; padding-left:20px; }
.warn-details { margin-top:10px; font-size:12.8px; color:var(--muted); }
.warn-details summary { cursor:pointer; color:#b26a00; }
.empty-box { border:1.5px dashed #b0bec5; background:#fafafa; color:#78909c;
             border-radius:6px; padding:14px 16px; font-size:12.8px; }
.chart-empty { border:1.5px dashed #b0bec5; background:#fafafa; color:#78909c;
               border-radius:6px; padding:22px 16px; text-align:center;
               font-size:12.8px; }
.note { font-size:12px; color:var(--muted); margin-top:10px; }
.sub { color:var(--muted); font-size:11.5px; }
.impact { max-width:260px; }
.files { font-size:12px; color:var(--muted); margin-top:8px; }
.files code { margin-right:8px; }
.chips code { margin:0 6px 6px 0; display:inline-block; }
details { margin:4px 0; }
details > summary { cursor:pointer; font-size:12.5px; color:var(--blue);
                    padding:3px 0; }
.ev-group details { border-left:2px solid #e3e9f0; padding-left:10px;
                    margin:6px 0; }
.ev-meta { font-size:12px; color:var(--muted); margin:4px 0 8px; }
.rc-line { font-size:12.8px; padding:2px 0; }
.diag-head p { margin:6px 0 0; }
.dq-line { margin-bottom:10px; }
.foot { text-align:center; color:#90a4ae; font-size:11.5px; padding:14px 0 26px; }
.foot span { margin:0 10px; }
@media print { .toc { position:static; } .card { box-shadow:none;
               break-inside:avoid; } pre.code { max-height:none; } }
"""


def render(data):
    """bundle.to_dict() 结构 -> 完整 HTML 文本（固定 18 章节）。"""
    data = data or {}
    body = [_render_header(data), _render_toc(), "<main>"]
    for num, title, dim in _SECTIONS:
        if dim is None:
            fn = {
                1: _render_summary, 2: _render_diagnosis,
                3: _render_assessment, 10: _render_timeline,
                11: _render_framework, 13: _render_root_cause,
                14: _render_evidence, 15: _render_suggestions,
                16: _render_risk, 17: _render_collection,
                18: _render_appendix,
            }[num]
            inner = fn(data)
        else:
            inner = _render_dim_section(data, dim[0], dim[1])
        body.append(_section(num, title, inner))
    body.append("</main>")
    body.append(_render_footer(data))
    return ('<!DOCTYPE html>\n<html lang="zh-CN">\n<head>\n'
            '<meta charset="utf-8"/>\n'
            '<meta name="viewport" content="width=device-width, initial-scale=1"/>\n'
            '<title>CPU / 深度学习 Host Bound 性能诊断报告</title>\n'
            '<style>%s</style>\n</head>\n<body>\n%s\n</body>\n</html>\n'
            % (_CSS, "\n".join(body)))
