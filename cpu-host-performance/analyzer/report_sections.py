"""Shared report sections for frequency, discovery, and evidence-backed contention."""
from html import escape
from .report_explanation import fmt
from .npu_affinity import _topology, cpu_list


def _text(value):
    if value is None:return '缺失 / 不可用'
    if isinstance(value,list):return ', '.join(str(x) for x in value) or '—'
    return str(value)


def table(headers,rows,html=False):
    if html:
        return '<div class="core-table-scroll"><table><thead><tr>'+''.join('<th>%s</th>'%escape(x) for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>%s</td>'%escape(_text(x)) for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'
    def clean(x):return _text(x).replace('|','\\|').replace('\n',' ')
    return '| '+' | '.join(headers)+' |\n|'+'|'.join('---' for _ in headers)+'|\n'+'\n'.join('| '+' | '.join(clean(x) for x in row)+' |' for row in rows)+'\n'


def paragraph(text,html):
    return '<p>%s</p>'%escape(text) if html else text+'\n\n'


def heading(text,html):
    return '<h3>%s</h3>'%escape(text) if html else '### '+text+'\n\n'


def frequency_section(m,html=False):
    f=m.get('frequency',{});out=[heading('CPU Frequency Analysis',html)]
    out.append(paragraph('频率状态：%s；事件状态：%s；动态样本数：%s。%s'%(f.get('status','UNKNOWN'),f.get('event_status','UNKNOWN'),f.get('sample_count',0),'；'.join(f.get('reasons',[]))),html))
    rows=[]
    for p in f.get('domains',[]):
        rows.append([p['id'],cpu_list(p['cpus']),fmt(p['current_mhz'],' MHz'),fmt(p['average_mhz'],' MHz'),fmt(p['minimum_mhz'],' MHz'),fmt(p['highest_mhz'],' MHz'),fmt(p['maximum_mhz'],' MHz'),fmt(p['low_frequency_ratio'],'%'),p.get('governor'),p['source']+' / '+p['reliability'],p['sample_count'],str(p.get('errors',{}))])
    if rows:
        out.append(table(['策略域','相关CPU','当前','时间加权平均','窗口最低','窗口最高','硬件最大参考','低频时间占比','Governor','来源 / 可靠性','样本数','接口读取限制'],rows,html))
    else:out.append(paragraph('没有可用策略域数据；频率缺失时不显示0 MHz。',html))
    out.append(paragraph(f.get('note',''),html))
    out.append(paragraph('低频占比仅在cpuinfo_max_freq可靠参考存在时计算（≤其50%，经验筛选）；首个样本前不外推。窗口无频率转换不表示降频。当前值来自结束快照，平均值仅覆盖已观测样本持续区间。',html))
    if html:
        for p in f.get('domains',[]):
            values=p['series']
            if not values:continue
            lo=min(t for t,v in values);hi=max(t for t,v in values);vmax=max(v for t,v in values)*1.1
            points=' '.join('%.2f,%.2f'%(45+900*(t-lo)/(hi-lo or 1),140-110*v/(vmax or 1)) for t,v in values)
            out.append('<div class="chart"><p>%s · %s · 时间戳 %.3f~%.3f 秒</p><svg viewBox="0 0 1000 180" role="img" aria-label="策略域频率趋势"><text x="5" y="20" fill="#9eaec2" font-size="12">MHz</text><polyline points="%s" fill="none" stroke="#68b8ff" stroke-width="2"/>'%(escape(p['id']),escape(p['source']),lo,hi,points))
            for t,v in values:
                out.append('<circle cx="%.2f" cy="%.2f" r="2.5" fill="#62d6a5"><title>t=%.6f / %.2f MHz</title></circle>'%(45+900*(t-lo)/(hi-lo or 1),140-110*v/(vmax or 1),t,v))
            out.append('<text x="45" y="168" fill="#9eaec2" font-size="12">%.1f ~ %.1f MHz；已知持续 %.3f s；样本 %d</text></svg></div>'%(p['minimum_mhz'],p['highest_mhz'],p['observed_sec'],p['sample_count']))
    pmu=f.get('pmu',{})
    out.append(paragraph('PMU：%s；%s'%(pmu.get('status','NOT_VALIDATED'),pmu.get('reason','未验证')),html))
    return ''.join(out) if html else '\n'.join(out)


def thread_section(m,html=False):
    a=m.get('process_analysis',{});ct=m.get('contention',{});rows=m.get('thread_metrics',[])
    out=[heading('Process / Thread Discovery',html)]
    out.append(paragraph('已发现进程 %d，任务身份 %d，关联TGID %s。%s'%(len(a.get('processes',[])),len(a.get('tasks',[])),a.get('related_tgids',[]),a.get('note','')),html))
    if a.get('errors'):out.append(paragraph('快照读取失败/退出竞争 %d 项；错误保留在report_data.json。'%len(a['errors']),html))
    mapping=[]
    for t in rows:
        mapping.append([t.get('process_name'),t.get('tgid'),t['tid'],t.get('ppid'),t['identity'],t.get('role'),t.get('npu_ids',[]),cpu_list(t.get('allowed_cpus') or []) if t.get('allowed_cpus') is not None else None,t.get('last_cpu'),t.get('mems_allowed_list'),t.get('task_group'),t.get('confidence'),'; '.join(t.get('evidence',[]))])
    out.append(table(['进程','TGID','TID','PPID','任务身份/starttime','角色','NPU关联','Affinity快照','PSR快照','Mems_allowed','Task Group','角色置信度','证据'],mapping,html))
    out.append(heading('CPU Contention Analysis',html))
    out.append(paragraph(ct.get('note',''),html))
    by_id={t['identity']:t for t in rows}
    for name,label in [('runtime','Top CPU Runtime Threads'),('wait','Top Runqueue Wait Threads'),('runnable','Top Runnable Switch-out Threads'),('migration','Top CPU Migration Threads')]:
        out.append(heading(label,html));rank=[]
        for key in ct.get('rankings',{}).get(name,[]):
            t=by_id[key]
            rank.append([t.get('process_name') or t.get('name'),t.get('tgid'),t['tid'],t.get('npu_ids',[]),fmt(t['runtime_sec'],' s'),fmt(t.get('runtime_share'),'%'),fmt(t.get('cpu_utilization'),'%'),fmt(t.get('runqueue_wait_sec'),' s'),fmt(t.get('wait_p50_ms'),' ms'),fmt(t.get('wait_p95_ms'),' ms'),fmt(t.get('wait_p99_ms'),' ms'),t['wait_samples'],t.get('context_switches'),t.get('wakeups'),t.get('runnable_switch_out'),t.get('migrations'),t['cpus'],t.get('data_quality')])
        out.append(table(['Process','TGID','TID','NPU','CPU Runtime','Runtime份额','线程利用率','Runqueue Wait','Wait P50','Wait P95','Wait P99','唤醒样本','上下文切换','成功唤醒','Runnable切出','Migration','观测CPU','数据质量'],rank,html))
    out.append(paragraph('Runtime份额以全体任务已观测runtime为分母，线程利用率以采集窗口为分母；Runtime含任务上下文内IRQ，不等同纯用户态CPU。Runqueue Wait为已完成成功唤醒+R/R+切出的等待区间下界，Wait P99仅含成功唤醒；未配对等待不填零。',html))
    _,nodes,_=_topology(m.get('snapshot',{}));c2n={c:n for n,cs in nodes.items() for c in cs}
    out.append(heading('NPU Worker CPU 竞争情况与证据链',html));chains=[]
    for key in ct.get('workers',[]):
        t=by_id[key];candidates=[c for c in ct.get('candidates',[]) if c['target_identity']==key]
        observed_nodes=sorted({c2n[c] for c in t['cpus'] if c in c2n})
        chains.append([t.get('npu_ids'),t.get('tgid'),t['tid'],t.get('role'),cpu_list(t['cpus']),observed_nodes,fmt(t.get('wait_p99_ms'),' ms'),'; '.join('TID%d %.3fms CPU%s'%(c['contender_tid'],c['overlap_sec']*1000,c['cpus']) for c in candidates) or '未观测到候选；不代表绝无竞争'])
    out.append(table(['NPU','Worker/关联TGID','TID','角色','观测CPU','NUMA','Wait P99','候选与重叠'],chains,html))
    candidates=ct.get('candidates',[])
    out.append(table(['目标TID/TGID','候选TID/TGID','CPU','NUMA','重叠ms','时间窗证据','置信度','因果'],[
        ['%s/%s'%(c['target_tid'],c['target_tgid']),'%s/%s'%(c['contender_tid'],c['contender_tgid']),c['cpus'],sorted({c2n[x] for x in c['cpus'] if x in c2n}),fmt(c['overlap_sec']*1000),str(c['windows'][:3])+'（其余窗口见JSON，最多保存20个例证）； '+'; '.join(c['evidence']),c['confidence'],c['causality']] for c in candidates],html))
    out.append(heading('热点CPU与关联线程',html))
    out.append(table(['CPU','NUMA','利用率','TID'],[[r['cpu'],c2n.get(r['cpu']),fmt(r['util'],'%'),sorted(set(r['threads']))] for r in ct.get('hot_cores',[])],html))
    out.append(heading('CPU Affinity 重叠（配置相关性）',html))
    out.append(paragraph('Affinity比较范围：'+str(ct.get('affinity_scope',{})),html))
    out.append(table(['TID A','TID B','重叠CPU','因果'],[[r['a_tid'],r['b_tid'],cpu_list(r['cpus']),r['causality']] for r in ct.get('affinity_overlaps',[])],html))
    out.append(heading('cgroup CPU quota / throttling',html))
    cg=ct.get('cgroups',[])
    if not cg:out.append(paragraph('缺少可配对cgroup快照；不能排除容器配额限制。',html))
    out.append(table(['cgroup/祖先','TGID','Quota核数','nr_throttled增量','throttled_usec增量','来源与限制'],[[r['path'],r['tgids'],('无限制（核验祖先）' if r.get('quota_status')=='UNLIMITED' else fmt(r['quota_cpus'])),r['nr_throttled_delta'],r['throttled_usec_delta'],r['evidence']+'; '+r['note']] for r in cg],html))
    out.append(paragraph('Device时间关联：%s；匹配 %s。未提供或未对齐时，只能报告Host侧风险，不能确认NPU性能根因。'%(ct.get('device_correlation',{}).get('status'),ct.get('device_correlation',{}).get('matches')),html))
    out.append(paragraph('优化与验证：先核验目标/候选线程职责、有效cpuset、IRQ Affinity和cgroup限额；比较同负载下的Worker等待分位数、重叠窗口与NPU吞吐/请求延迟。调整并发或配额等方案须经用户确认，保存原配置，复采同一窗口验证收益与副作用。',html))
    return ''.join(out) if html else '\n'.join(out)


def core_table(m,html=False):
    _,nodes,_=_topology(m.get('snapshot',{}));c2n={c:n for n,cs in nodes.items() for c in cs}
    return table(['CPU','NUMA','观测秒','利用率','空闲率','切换/秒','唤醒/秒','R切出/秒','迁移/秒','硬中断','软中断'],[
        [c,c2n.get(c),fmt(p.get('observed_sec'),' s'),fmt(p.get('util'),'%'),fmt(p.get('idle_ratio'),'%'),fmt(p.get('cs_rate')),fmt(p.get('wakeup_rate')),fmt(p.get('runnable_switch_out_rate')),fmt(p.get('migration_rate')),fmt(p.get('irq_ratio'),'%'),fmt(p.get('softirq_ratio'),'%')] for c,p in sorted(m.get('per_cpu',{}).items())],html)


def coverage_section(m,html=False):
    return heading('可选工具证据覆盖（缺失不等于正常）',html)+table(['工具','状态','证据文件','限制'],[[r['name'],r['status'],r['evidence'],r['note']] for r in m.get('optional_tools',[])],html)
