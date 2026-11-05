# -*- coding: utf-8 -*-
"""report.py — Markdown 报告 + inline SVG 图表工具 (仅标准库, 全中文报告)
HTML 渲染见 report_html.py
"""
import os
import io
import json
from .metrics import METRIC_EXPLANATIONS, EVENT_EXPLANATIONS, IRQ_NAME_EXPLANATIONS, _explain_irq_name, freq_to_mhz, _pid_disp
from .diagnosis import THRESHOLDS
from .report_html import render_html, _cs_limit
from .npu_report import affinity_md, binding_md
from .report_explanation import explanations_md, fmt, collection_actions_md
from .report_sections import frequency_section, thread_section, core_table, coverage_section, topology_section, resource_section
from .report_tables import limit_markdown_tables

SEV_COLOR = {"CRITICAL": "#d64545", "WARNING": "#d98f00", "INFO": "#6b7280", "OK": "#2e9e5b"}
SEV_CN = {"CRITICAL": "严重", "WARNING": "警告", "INFO": "提示", "OK": "健康"}
STATUS_CN = {"CRITICAL": "CRITICAL（严重异常）", "WARNING": "WARNING（存在风险）",
             "DEGRADED": "DEGRADED（部分受限）", "HEALTHY": "HEALTHY（健康）",
             "UNKNOWN": "UNKNOWN（无法判定）"}

# ================================================================
# SVG 图表工具
# ================================================================
def _esc(s):
    return (str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#x27;"))

def svg_bar_chart(items, width=760, bar_h=16, gap=6, unit="%", color="#4a9eff", fmt=None):
    """横向条形图: items=[(label, value, color?)]"""
    if not items:
        return "<svg width='%d' height='40'><text x='10' y='25' fill='#888'>无数据</text></svg>" % width
    maxv = max(v for _l, v, *_ in items) or 1.0
    label_w = 110
    val_w = 90
    plot_w = width - label_w - val_w - 10
    h = len(items) * (bar_h + gap) + 10
    parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='%d' height='%d' font-family='sans-serif'>" % (width, h)]
    y = 5
    for it in items:
        label, v = it[0], it[1]
        c = it[2] if len(it) > 2 and it[2] else color
        w = max(plot_w * float(v) / maxv, 1.5) if maxv else 1
        parts.append("<text x='0' y='%d' font-size='11' fill='#c9d1d9' text-anchor='start'>%s</text>"
                     % (y + bar_h - 3, _esc(label)[:14]))
        parts.append("<rect x='%d' y='%d' width='%d' height='%d' rx='3' fill='%s'/>"
                     % (label_w, y, plot_w, bar_h, "#22303c"))
        parts.append("<rect x='%d' y='%d' width='%.1f' height='%d' rx='3' fill='%s'/>"
                     % (label_w, y, w, bar_h, c))
        txt = fmt(v) if fmt else ("%.1f%s" % (v, unit))
        parts.append("<text x='%d' y='%d' font-size='11' fill='#c9d1d9'>%s</text>"
                     % (width - 2, y + bar_h - 3, _esc(txt)))
        y += bar_h + gap
    parts.append("</svg>")
    return "".join(parts)

def svg_line_chart(series, width=760, height=200, y_max=None, y_label="%"):
    """折线图: series=[(name, color, [values]), ...] values 可含 None (断点)"""
    n = max(len(s[2]) for s in series) if series else 0
    if n == 0:
        return "<svg width='%d' height='40'><text x='10' y='25' fill='#888'>无数据</text></svg>" % width
    pad_l, pad_b, pad_t = 46, 22, 10
    plot_w, plot_h = width - pad_l - 10, height - pad_b - pad_t
    gmax = y_max
    if gmax is None:
        vals = [v for _n, _c, arr in series for v in arr if v is not None]
        gmax = max(vals) if vals else 1.0
        gmax = gmax * 1.15 or 1.0
    parts = ["<svg xmlns='http://www.w3.org/2000/svg' width='%d' height='%d' font-family='sans-serif'>" % (width, height)]
    # 网格与 Y 轴刻度
    for i in range(5):
        fy = pad_t + plot_h * i / 4.0
        yv = gmax * (1 - i / 4.0)
        parts.append("<line x1='%d' y1='%.1f' x2='%d' y2='%.1f' stroke='#22303c' stroke-width='1'/>"
                     % (pad_l, fy, pad_l + plot_w, fy))
        parts.append("<text x='%d' y='%.1f' font-size='10' fill='#8b949e' text-anchor='end'>%.1f</text>"
                     % (pad_l - 4, fy + 3, yv))
    x0 = pad_l
    for name, color, arr in series:
        pts = []
        for i, v in enumerate(arr):
            if v is None:
                continue
            px = x0 + plot_w * i / max(n - 1, 1)
            py = pad_t + plot_h * (1 - min(float(v) / gmax, 1.0))
            pts.append("%.1f,%.1f" % (px, py))
        if len(pts) >= 2:
            parts.append("<polyline points='%s' fill='none' stroke='%s' stroke-width='1.6'/>" % (" ".join(pts), color))
        elif pts:
            x, y = pts[0].split(",")
            parts.append("<circle cx='%s' cy='%s' r='2' fill='%s'/>" % (x, y, color))
    # X 轴时间刻度 (首/中/尾)
    for i, lab in ((0, "0"), (n // 2, "50%"), (n - 1, "100%")):
        px = x0 + plot_w * i / max(n - 1, 1)
        parts.append("<text x='%.1f' y='%d' font-size='10' fill='#8b949e' text-anchor='middle'>%s</text>"
                     % (px, height - 6, lab))
    parts.append("<text x='%d' y='%.1f' font-size='10' fill='#8b949e' text-anchor='end'>%s</text>"
                 % (pad_l - 4, pad_t - 2, y_label))
    parts.append("</svg>")
    return "".join(parts)

def _legend(items):
    return " ".join("<span style='color:%s'>■</span> %s" % (c, _esc(n)) for n, c in items)

# ================================================================
# Markdown 报告
# ================================================================
def _md_finding(f, idx):
    lines = ["### 问题 %d：%s（%s / 置信度 %s）" % (idx, f["title"], SEV_CN.get(f["severity"], f["severity"]), f["confidence"]),
             "", "- **类别**：`%s` — %s" % (f["type"], f.get("explanation", "")),
             "- **证据**："]
    for e in f["evidence"]:
        lines.append("  - %s = **%s**（判定阈值：%s；来源：%s；时间窗：%s）" % (
            e["metric"], e["value"], e["threshold"], e.get('source', 'ftrace / 对应系统快照'),
            e.get('time_window', '本次采集窗口；累计快照除外')))
    lines += ["- **影响**：%s" % f["impact"],
              "- **可能根因**：%s" % f["possible_root_cause"],
              "- **优化动作**：见第三部分。"]
    return "\n".join(lines)

def _md_metric_row(name, value, threshold, status):
    return "| %s | %s | %s | %s |" % (name, value, threshold, status)

def render_md(m, diag):
    st = diag["stats"]
    L = []
    A = L.append
    A("# HostBound 性能诊断报告")
    A("")
    A("> 数据格式：%s ｜ 采集时长：%.1f 秒 ｜ 核心数：%d ｜ 分析时间：%s" % (
        m.get("format_name", "-"), st["duration"], st["n_cpus"], _now()))
    A("")
    if m.get("metadata",{}).get("data_origin")=="SYNTHETIC_TEST_DATA":
        A("> 合成测试数据 / SYNTHETIC TEST DATA，非真实服务器测量。")
    A("## 1. Executive Summary（结论摘要）")
    A("")
    A("### Host 状态：%s" % STATUS_CN.get(diag["host_status"], diag["host_status"]))
    A("")
    p = diag.get("primary")
    if p:
        A("### 核心问题")
        A("")
        A("**%s**" % p["title"])
        A("")
        A("### 影响")
        A("")
        A(p["impact"])
        A("")
        A("完整优化方案与可复制命令见第三部分。")
    else:
        A("未发现明显的 CPU Host 侧问题。若业务性能仍异常，建议从 NPU/IO/框架侧继续排查。")
    if m.get("warnings"):
        A("")
        A("**数据告警**：" + "；".join(m["warnings"]))
    A("")
    A("## 2. 问题证据总表与根因判断")
    A("")
    A("### 2.1 关键指标证据与解释")
    A(explanations_md(m,diag))
    sch=m.get("sched",{})
    irq=m.get("irq",{})
    soft=m.get("softirq",{})
    A("### 2.2 根因判断与证据链")
    A("")
    if p:
        A("**现象** → %s" % p["title"])
        A("")
        A("**证据** → " + "；".join("%s=%s（阈值 %s）" % (e["metric"], e["value"], e["threshold"]) for e in p["evidence"]))
        A("")
        A("**推断** → %s" % p["possible_root_cause"])
        A("")
        A("**候选判断** → `%s`（%s）" % (p["type"], p.get("explanation", "")))
    else:
        A("未发现需要根因判断的问题。")
    A("")
    A(affinity_md(m.get("npu_affinity", {})))
    A(resource_section(m, summary=True))
    A("")
    A("## 3. 优化建议")
    A(binding_md(m.get("npu_affinity", {})))
    A("")
    A("## 4. 问题详情")
    A("")
    if diag["findings"]:
        for i, f in enumerate(diag["findings"][:100], 1):
            A(_md_finding(f, i))
            A("")
    else:
        A("未发现问题。")
        A("")
    A("## 5. 机器资源与 CPU 总体状态")
    A(resource_section(m))
    A("")
    ps = m.get("proc_stat") or {}
    if ps:
        A("/proc/stat 快照（开机以来累计）：user %.1f%%、system %.1f%%、iowait %.1f%%、irq %.1f%%、softirq %.1f%%、idle %.1f%%、steal %.1f%%。"
          % (ps.get("user", 0), ps.get("system", 0), ps.get("iowait", 0),
             ps.get("irq", 0), ps.get("softirq", 0), ps.get("idle", 0), ps.get("steal", 0)))
        A("")
        A("> 注意：trace 只能区分 busy/idle；user/system/iowait 为开机累计值，仅供参考。")
    else:
        A("无 /proc/stat 快照，总体占用以 trace 计算为准。")
    A("")
    A("## 6. CPU Core Balance（各核心负载）")
    A("")
    A(core_table(m))
    A("完整折线/柱状图、单位分组与NUMA筛选见离线HTML第6节。")
    A("")
    A("## 7. Scheduler 调度分析")
    A("")
    A("- 上下文切换总计 **%s** 次（%s 次/秒）%s" % (
        fmt(sch.get("cs_total"),digits=0), fmt(sch.get("cs_rate"),digits=0),
        "— 上下文切换率过高会增加调度开销，任务碎片化明显。" if (sch.get("cs_rate") or 0) > _cs_limit(st["n_cpus"]) else ""))
    A("- 唤醒总计 **%s** 次（%s 次/秒）— 唤醒率反映任务碎片化程度。" % (
        fmt(sch.get("wakeup_total"),digits=0), fmt(sch.get("wakeup_rate"),digits=0)))
    if sch.get("lat_avg") is not None:
        A("- 调度延迟：平均 %.3f ms，P50 %.3f ms，P95 %.2f ms，P99 %.2f ms，最大 %.2f ms（共 %s 次可配对样本）。" % (
            sch["lat_avg"], sch["lat_p50"], sch["lat_p95"], sch["lat_p99"], sch["lat_max"], format(sch["lat_count"], ",")))
        A("  - 调度延迟含义：" + METRIC_EXPLANATIONS["sched_latency"])
    else:
        A("- 调度延迟：" + (sch.get("lat_note") or "无数据。"))
    if sch.get("runnable_avg") is not None:
        A("- 平均可运行任务数 %.1f — %s" % (sch["runnable_avg"], METRIC_EXPLANATIONS["runnable_avg"]))
    if sch.get("top_lat_tasks"):
        A("")
        A("| 任务 | 最大延迟 ms | 平均 ms | 次数 |")
        A("|------|----------:|--------:|-----:|")
        for pid, comm, mx, avg, n in sch["top_lat_tasks"]:
            A("| %s | %.2f | %.3f | %d |" % (_pid_disp(comm, pid), mx * 1000, avg * 1000, n))
    A("")
    A("## 8. IRQ / SoftIRQ 分析")
    A("")
    A("### 8.1 硬中断")
    A("")
    A("硬中断全机观测占比 **%s**；单核最高 %s；配对次数 %s。" % (fmt(irq.get("total_ratio"),"%"),fmt(irq.get("max_cpu_ratio"),"%"),fmt(irq.get("total_count"),digits=0)))
    top_irq = irq.get("top_names") or []
    if top_irq:
        A("")
        A("| 中断源 | 次数 | 累计耗时 ms | 说明 |")
        A("|--------|-----:|-----------:|------|")
        for n, cnt, t in top_irq:
            A("| %s | %s | %.1f | %s |" % (n, format(cnt, ","), t * 1000, _explain_irq_name(n)))
    A("")
    A("### 8.2 软中断")
    A("")
    A("软中断全机观测占比 **%s**；单核最高 %s；配对次数 %s。" % (fmt(soft.get("total_ratio"),"%"),fmt(soft.get("max_cpu_ratio"),"%"),fmt(soft.get("total_count"),digits=0)))
    top_soft = soft.get("top_actions") or []
    if top_soft:
        A("")
        A("| 软中断类型 | 次数 | 累计耗时 ms | 说明 |")
        A("|-----------|-----:|-----------:|------|")
        for n, cnt, t in top_soft:
            A("| %s | %s | %.1f | %s |" % (n, format(cnt, ","), t * 1000,
                                           EVENT_EXPLANATIONS.get(n, "未收录软中断类型，参考内核文档。")))
    A("")
    A("各核中断分布见第 6 节表格与 HTML 图表。")
    A("")
    A("## 9. CPU Frequency / Idle 分析")
    A("")
    A(frequency_section(m))
    idle = m.get("idle", {})
    if idle.get("available"):
        avgs = {c:v for c,v in idle["per_cpu_ratio"].items() if v is not None}
        if avgs:
            A("各核空闲占比：平均 %.1f%%，最高 %.1f%%（%s），最低 %.1f%%（%s）。%s" % (
                sum(avgs.values()) / len(avgs),
                max(avgs.values()), max(avgs, key=avgs.get),
                min(avgs.values()), min(avgs, key=avgs.get),
                "多数核心大量空闲，负载分布问题见问题详情。" if (sum(avgs.values()) / len(avgs)) >= 70 else ""))
    else:
        A("trace 中无 cpu_idle 事件，空闲比基于 swapper 任务近似。")
    A("")
    A("## 10. Task / Thread 分析")
    A("")
    A(thread_section(m))
    tk = m.get("tasks", {})
    def _tbl(rows, head, fmt_fn):
        if not rows:
            return
        A(head)
        A(fmt_fn)
        for r in rows:
            A(r)
        A("")
    if tk.get("top_runtime"):
        A("| Top 运行时间任务 | 累计运行 ms | 说明 |")
        A("|-----------------|-----------:|------|")
        for pid, comm, v in tk["top_runtime"]:
            A("| %s | %.0f | %s |" % (_pid_disp(comm, pid), v * 1000,
                                               _task_hint(comm)))
        A("")
    if tk.get("top_cs"):
        A("| Top 切出次数任务 | 次数 |")
        A("|-----------------|-----:|")
        for pid, comm, v in tk["top_cs"]:
            A("| %s | %s |" % (_pid_disp(comm, pid), format(v, ",")))
        A("")
    if tk.get("top_wakeup"):
        A("| Top 被唤醒任务 | 次数 |")
        A("|---------------|-----:|")
        for comm, n in tk["top_wakeup"]:
            A("| %s | %s |" % (comm or "?", format(n, ",")))
        A("")
    if tk.get("top_migration"):
        A("| Top 迁移任务 | 次数 |")
        A("|-------------|-----:|")
        for pid, comm, n in tk["top_migration"]:
            A("| %s | %d |" % (_pid_disp(comm, pid), n))
        A("")
    A("## 11. Affinity / NUMA 分析")
    A("")
    A(topology_section(m))
    numa_txt = (m.get("snapshot", {}) or {}).get("numa.txt", "")
    if numa_txt and "node" in numa_txt:
        nnodes = numa_txt.count("== /sys/devices/system/node/node") if "== " in numa_txt else None
        A("快照包含 NUMA 拓扑信息（%s 个节点可见）。" % (nnodes if nnodes else "多"))
        A("")
    if tk.get("top_runtime"):
        A("Top 任务运行分布与各核利用率见第 6 节；任务集中在少数核时可结合绑核配置判断。")
    A("CPU 亲和性、NPU/PID 对应及线程运行核见第二部分；互斥 CPU 分区和宿主机命令见第三部分。")
    A("内存页 NUMA 分布与跨节点内存访问须 `numastat -p PID`、`/proc/PID/numa_maps` 或性能数据验证；taskset 本身不迁移内存页。")
    A("")
    A("## 12. 时间窗口分析")
    A("")
    A(_time_window_md(m))
    A("")
    A("## 13. 数据说明与指标解释")
    A(coverage_section(m))
    A("")
    A("### 数据概况")
    A("")
    A("- trace 时间范围：%.3f ~ %.3f（相对秒），时长 %.1f 秒" % (m.get("t_start", 0), m.get("t_end", 0), st["duration"]))
    A("- CPU 数量（参与统计）：%d；报告核数（nproc）：%s" % (st["n_cpus"], m.get("n_cpus_report") or "-"))
    A("- 内核版本：%s ｜ 主机名：%s" % (m["metadata"].get("kernel", "未知"), m["metadata"].get("hostname", "未知")))
    A("- 可用事件：%s" % ", ".join(sorted(m.get("available", [])) or ["无"]))
    A("- 解析统计：总行 %s，成功 %s，失败 %s（失败行已自动跳过）" % (
        format(m["parse_stats"]["total"], ","), format(m["parse_stats"]["parsed"], ","), format(m["parse_stats"]["failed"], ",")))
    A("- buffer 溢出：%s" % ((m.get("buffer_overrun") or "0（统计已提供）") if m.get("buffer_overrun_known") else "未知（未提供丢失统计，不能当作0）"))
    hostbound_snapshots = [k for k in (m.get("snapshot", {}) or {}) if k.startswith("hostbound/")]
    if hostbound_snapshots:
        A("- HostBound 扩展快照：%s（已纳入证据覆盖说明；名称匹配本身不构成根因）。" % \
          "、".join(sorted(k.rsplit("/", 1)[-1] for k in hostbound_snapshots)))
    else:
        A("- HostBound 扩展快照：未提供；CPU affinity/NPU 拓扑/目标线程对应关系可能证据不足。")
    A("")
    A("### 分析限制")
    A("")
    A("- trace 仅覆盖采集窗口，不代表长期行为。")
    A("- 利用率基于连续switch有效区间，无switch可用cpu_idle降级；user/system快照为开机累计。")
    A("- 调度延迟为 wakeup→switch 配对估算；中断耗时为 entry→exit 配对。")
    A("- 频率按显式单位转MHz；驱动报告值不等同于PMU实测频率。")
    A("")
    A("### 指标解释（每个指标是什么）")
    A("")
    A("| 指标 | 解释 |")
    A("|------|------|")
    for k, v in METRIC_EXPLANATIONS.items():
        A("| %s | %s |" % (k, v))
    A("")
    A("### 事件/术语解释")
    A("")
    A("| 术语 | 解释 |")
    A("|------|------|")
    for k, v in EVENT_EXPLANATIONS.items():
        A("| %s | %s |" % (k, v))
    return limit_markdown_tables("\n".join(L))

def _task_hint(comm):
    from .metrics import _explain_task
    h = _explain_task(comm or "")
    return h or "普通任务"

def _time_window_md(m):
    ts = m.get("timeseries") or {}
    util = ts.get("util") or []
    if not util:
        return "时间序列数据不足，无法进行时间窗口分析。"
    b = ts.get("bucket_sec", 1)
    vals = [(i, v) for i, v in enumerate(util) if v is not None]
    if not vals:
        return "时间序列数据不足，无法进行时间窗口分析。"
    top3 = sorted(vals, key=lambda x: -x[1])[:3]
    top3 = sorted(top3)  # 按时间排列
    lines = ["以下为利用率最高的时间窗口（桶 = %.2f 秒）：" % b, ""]
    cs = ts.get("cs_rate") or []
    lat = ts.get("lat_p99") or []
    soft = ts.get("softirq_ratio") or []
    for i, v in top3:
        seg = "窗口 %.1f~%.1f 秒：利用率峰值 **%.1f%%**" % (i * b, (i + 1) * b, v)
        extra = []
        if cs and cs[i] is not None:
            extra.append("上下文切换 %.0f 次/秒" % cs[i])
        if soft and soft[i] is not None:
            extra.append("软中断占比 %.1f%%" % soft[i])
        if lat and lat[i] is not None:
            extra.append("调度延迟 P99 %.2f ms" % lat[i])
        if extra:
            seg += "，同期 " + "、".join(extra)
        lines.append("- " + seg)
    lines.append("")
    lines.append("若峰值窗口内同步出现中断/调度延迟上升，可建立「负载升高 → 中断增加 → 调度延迟上升 → "
                 "worker 运行时间下降 → NPU 等待 Host」的关联链；仅当上述同期证据存在时成立。")
    return "\n".join(lines)

def _now():
    import time
    return time.strftime("%Y-%m-%d %H:%M:%S")

# ================================================================
# 入口
# ================================================================
def generate_reports(m, diag, out_dir):
    if not os.path.isdir(out_dir):
        os.makedirs(out_dir, exist_ok=True)
    md = render_md(m, diag)
    html = render_html(m, diag, svg_bar_chart, svg_line_chart, _esc, SEV_COLOR, SEV_CN, STATUS_CN)
    md_path = os.path.join(out_dir, "report.md")
    # 固定交付物名称。report.md 保留为机器可读/审阅辅助文件；最终面向客户的
    # 交付入口始终是同名 HTML，便于自动化系统稳定引用。
    html_path = os.path.join(out_dir, "hostbound_report.html")
    with io.open(md_path, "w", encoding="utf-8") as f:
        f.write(md)
    with io.open(html_path, "w", encoding="utf-8") as f:
        f.write(html)
    with io.open(os.path.join(out_dir,'report_data.json'),'w',encoding='utf-8') as f:
        json.dump({'schema_version':2,'units':{'time':'s','latency':'ms','frequency':'MHz','ratio':'percent','rate':'events/s'},'metrics':m,'diagnosis':diag},f,ensure_ascii=False,indent=2,allow_nan=False,default=lambda x:sorted(x) if isinstance(x,set) else str(x))
    return md_path, html_path
