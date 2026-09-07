# -*- coding: utf-8 -*-
"""报告图表层：纯标准库生成内嵌 SVG（折线/柱状/雷达/仪表盘/热力条/根因树）。

设计约束（DESIGN.md §10）：
- 零 JS、零外部资源：<svg> 直接内嵌 HTML，双击离线可看；
- 任何输入缺失/非法 -> 返回占位块（"数据缺失"），绝不抛异常、绝不编造数据；
- 所有动态文本经 HTML 转义后输出。
"""

# 状态色（DESIGN.md §10 固定色板）
SEV_COLOR = {
    "CRITICAL": "#d32f2f",
    "HIGH": "#e65100",
    "MEDIUM": "#f9a825",
    "LOW": "#1976d2",
    "INFO": "#616161",
}

# 指标级别色（analyzer Section 的 ok/warn/bad/info/na）
LEVEL_COLOR = {
    "ok": "#2e7d32",
    "warn": "#f9a825",
    "bad": "#d32f2f",
    "info": "#1976d2",
    "na": "#9e9e9e",
}

# 折线默认调色板（多序列时依序取用）
_PALETTE = ["#1976d2", "#e65100", "#2e7d32", "#7b1fa2", "#00838f", "#c62828"]


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------

def _esc(s):
    """HTML 转义（含引号），None 安全。"""
    if s is None:
        return ""
    s = str(s)
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
             .replace('"', "&quot;").replace("'", "&#39;"))


def _fnum(v):
    """数值格式化：浮点去尾零；不可转换时原样转义返回。"""
    try:
        f = float(v)
    except (TypeError, ValueError):
        return _esc(v)
    if f == int(f) and abs(f) < 1e15:
        return str(int(f))
    return ("%.4f" % f).rstrip("0").rstrip(".")


def placeholder(msg="数据缺失，无法绘制该图"):
    """统一占位块：缺数据必须显式说明，绝不编造图形。"""
    return '<div class="chart-empty">%s</div>' % _esc(msg)


def _clamp(v, lo, hi):
    if v < lo:
        return lo
    if v > hi:
        return hi
    return v


def _heat_color(v):
    """0-100 -> 绿(#2e7d32) 黄(#f9a825) 红(#d32f2f) 渐变。"""
    v = _clamp(float(v), 0.0, 100.0)
    lo_rgb = (46, 125, 50)
    mid_rgb = (249, 168, 37)
    hi_rgb = (211, 47, 47)
    if v <= 50:
        t = v / 50.0
        a, b = lo_rgb, mid_rgb
    else:
        t = (v - 50.0) / 50.0
        a, b = mid_rgb, hi_rgb
    rgb = tuple(int(round(a[i] + (b[i] - a[i]) * t)) for i in range(3))
    return "#%02x%02x%02x" % rgb


# ---------------------------------------------------------------------------
# 折线图（多序列）
# ---------------------------------------------------------------------------

def line_chart(title, series, y_max=None, width=760, height=250):
    """多序列折线图。

    series: [{"name": str, "color": "#hex"(可选), "points": [(x, y), ...]}, ...]
    x/y 均为数值；任一序列无有效点则跳过该序列；全部为空返回占位块。
    """
    cleaned = []
    ci = 0
    for s in (series or []):
        pts = []
        for p in (s.get("points") or []):
            try:
                pts.append((float(p[0]), float(p[1])))
            except (TypeError, ValueError, IndexError):
                continue
        if not pts:
            continue
        color = s.get("color") or _PALETTE[ci % len(_PALETTE)]
        ci += 1
        cleaned.append({"name": s.get("name") or ("series%d" % ci), "color": color,
                        "points": pts})
    if not cleaned:
        return placeholder("%s：无时序数据" % title) if title else placeholder()

    xs, ys = [], []
    for s in cleaned:
        for x, y in s["points"]:
            xs.append(x)
            ys.append(y)
    xmin, xmax = min(xs), max(xs)
    if xmax <= xmin:
        xmax = xmin + 1.0
    if y_max is not None:
        ymax = float(y_max)
    else:
        ymax = max(1.0, max(ys) * 1.08)
    ymin = 0.0

    L, R, T, B = 56, width - 14, 26, height - 44
    plot_w, plot_h = R - L, B - T

    def sx(x):
        return L + (x - xmin) / (xmax - xmin) * plot_w

    def sy(y):
        return B - (y - ymin) / (ymax - ymin) * plot_h

    out = ['<svg class="chart" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, height, width, height)]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (width, height))
    if title:
        out.append('<text x="10" y="16" font-size="13" font-weight="bold" '
                   'fill="#424242">%s</text>' % _esc(title))

    # 横向网格 + y 刻度
    for i in range(5):
        gy = T + plot_h * i / 4.0
        val = ymax - (ymax - ymin) * i / 4.0
        out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#eceff1" '
                   'stroke-width="1"/>' % (L, gy, R, gy))
        out.append('<text x="%g" y="%g" font-size="10" fill="#9e9e9e" '
                   'text-anchor="end">%s</text>' % (L - 5, gy + 3, _fnum(round(val, 2))))

    # 纵向网格 + x 刻度（首/中/尾）
    for i in range(5):
        gx = L + plot_w * i / 4.0
        out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#f5f5f5" '
                   'stroke-width="1"/>' % (gx, T, gx, B))
    for i in range(3):
        xv = xmin + (xmax - xmin) * i / 2.0
        gx = L + plot_w * i / 2.0
        lab = ("%.0f" % xv) if abs(xv) >= 1000 else _fnum(round(xv, 2))
        out.append('<text x="%g" y="%g" font-size="10" fill="#9e9e9e" '
                   'text-anchor="middle">%s</text>' % (gx, B + 14, _esc(lab)))

    # 序列折线（点少时附圆点）
    for s in cleaned:
        pstr = " ".join("%g,%g" % (sx(x), sy(y)) for x, y in s["points"])
        out.append('<polyline points="%s" fill="none" stroke="%s" '
                   'stroke-width="1.8" stroke-linejoin="round"/>' % (pstr, s["color"]))
        if len(s["points"]) <= 40:
            for x, y in s["points"]:
                out.append('<circle cx="%g" cy="%g" r="2.6" fill="%s"/>'
                           % (sx(x), sy(y), s["color"]))

    # 图例
    lx = L
    ly = height - 12
    for s in cleaned:
        out.append('<rect x="%g" y="%g" width="12" height="4" rx="2" fill="%s"/>'
                   % (lx, ly - 4, s["color"]))
        out.append('<text x="%g" y="%g" font-size="11" fill="#616161">%s</text>'
                   % (lx + 16, ly, _esc(s["name"])))
        lx += 16 + 7 * len(s["name"]) + 22
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 横向柱状图（TOP-N 类）
# ---------------------------------------------------------------------------

def bar_chart(items, width=760, bar_h=26, gap=10, y_max=None, unit=""):
    """items: [{"label": str, "value": num|None, "color": "#hex"(可选), "unit": str(可选)}]"""
    rows = []
    maxv = 0.0
    for it in (items or []):
        try:
            v = float(it.get("value"))
        except (TypeError, ValueError):
            v = None
        if v is not None and abs(v) > maxv:
            maxv = abs(v)
        rows.append({"label": str(it.get("label") or ""), "value": v,
                     "color": it.get("color"),
                     "unit": it.get("unit") or unit})
    if not rows:
        return placeholder()
    if y_max is not None:
        maxv = max(maxv, float(y_max))
    maxv = max(maxv, 1.0)

    L, lab_w, val_w = 8, 200, 70
    bar_x = L + lab_w
    bar_w = width - L - lab_w - val_w - 10
    T = 8
    H = T + len(rows) * (bar_h + gap) + 4

    out = ['<svg class="chart" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, H, width, H)]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (width, H))
    for i, r in enumerate(rows):
        y = T + i * (bar_h + gap)
        label = r["label"]
        if len(label) > 22:
            label = label[:21] + "…"
        out.append('<text x="%g" y="%g" font-size="11" fill="#424242" '
                   'text-anchor="end">%s</text>' % (bar_x - 8, y + bar_h / 2 + 4, _esc(label)))
        if r["value"] is None:
            out.append('<text x="%g" y="%g" font-size="11" fill="#9e9e9e">无数据</text>'
                       % (bar_x + 6, y + bar_h / 2 + 4))
            continue
        frac = _clamp(abs(r["value"]) / maxv, 0.0, 1.0)
        w = max(2.0, bar_w * frac)
        color = r["color"] or "#1976d2"
        out.append('<rect x="%g" y="%g" width="%g" height="%g" rx="3" fill="%s"/>'
                   % (bar_x, y, w, bar_h, _esc(color)))
        out.append('<text x="%g" y="%g" font-size="11" fill="#212121">%s%s</text>'
                   % (bar_x + w + 6, y + bar_h / 2 + 4, _fnum(r["value"]),
                      _esc(r["unit"])))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 雷达图（9 轴风险）
# ---------------------------------------------------------------------------

def radar_chart(scores, axes=None, width=430, height=380):
    """scores: {axis: 0..100}；axes 为固定轴序（缺轴按 0 处理）。"""
    axes = list(axes or (scores or {}).keys())
    if len(axes) < 3:
        return placeholder("雷达图数据不足（至少 3 个维度）")
    cx, cy = width / 2.0, height / 2.0 + 12
    R = min(width, height) / 2.0 - 46
    n = len(axes)

    def pt(i, r):
        ang = -3.141592653589793 / 2.0 + i * 2 * 3.141592653589793 / n
        return (cx + r * _cos(ang), cy + r * _sin(ang))

    out = ['<svg class="chart" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, height, width, height)]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (width, height))
    # 网格环
    for lv in (20, 40, 60, 80, 100):
        pts = " ".join("%g,%g" % pt(i, R * lv / 100.0) for i in range(n))
        fill = "#fafafa" if lv == 100 else "none"
        out.append('<polygon points="%s" fill="%s" stroke="#e0e0e0" stroke-width="1"/>'
                   % (pts, fill))
    # 轴线与标签
    for i, name in enumerate(axes):
        x, y = pt(i, R)
        out.append('<line x1="%g" y1="%g" x2="%g" y2="%g" stroke="#e0e0e0" '
                   'stroke-width="1"/>' % (cx, cy, x, y))
        lx, ly = pt(i, R + 18)
        dx = lx - cx
        anchor = "middle"
        if dx > R * 0.25:
            anchor = "start"
        elif dx < -R * 0.25:
            anchor = "end"
        v = scores.get(name)
        try:
            vs = _fnum(round(float(v), 1))
        except (TypeError, ValueError):
            vs = "-"
        out.append('<text x="%g" y="%g" font-size="12" fill="#424242" '
                   'text-anchor="%s">%s</text>' % (lx, ly + 4, anchor, _esc(name)))
        out.append('<text x="%g" y="%g" font-size="10" fill="#9e9e9e" '
                   'text-anchor="%s">%s</text>' % (lx, ly + 16, anchor, vs))
    # 数据多边形
    dpts = []
    for i, name in enumerate(axes):
        try:
            v = _clamp(float(scores.get(name) or 0), 0.0, 100.0)
        except (TypeError, ValueError):
            v = 0.0
        dpts.append(pt(i, R * v / 100.0))
    pts = " ".join("%g,%g" % p for p in dpts)
    out.append('<polygon points="%s" fill="#1976d2" fill-opacity="0.18" '
               'stroke="#1976d2" stroke-width="1.8"/>' % pts)
    for x, y in dpts:
        out.append('<circle cx="%g" cy="%g" r="3" fill="#1976d2"/>' % (x, y))
    out.append("</svg>")
    return "".join(out)


def _cos(a):
    return _trig(a, True)


def _sin(a):
    return _trig(a, False)


def _trig(a, want_cos):
    """无 math 依赖的三角近似（雷达图精度足够）：6 项泰勒展开。"""
    x = a - int(a / (2 * 3.141592653589793)) * 2 * 3.141592653589793
    # 归约到 [-pi, pi]
    if x > 3.141592653589793:
        x -= 2 * 3.141592653589793
    if want_cos:
        return _poly(x, True)
    return _poly(x - 3.141592653589793 / 2.0, False) if False else _poly_sin(x)


def _poly_cos(x):
    x2 = x * x
    return 1 - x2 / 2 + x2 * x2 / 24 - x2 * x2 * x2 / 720


def _poly_sin(x):
    x2 = x * x
    return x - x * x2 / 6 + x * x2 * x2 / 120


def _poly(x, is_cos):
    return _poly_cos(x) if is_cos else _poly_sin(x)


# ---------------------------------------------------------------------------
# 半环仪表盘（健康度 9 项）
# ---------------------------------------------------------------------------

def gauge(label, value, level="na", unit="", width=158, height=106):
    """半环仪表：value 0-100；level 决定弧色；na 显示 '--'。"""
    cx = width / 2.0
    cy = height - 26
    r = 54.0
    arc_len = 3.141592653589793 * r
    x1, y1 = cx - r, cy
    x2, y2 = cx + r, cy
    out = ['<svg class="gauge" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, height, width, height)]
    out.append('<path d="M %g %g A %g %g 0 0 1 %g %g" fill="none" stroke="#e8e8e8" '
               'stroke-width="12" stroke-linecap="round"/>' % (x1, y1, r, r, x2, y2))
    num_txt = "--"
    if value is not None and level != "na":
        try:
            v = _clamp(float(value), 0.0, 100.0)
            dash = arc_len * v / 100.0
            out.append('<path d="M %g %g A %g %g 0 0 1 %g %g" fill="none" stroke="%s" '
                       'stroke-width="12" stroke-linecap="round" stroke-dasharray="%g %g"/>'
                       % (x1, y1, r, r, x2, y2, _esc(LEVEL_COLOR.get(level, "#9e9e9e")),
                          dash, arc_len))
            num_txt = _fnum(round(v, 1))
        except (TypeError, ValueError):
            num_txt = "--"
    out.append('<text x="%g" y="%g" font-size="21" font-weight="bold" fill="#212121" '
               'text-anchor="middle">%s</text>' % (cx, cy - 8, num_txt))
    if unit and num_txt != "--":
        out.append('<text x="%g" y="%g" font-size="9" fill="#9e9e9e" '
                   'text-anchor="middle">%s</text>' % (cx, cy + 4, _esc(unit)))
    out.append('<text x="%g" y="%g" font-size="10.5" fill="#616161" '
               'text-anchor="middle">%s</text>' % (cx, height - 6, _esc(label)))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# per-core 热力条
# ---------------------------------------------------------------------------

def heatmap_strip(values, labels=None, cell=24, gap=4, height=40):
    """核心利用率热力条：values 为 0-100 列表；labels 缺省为 core0..n。"""
    vals = []
    for v in (values or []):
        try:
            vals.append(_clamp(float(v), 0.0, 100.0))
        except (TypeError, ValueError):
            vals.append(None)
    if not vals:
        return placeholder()
    n = len(vals)
    width = n * (cell + gap) + 8
    out = ['<svg class="chart" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, height, width, height)]
    for i, v in enumerate(vals):
        x = 4 + i * (cell + gap)
        y = 6
        color = "#e0e0e0" if v is None else _heat_color(v)
        tip = (labels[i] if labels and i < len(labels) else "core%d" % i)
        tip_txt = "无数据" if v is None else "%.1f%%" % v
        out.append('<rect x="%g" y="%g" width="%g" height="%g" rx="4" fill="%s">'
                   '<title>%s: %s</title></rect>' % (x, y, cell, cell, color,
                                                     _esc(tip), _esc(tip_txt)))
    out.append("</svg>")
    return "".join(out)


# ---------------------------------------------------------------------------
# 根因树
# ---------------------------------------------------------------------------

def tree_svg(root, width=780, node_w=176, node_h=54, level_h=92):
    """根因树：root 为 RootCauseNode.to_dict() 结构（name/score/confidence/children/detail）。"""
    if not isinstance(root, dict) or not root.get("name"):
        return placeholder("根因树数据缺失（无发现或数据不足，不虚构根因）")

    # 布局：叶子均分横向槽位，父节点居中于子节点
    slots = {"n": 0}

    def layout(node, depth):
        kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
        if not kids:
            i = slots["n"]
            slots["n"] += 1
            x = (i + 0.5) * width / max(1, slots["n"] if False else 1)  # 先占位，后面重算
            return {"x": i, "depth": depth, "leaf": True}
        laid = [layout(c, depth + 1) for c in kids]
        x = sum(l["x"] for l in laid) / float(len(laid))
        md = max(l["depth"] for l in laid)
        return {"x": x, "depth": depth, "leaf": False, "kids": laid}

    top = layout(root, 0)
    n_leaf = max(1, slots["n"])
    slot_w = width / float(n_leaf)
    max_depth = _max_depth(root, 0)
    H = (max_depth + 1) * level_h + 12

    out = ['<svg class="chart" width="%d" height="%d" viewBox="0 0 %d %d" '
           'xmlns="http://www.w3.org/2000/svg" role="img">' % (width, H, width, H)]
    out.append('<rect x="0" y="0" width="%d" height="%d" fill="#ffffff"/>' % (width, H))

    def draw(node, info):
        x = (info["x"] + 0.5) * slot_w if info.get("leaf") else (info["x"] + 0.5) * slot_w
        y = info["depth"] * level_h + 8
        cx_n = x
        cy_n = y + node_h / 2.0
        kids = info.get("kids") or []
        for kinfo, kchild in zip(kids, [c for c in (node.get("children") or [])
                                        if isinstance(c, dict)]):
            kx = (kinfo["x"] + 0.5) * slot_w
            ky = kinfo["depth"] * level_h + 8
            out.append('<path d="M %g %g C %g %g %g %g %g %g" fill="none" '
                       'stroke="#b0bec5" stroke-width="1.4"/>'
                       % (cx_n, y + node_h, cx_n, y + node_h + 26,
                          kx, ky - 26, kx, ky))
            draw(kchild, kinfo)
        try:
            score = _clamp(float(node.get("score") or 0), 0.0, 100.0)
        except (TypeError, ValueError):
            score = 0.0
        color = "#d32f2f" if score >= 60 else ("#e65100" if score >= 30 else "#1976d2")
        rx = cx_n - node_w / 2.0
        rx = _clamp(rx, 2, max(2, width - node_w - 2))
        out.append('<rect x="%g" y="%g" width="%g" height="%g" rx="8" fill="#ffffff" '
                   'stroke="%s" stroke-width="1.6"/>' % (rx, y, node_w, node_h, color))
        name = str(node.get("name") or "")
        if len(name) > 16:
            name = name[:15] + "…"
        out.append('<text x="%g" y="%g" font-size="12" font-weight="bold" fill="#212121" '
                   'text-anchor="middle">%s</text>'
                   % (cx_n, y + 18, _esc(name)))
        conf = node.get("confidence")
        try:
            conf_txt = "置信度 %.2f" % float(conf)
        except (TypeError, ValueError):
            conf_txt = ""
        out.append('<text x="%g" y="%g" font-size="9.5" fill="#757575" '
                   'text-anchor="middle">%s</text>' % (cx_n, y + 30, _esc(conf_txt)))
        bw = (node_w - 20) * score / 100.0
        out.append('<rect x="%g" y="%g" width="%g" height="6" rx="3" fill="%s"/>'
                   % (rx + 10, y + node_h - 12, max(2.0, bw), color))
        out.append('<text x="%g" y="%g" font-size="9.5" fill="#9e9e9e" '
                   'text-anchor="middle">影响 %s</text>'
                   % (cx_n, y + node_h - 1, _fnum(round(score, 1))))
        for kinfo, kchild in zip(kids, [c for c in (node.get("children") or [])
                                        if isinstance(c, dict)]):
            pass
        return

    draw(root, top)
    out.append("</svg>")
    return "".join(out)


def _max_depth(node, depth):
    kids = [c for c in (node.get("children") or []) if isinstance(c, dict)]
    if not kids:
        return depth
    return max(_max_depth(c, depth + 1) for c in kids)
