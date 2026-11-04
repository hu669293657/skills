"""All-core offline SVG view; data comes exclusively from computed metrics."""
import json
from html import escape
from pathlib import Path
from .npu_affinity import _topology

METRICS = [
    {'key':'util','label':'CPU 利用率','group':'time','unit':'%','color':'#68b8ff'},
    {'key':'idle_ratio','label':'CPU 空闲率','group':'time','unit':'%','color':'#62d6a5'},
    {'key':'irq_ratio','label':'硬中断','group':'time','unit':'%','color':'#ff956e'},
    {'key':'softirq_ratio','label':'软中断','group':'time','unit':'%','color':'#d09cff'},
    {'key':'cs_rate','label':'上下文切换','group':'activity','unit':'次/秒','color':'#68b8ff'},
    {'key':'wakeup_rate','label':'成功唤醒','group':'activity','unit':'次/秒','color':'#62d6a5'},
    {'key':'runnable_switch_out_rate','label':'可运行态切出','group':'activity','unit':'次/秒','color':'#ff956e'},
    {'key':'migration_rate','label':'迁移','group':'activity','unit':'次/秒','color':'#d09cff'},
    {'key':'freq_current','label':'当前频率','group':'frequency','unit':'MHz','color':'#62d6a5'},
    {'key':'freq_average','label':'平均频率','group':'frequency','unit':'MHz','color':'#68b8ff'},
    {'key':'freq_maximum','label':'硬件最大频率','group':'frequency','unit':'MHz','color':'#d09cff'},
]


def safe_json(data):
    return json.dumps(data,ensure_ascii=False,allow_nan=False,separators=(',',':')).replace('&','\\u0026').replace('<','\\u003c').replace('>','\\u003e').replace('\u2028','\\u2028').replace('\u2029','\\u2029')


def core_data(m):
    _,nodes,_=_topology(m.get('snapshot',{}))
    c2n={c:n for n,cs in nodes.items() for c in cs}
    freq={c:p for p in m.get('frequency',{}).get('domains',[]) for c in p['cpus']}
    rows=[]
    for c,pc in sorted(m.get('per_cpu',{}).items()):
        row=dict(pc,cpu=c,numa=c2n.get(c));p=freq.get(c,{})
        row.update(freq_current=p.get('current_mhz'),freq_average=p.get('average_mhz'),freq_maximum=p.get('maximum_mhz'))
        rows.append({key:row.get(key) for key in ['cpu','numa','observed_sec']+[x['key'] for x in METRICS]})
    return {'cpus':rows,'nodes':sorted(nodes),'metrics':METRICS,'hot_threshold':90,
            'note':'全部在线/观测CPU；未采集或无法计算的指标为N/A。频率按共享policy映射，仅供每核对照，policy统计不重复计数。'}


def static_svg(data):
    rows=data['cpus'];width=1080;height=300;left=56;top=28;plotw=1000;ploth=220
    out=["<svg class='core-static' viewBox='0 0 1080 300' role='img' aria-label='全部CPU利用率静态后备图'>"]
    if not rows:
        return ''.join(out)+"<text x='60' y='100' fill='#8b949e'>CPU数据为空</text></svg>"
    for v in (0,25,50,75,100):
        y=top+ploth*(1-v/100)
        out.append("<line x1='56' x2='1056' y1='%g' y2='%g' stroke='#2d3742'/><text x='5' y='%g' fill='#8b949e' font-size='12'>%d%%</text>"%(y,y,y+4,v))
    parts=[];known=[]
    for i,r in enumerate(rows):
        v=r['util'];x=left+plotw*(i+.5)/len(rows)
        if v is None:
            if parts:out.append("<polyline points='%s' fill='none' stroke='#68b8ff' stroke-width='1.7'/>"%' '.join(parts));parts=[]
            continue
        y=top+ploth*(1-v/100);parts.append('%g,%g'%(x,y));known.append(v)
        out.append("<circle cx='%g' cy='%g' r='%g' fill='%s'><title>CPU%d / NUMA%s / %.2f%%</title></circle>"%(x,y,3 if v>=90 else 1.5,'#ff956e' if v>=90 else '#68b8ff',r['cpu'],escape(str(r['numa'])),v))
        if i%max(1,len(rows)//12)==0 or i==len(rows)-1:
            out.append("<text x='%g' y='272' fill='#8b949e' font-size='11' text-anchor='middle'>%d</text>"%(x,r['cpu']))
    if parts:out.append("<polyline points='%s' fill='none' stroke='#68b8ff' stroke-width='1.7'/>"%' '.join(parts))
    if known:
        avg=sum(known)/len(known);y=top+ploth*(1-avg/100)
        out.append("<line x1='56' x2='1056' y1='%g' y2='%g' stroke='#62d6a5' stroke-dasharray='5 5'/><text x='60' y='18' fill='#62d6a5' font-size='12'>平均 %.1f%% · 最低 %.1f%% · 最高 %.1f%% · 全部 %d CPU</text>"%(y,y,avg,min(known),max(known),len(rows)))
    else:out.append("<text x='60' y='100' fill='#8b949e'>所有CPU利用率均缺失</text>")
    return ''.join(out)+"<text x='540' y='295' fill='#8b949e' font-size='12'>CPU编号</text></svg>"


def render_core_balance(m):
    data=core_data(m)
    script=(Path(__file__).parent/'assets/core_balance.js').read_text(encoding='utf-8-sig')
    return '''<div id="core-balance" class="chart">
<style>
#core-balance svg{display:block;width:100%%;min-width:560px}#core-balance{position:relative}
.core-controls{display:none;gap:12px;align-items:center;flex-wrap:wrap;margin-bottom:12px}
.core-controls label{font-size:12px;color:var(--muted)}.core-controls select,.core-controls input,.metric-toggle{background:#182333;color:#dce3ea;border:1px solid #40516a;border-radius:5px;padding:6px}
.core-controls input{width:80px}.metric-toggle{cursor:pointer;margin:3px}.metric-toggle[aria-pressed="false"]{opacity:.45}
#core-tooltip{white-space:pre-wrap;font:12px/1.7 Consolas,monospace;min-height:42px;margin-top:8px}
.core-summary{font-size:12px;color:var(--muted)}.core-dynamic{overflow:auto}.core-table-scroll{overflow:auto}
@media print{.core-controls,#core-metrics,.core-dynamic,#core-tooltip{display:none!important}.core-static{display:block!important}}
</style>
<div class="core-controls">
<label>指标组 <select id="core-group"><option value="time">CPU 时间占比（%%）</option><option value="activity">调度活动（次/秒）</option><option value="frequency">CPU Frequency（MHz）</option></select></label>
<label>图形 <select id="core-style"><option value="line">折线图</option><option value="bar">柱状图</option></select></label>
<label>NUMA <select id="core-numa"><option value="all">全部 NUMA</option>%s</select></label>
<label>CPU起始 <input id="core-min" type="number" min="0" placeholder="全部"></label><label>结束 <input id="core-max" type="number" min="0" placeholder="全部"></label>
<button type="button" id="core-reset" class="metric-toggle">全部核心</button></div>
<div id="core-metrics" aria-label="点击显示或隐藏指标"></div>
<p class="core-summary" id="core-summary">%s</p>
%s<div class="core-dynamic" id="core-plot"></div><div id="core-tooltip" aria-live="polite"></div>
<noscript><p>JavaScript 已关闭。上方保留全部CPU静态图，下方表格保留基础统计；交互筛选需启用JavaScript。</p></noscript>
<script type="application/json" id="core-data">%s</script><script>%s</script></div>''' % (
        ''.join('<option value="%d">NUMA%d</option>'%(n,n) for n in data['nodes']),escape(data['note']),static_svg(data),safe_json(data),script)
