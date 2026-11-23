"""Paired host/device columns in the existing HTML and translated workbook."""
from html import escape


def labels(result, comparison=None):
    left, right, peer = (("比对 NPU", "基线 NPU", "base") if result.get("mode") == "npu_npu"
                         else ("NPU", "GPU", "gpu"))
    if comparison and result.get("side_roles"):
        left, right = [comparison[role]["short_label"] for role in result["side_roles"]]
    return left, right, peer


def fmt(value, decimals=2, signed=False):
    if value is None:
        return "—"
    return format(value, ("+" if signed else "") + f",.{decimals}f")


def small(text):
    return "<small>" + escape(text) + "</small>"


def pair_headers(left, right, metrics):
    groups = "".join(f'<th scope="colgroup" colspan="2">{m}</th>' for m in metrics)
    sides = "".join(f'<th class="ev-left" scope="col">{left}</th><th class="ev-right" scope="col">{right}</th>' for _ in metrics)
    return (f'<thead><tr><th scope="col" rowspan="2">#</th>{groups}<th colspan="2" scope="colgroup">累计耗时差异</th></tr>'
            f'<tr>{sides}<th scope="col">{left} − {right} (ms)</th>'
            f'<th scope="col">{left} / {right}</th></tr></thead>')


def times(row, peer, is_host=False):
    a = row["npu"] if is_host else (row["matched_npu"] or row["npu"])
    b, d = row.get(peer) or {}, row.get("diff") or {}
    items = [a["total_ms"], b.get("total_ms")]
    if is_host:
        items += [row["npu_self_ms"], row.get(peer+"_self_ms")]
    return items + [a["calls"], b.get("calls"), a["avg_us"], b.get("avg_us"), d.get("delta_ms"), d.get("ratio")]


def metric_cells(row, peer, is_host=False):
    items = times(row, peer, is_host)
    partial = not is_host and row["matched_npu"] and row["matched_npu"]["calls"] < row["npu"]["calls"]
    cells = []
    for idx, value in enumerate(items):
        is_count = len(items)-6 <= idx < len(items)-4
        is_delta = idx == len(items)-2
        css = "col-num"
        if is_delta and value:
            css += " bad" if value>0 else " good"
        content = fmt(value, 0 if is_count else 2, signed=is_delta)
        if partial and idx in (0, len(items)-6):
            raw = row["npu"]["calls" if is_count else "total_ms"]
            content += small("全量 "+fmt(raw, 0 if is_count else 2))
        cells.append(f'<td class="{css}">{content}</td>')
    return "".join(cells)


def gpu_short(symbol):
    if symbol.startswith("nvjet_"):
        return "_".join(symbol.split("_")[:2]) + "_*"
    for tag in ("quackgemm_default_epiGemmDefaultSm90", "FlashAttnBwdSm90", "FlashAttnBwdPreprocess",
                "FlashAttnBwdPostprocessConvertdQ"):
        if tag in symbol:
            return tag
    name = symbol.split("<", 1)[0].split("(", 1)[0]
    return name[5:] if name.startswith("void ") else name


def render_device(result, comparison=None):
    left, right, peer = labels(result, comparison)
    rows = []
    for i,r in enumerate(result["device"],1):
        name=escape(r["op"])
        if result.get("execution"):
            name=f'<a class="ev-left" href="#kernel-execution" data-execution-type="{escape(r["op"],quote=True)}">{name}</a>'
        if r["status"] == "见框架子组":
            name += '<small><a href="#kernel-attribution">见框架子组</a></small>'
        symbols=list(r[peer+"_symbols"])
        if peer=="gpu":
            peers=" / ".join(dict.fromkeys(gpu_short(s) for s in symbols)) if symbols else "—"
        else:
            peers=r.get("base_op") or (" / ".join(symbols) if symbols else "—")
        other=escape(peers)
        if peer=="base" and symbols:
            other+=small(r["core"])
        elif len(symbols)>1:
            other+=small(f"{len(symbols)} 个实现")
        title=escape("\n".join(symbols),quote=True)
        rows.append(f'<tr><th scope="row">{i}</th><td class="ev-left">{name}{small(r["core"])}</td>'
                    f'<td class="ev-right" title="{title}">{other}</td>'+metric_cells(r,peer)+'</tr>')
    note=f"按{left}全量累计耗时排名。部分覆盖行用可比子集计算差值，并列出全量值。— 表示缺少可比数据。"
    scope=f"{left} step {', '.join(result['npu_steps'])} / {right} step {', '.join(result[peer+'_steps']) or '未标注'}。"
    if result.get("msprof_step_filter"):
        scope+=" 本节、Host 和单次执行均使用完整导出窗口，综合工作表按所选 step 筛选。"
    phases=[]
    if peer=="gpu":
        for phase in ("MoeEpDispatch","MoeEpCombine"):
            pair=[r for r in result["device"] if r["op"] in (phase,phase+"Epilogue") and r["diff"]]
            if len(pair)==2 and len({r["op"] for r in pair})==2:
                n=sum(r["matched_npu"]["total_ms"] for r in pair)
                g=sum(r[peer]["total_ms"] for r in pair)
                phases.append(f"<tr><td>{phase}</td><td>{fmt(n)}</td><td>{fmt(g)}</td><td>{fmt(n-g,signed=True)}</td></tr>")
    left, right = escape(left), escape(right)
    phase_table=(f"<h4>MoE 主核与 epilogue 合计</h4><table><tr><th>阶段</th><th>{left} ms</th>"
                 f"<th>{right} ms</th><th>{left} − {right} (ms)</th></tr>"
                 +"".join(phases)+"</table>") if phases else ""
    hdr=pair_headers(left,right,["Kernel 名称","累计耗时 ms","调用次数","单次均值 μs"])
    return (f'<section class="raw-detail" id="device-top"><h3>Device kernel · {left} 耗时 Top {len(rows)}</h3>'
            f'<p>{escape(scope)}{escape(note)}</p><div class="table-wrap"><table class="paired-table">{hdr}<tbody>'
            +''.join(rows)+'</tbody></table></div>'+phase_table+'</section>')


def render_host(result, comparison=None):
    left, right, peer = labels(result, comparison)
    left, right = escape(left), escape(right)
    rows=[]
    for i,r in enumerate(result["host"],1):
        hidden=' hidden' if i>15 else ''
        rows.append(f'<tr{hidden}><th scope="row">{i}</th><td class="ev-left">{escape(r["npu_name"])}</td>'
                    f'<td class="ev-right">{escape(r[peer+"_name"] or "—")}</td>'+metric_cells(r,peer,True)+'</tr>')
    hdr=pair_headers(left,right,["框架算子名称","累计耗时 ms","Self 耗时 ms","调用次数","单次均值 μs"])
    button=""
    if len(rows)>15:
        button=f'<button type="button" data-total="{len(rows)}" aria-controls="host-rows" aria-expanded="false">显示全部 {len(rows)} 行</button>'
    return (f'<section class="raw-detail" id="host-ops"><h3>Host 框架算子</h3><p>按{left} self 排序，差值使用包含子调用的 host total。</p>'
            +button+f'<div class="table-wrap"><table class="paired-table">{hdr}<tbody id="host-rows">'+''.join(rows)+
            """</tbody></table></div><script>(()=>{const s=document.currentScript.closest('section'),b=s.querySelector('button');
            if(b)b.onclick=()=>{const all=b.getAttribute('aria-expanded')==='false';b.setAttribute('aria-expanded',String(all));
            Array.from(s.querySelectorAll('tbody tr')).slice(15).forEach(r=>r.hidden=!all);
            b.textContent=all?'仅显示前 15 行':'显示全部 '+b.dataset.total+' 行';};})();</script></section>""")


def spreadsheet_tables(result, comparison=None):
    left, right, peer = labels(result, comparison)
    def cols(metrics):
        return [side+" "+metric for metric in metrics for side in (left,right)]+[
            f"{left} − {right} (ms)", f"{left} / {right}"]
    device=[["排名",left+" 类型",right+" 类型或实现","加速器核",*cols(["累计 ms","次数","单次 μs"]),
             left+" 全量 ms",left+" 全量次数","覆盖情况"]]
    for i,r in enumerate(result["device"],1):
        partial=r["matched_npu"] and r["matched_npu"]["calls"]<r["npu"]["calls"]
        device.append([i,r["op"],r.get("base_op") or "\n".join(r[peer+"_symbols"]),r["core"],*times(r,peer),
                       r["npu"]["total_ms"],r["npu"]["calls"],"部分覆盖" if partial else "整组" if r["diff"] else r["status"]])
    host=[[left+" 算子",right+" 算子",*cols(["累计 ms","Self ms","次数","单次 μs"])]]
    for r in result["host"]:
        host.append([r["npu_name"],r[peer+"_name"],*times(r,peer,True)])
    return [("Device热点Top15",device),("Host算子实测",host), *attribution_tables(result, comparison)]

COVERAGE_LABELS = {
    "npu": {"all": "设备任务", "native_framework": "原生框架连接", "cann_context": "CANN launch 回溯",
            "no_unique_framework": "未定位唯一框架调用"},
    "gpu": {"all": "Device kernel", "launch": "唯一 launch", "flow": "ac2g flow",
            "launch_and_flow": "launch 与 flow 一致", "framework": "直接 CPU 算子",
            "aten": "其中有 aten 上下文", "graph_replay": "CUDA Graph 重放",
            "no_cpu_context": "有独立 launch，缺少唯一 CPU 算子",
            "gemm_annotation_only": "其中有独立 GEMM 标注",
            "no_launch": "缺少 launch", "ambiguous_launch": "launch 存在歧义"}
}


def attribution_tables(result, comparison=None):
    audit = result.get("attribution")
    if not audit:
        return []
    left, right, _ = labels(result, comparison)
    coverage = [["设备", "归属证据", "事件数", "占本侧事件数 %", "累计 ms"]]
    for side, name in (("npu", left), ("gpu", right)):
        metrics = audit[side]
        total = metrics.get("all", {}).get("calls", 0)
        if not metrics:
            coverage.append([name, "缺少原生 DB 归属证据", None, None, None])
        for key, label in COVERAGE_LABELS[side].items():
            if key in metrics:
                value = metrics[key]
                coverage.append([name, label, value["calls"], 100 * value["calls"] / total if total else None,
                                 value["total_ms"]])
    coverage.extend([left, "Add 内 " + name, calls, None, None]
                    for name, calls in sorted(audit["npu_add_owners"].items()))
    groups = [["框架分组", "计算签名", left+" kernel 数", right+" kernel 数",
               left+" 逻辑调用数", right+" 逻辑调用数", left+" 累计 ms", right+" 累计 ms",
               left+" 每逻辑调用 ms", right+" 每逻辑调用 ms", left+" / "+right+" 均值",
               left+" GEMM 行数", right+" GEMM 行数", "证据与口径"]]
    for row in audit["groups"]:
        a, b = row["npu"], row["gpu"]
        groups.append([row["op"], row["signature"], a["kernels"], b["kernels"],
                       a["logical_calls"], b["logical_calls"], a["total_ms"], b["total_ms"],
                       a["mean_ms"], b["mean_ms"], row["mean_ratio"],
                       a.get("operand_rows"), b.get("operand_rows"), row["note"]])
    return [("Kernel归属覆盖", coverage), ("跨平台框架分组", groups)]


def render_attribution(result, comparison=None):
    tables = attribution_tables(result, comparison)
    if not tables:
        return ""

    def cell(value, index, integer_columns):
        if isinstance(value, (float, int)):
            return fmt(value, 0 if index in integer_columns else 3)
        return escape(value) if value is not None else "—"

    def table(rows, integer_columns):
        headers, *body = rows
        head = "".join(f'<th scope="col">{escape(label)}</th>' for label in headers)
        rendered = ["<tr>" + "".join(f"<td>{cell(v, i, integer_columns)}</td>"
                                      for i, v in enumerate(row)) + "</tr>" for row in body]
        return '<div class="table-wrap"><table><thead><tr>'+head+'</tr></thead><tbody>'+''.join(rendered)+'</tbody></table></div>'

    return ('<section class="raw-detail" id="kernel-attribution">'
            '<p>覆盖率以本侧设备事件为分母。Launch、flow 和框架归属可以重叠，CUDA Graph 重放入口只提供图级归属。</p>'
            + table(tables[0][1], {2}) +
            '<p>累计值为 kernel 耗时之和，可能跨流重叠。'
            '倍率按同形状的逻辑调用均值计算。空倍率表示计算边界或工作量仍需确认。</p>'
            + (table(tables[1][1], {2,3,4,5,11,12}) if result["attribution"]["groups"]
               else '<p>当前数据未找到可展示的框架分组。</p>') + '</section>')
