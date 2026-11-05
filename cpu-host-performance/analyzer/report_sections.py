"""Shared report sections for frequency, discovery, and evidence-backed contention."""
from html import escape
from .report_explanation import fmt
from .npu_affinity import _topology, cpu_list
from .report_tables import disclose_table, limit_markdown_tables
from .frequency_idle_chart import render_frequency_idle


def _text(value):
    if value is None:return '缺失 / 不可用'
    if isinstance(value,list):return ', '.join(str(x) for x in value) or '—'
    return str(value)


def table(headers,rows,html=False,title="完整明细",force=False):
    rows=list(rows)
    if html:
        markup = '<div class="core-table-scroll"><table><thead><tr>'+''.join('<th>%s</th>'%escape(x) for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>%s</td>'%escape(_text(x)) for x in row)+'</tr>' for row in rows)+'</tbody></table></div>'
        return disclose_table(markup,title,len(rows),force)
    def clean(x):return _text(x).replace('|','\\|').replace('\n',' ')
    return limit_markdown_tables('| '+' | '.join(headers)+' |\n|'+'|'.join('---' for _ in headers)+'|\n'+'\n'.join('| '+' | '.join(clean(x) for x in row)+' |' for row in rows))+'\n'


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
        out.append(table(['策略域','相关CPU','当前','时间加权平均','窗口最低','窗口最高','硬件最大参考','低频时间占比','Governor','来源 / 可靠性','样本数','接口读取限制'],rows,html,title='全部CPU Frequency策略域',force=html))
    else:out.append(paragraph('没有可用策略域数据；频率缺失时不显示0 MHz。',html))
    out.append(paragraph(f.get('note',''),html))
    out.append(paragraph('低频占比仅在cpuinfo_max_freq可靠参考存在时计算（≤其50%，经验筛选）；首个样本前不外推。窗口无频率转换不表示降频。当前值来自结束快照，平均值仅覆盖已观测样本持续区间。',html))
    idle=m.get('idle',{})
    out.append(heading('CPU Idle Residency',html))
    out.append(paragraph('Idle状态：%s；来源：%s；可靠性：%s。%s'%(idle.get('status','NO_EVENTS'),idle.get('source','ftrace:power/cpu_idle'),idle.get('reliability','UNKNOWN'),'；'.join(idle.get('reasons',[]))),html))
    out.append(paragraph(idle.get('note','没有可用cpu_idle事件；缺失值为N/A。swapper近似空闲率另见第6节。'),html))
    if html: out.append(render_frequency_idle(m))
    out.append(table(['CPU','整窗Idle Residency','已知Idle秒','有效覆盖','整窗Idle下界','各C-state持续秒'],[
        [p['cpu'],fmt(p.get('residency_percent'),'%'),fmt(p.get('idle_sec'),' s'),fmt(p.get('coverage_percent'),'%'),fmt(p.get('idle_lower_bound_percent'),'%'),'; '.join('state%s: %.6f s'%(x['state'],x['residency_sec']) for x in p.get('states',[])) or 'N/A'] for p in idle.get('cpus',[])],html,title='全部CPU Idle Residency',force=html))
    pmu=f.get('pmu',{})
    out.append(paragraph('PMU：%s；%s'%(pmu.get('status','NOT_VALIDATED'),pmu.get('reason','未验证')),html))
    return ''.join(out) if html else '\n'.join(out)


def thread_section(m,html=False):
    a=m.get('process_analysis',{});ct=m.get('contention',{});rows=m.get('thread_metrics',[])
    out=[heading('Process / Thread Discovery',html)]
    out.append(paragraph('已发现进程 %d，任务身份 %d，关联TGID %s。%s'%(len(a.get('processes',[])),len(a.get('tasks',[])),a.get('related_tgids',[]),a.get('note','')),html))
    if a.get('errors'):out.append(paragraph('快照读取失败/退出竞争 %d 项；错误保留在report_data.json。'%len(a['errors']),html))
    summary=a.get('system_summary',{})
    if summary: out.append(paragraph('系统进程 %s / 线程 %s；AI线程 %s；同核竞争线程 %s；非关联 %s；未知 %s；角色置信度 %s。'%(summary.get('total_processes'),summary.get('total_threads'),summary.get('ai_threads'),summary.get('competitor_threads'),summary.get('unrelated_threads'),summary.get('unknown_threads'),summary.get('confidence_counts')),html))
    by_id={t['identity']:t for t in rows}
    for key,label,fallback in [('ai_workers','AI Worker Top 10',ct.get('workers',[])),('ai_competitors','AI竞争线程 Top 10',[]),('system_top','系统线程 CPU Runtime Top 10',[])]:
        ids=ct.get(key,fallback)
        chosen=[by_id[x] for x in ids if x in by_id]
        if key=='system_top' and not chosen: chosen=sorted(rows,key=lambda t:-(t.get('runtime_sec') or 0))
        out.append(heading(label,html))
        out.append(table(['进程','TGID','TID','类别','角色/置信度','NPU','Runtime','Runqueue Wait','Wait P99','CPU','关系证据'],[
            [t.get('process_name') or t.get('name'),t.get('tgid'),t['tid'],t.get('category','UNKNOWN'),str(t.get('role'))+' / '+str(t.get('confidence')),t.get('npu_ids'),fmt(t.get('runtime_sec'),' s'),fmt(t.get('runqueue_wait_sec'),' s'),fmt(t.get('wait_p99_ms'),' ms'),t.get('cpus'),'; '.join(t.get('relation_evidence',[]))] for t in chosen[:10]],html))
    out.append(paragraph('AI Worker需要NPU PID/TGID、后代关系或运行证据支持；竞争线程需同核可运行等待时间重叠。仅线程名称/相同Affinity不足以确认角色或竞争因果。',html))
    mapping=[]
    for t in rows:
        mapping.append([t.get('process_name'),t.get('tgid'),t['tid'],t.get('ppid'),t['identity'],t.get('category','UNKNOWN'),t.get('role'),t.get('npu_ids',[]),cpu_list(t.get('allowed_cpus') or []) if t.get('allowed_cpus') is not None else None,t.get('last_cpu'),t.get('mems_allowed_list'),t.get('task_group'),t.get('confidence'),fmt(t.get('runtime_sec'),' s'),fmt(t.get('runtime_share'),'%'),fmt(t.get('cpu_utilization'),'%'),fmt(t.get('runqueue_wait_sec'),' s'),fmt(t.get('wait_p50_ms'),' ms'),fmt(t.get('wait_p95_ms'),' ms'),fmt(t.get('wait_p99_ms'),' ms'),t.get('wait_samples'),t.get('context_switches'),t.get('wakeups'),t.get('runnable_switch_out'),t.get('migrations'),t.get('cpus'),t.get('data_quality'),'; '.join(t.get('relation_evidence',[])),'; '.join(t.get('evidence',[]))])
    out.append(table(['进程','TGID','TID','PPID','任务身份/starttime','类别','角色','NPU关联','Affinity快照','PSR快照','Mems_allowed','Task Group','角色置信度','CPU Runtime','Runtime份额','线程利用率','Runqueue Wait','Wait P50','Wait P95','Wait P99','唤醒样本','上下文切换','成功唤醒','Runnable切出','Migration','观测CPU','数据质量','关系证据','采样证据'],mapping,html,title='全部进程/线程映射',force=html))
    out.append(heading('CPU Contention Analysis',html))
    out.append(paragraph(ct.get('note',''),html))
    by_id={t['identity']:t for t in rows}
    for name,label in [('runtime','Top CPU Runtime Threads'),('wait','Top Runqueue Wait Threads'),('runnable','Top Runnable Switch-out Threads'),('migration','Top CPU Migration Threads')]:
        out.append(heading(label,html));rank=[]
        for key in ct.get('rankings',{}).get(name,[]):
            t=by_id[key]
            rank.append([t.get('process_name') or t.get('name'),t.get('tgid'),t['tid'],t.get('npu_ids',[]),fmt(t['runtime_sec'],' s'),fmt(t.get('runtime_share'),'%'),fmt(t.get('cpu_utilization'),'%'),fmt(t.get('runqueue_wait_sec'),' s'),fmt(t.get('wait_p50_ms'),' ms'),fmt(t.get('wait_p95_ms'),' ms'),fmt(t.get('wait_p99_ms'),' ms'),t['wait_samples'],t.get('context_switches'),t.get('wakeups'),t.get('runnable_switch_out'),t.get('migrations'),t['cpus'],t.get('data_quality')])
        headers=['Process','TGID','TID','NPU','CPU Runtime','Runtime份额','线程利用率','Runqueue Wait','Wait P50','Wait P95','Wait P99','唤醒样本','上下文切换','成功唤醒','Runnable切出','Migration','观测CPU','数据质量']
        if html and len(rank)>10: out.append(table(headers,rank[:5],html))
        out.append(table(headers,rank,html,title=label+' 全部记录',force=html and len(rank)>10))
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
        ['%s/%s'%(c['target_tid'],c['target_tgid']),'%s/%s'%(c['contender_tid'],c['contender_tgid']),c['cpus'],sorted({c2n[x] for x in c['cpus'] if x in c2n}),fmt(c['overlap_sec']*1000),str(c['windows'])+'； '+'; '.join(c['evidence']),c['confidence'],c['causality']] for c in candidates],html))
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
        [c,c2n.get(c),fmt(p.get('observed_sec'),' s'),fmt(p.get('util'),'%'),fmt(p.get('idle_ratio'),'%'),fmt(p.get('cs_rate')),fmt(p.get('wakeup_rate')),fmt(p.get('runnable_switch_out_rate')),fmt(p.get('migration_rate')),fmt(p.get('irq_ratio'),'%'),fmt(p.get('softirq_ratio'),'%')] for c,p in sorted(m.get('per_cpu',{}).items())],html,title='全部CPU核心明细',force=html)


def coverage_section(m,html=False):
    return heading('可选工具证据覆盖（缺失不等于正常）',html)+table(['工具','状态','证据文件','限制'],[[r['name'],r['status'],r['evidence'],r['note']] for r in m.get('optional_tools',[])],html)


def topology_section(m,html=False):
    topology=m.get('topology',{})
    nodes=topology.get('nodes',[])
    count=topology.get('cpu_count',len(m.get('per_cpu',{})))
    out=[paragraph('CPU 总数 %s；NUMA 节点 %s；距离矩阵列顺序 %s。'%(count,topology.get('numa_count',topology.get('node_count',len(nodes))),topology.get('distance_node_order',[])),html)]
    if not nodes:
        _,mapped,_=_topology(m.get('snapshot',{}))
        nodes=[{'node':node,'cpus':cpus} for node,cpus in sorted(mapped.items())]
    def gib(value):
        return fmt(value/1048576.,' GiB') if isinstance(value,(int,float)) else 'N/A'
    rows=[]
    for node in nodes:
        memory=node.get('memory_kb',{})
        rows.append([node.get('node'),cpu_list(node.get('cpus',[])),gib(node.get('total_kb',memory.get('MemTotal'))),gib(node.get('free_kb',memory.get('MemFree'))),node.get('distance_by_node') or node.get('distance'),node.get('errors',{})])
    out.append(table(['NUMA','CPU列表','内存容量','内存空闲','节点距离','读取限制'],rows,html,title='全部NUMA拓扑与容量',force=html))
    out.append(paragraph('CPU许可范围、PSR/trace运行位置、Mems_allowed许可内存节点、numa_maps/numastat实际页分布分别核验。节点距离是拓扑成本提示，实际页不在本地不能单独证明跨节点访问导致性能损失；相关任务证据见2.3节。',html))
    return ''.join(out) if html else '\n'.join(out)


def resource_section(m, html=False, summary=False):
    r = m.get('resources', {})
    out = [heading('2.4 机器资源风险' if summary else '机器资源与压力（CPU / 内存 / 磁盘）', html)]
    if not r.get('available'):
        out.append(paragraph('本包缺少机器资源数据；无法据此排除内存、磁盘或资源配额不足。', html))
        return ''.join(out) if html else '\n'.join(out)
    out.append(paragraph('资源快照窗口 %s 秒，共 %s 份样本；与 trace 窗口分别计时。%s' %
        (fmt(r.get('window_sec')), r.get('sample_count', 0), r.get('note', '')), html))
    risks = r.get('risks', [])
    if risks:
        out.append(table(['风险候选', '依据', '判断限制'], [[x['title'], x['evidence'], x['note']] for x in risks], html, title='机器资源风险明细'))
    else:
        out.append(paragraph('已有资源数据未触发经验筛选条件；短窗口不能排除其他时段的资源不足。', html))
    if summary:
        return ''.join(out) if html else '\n'.join(out)
    cpu, memory = r.get('cpu', {}), r.get('memory', {})
    def gib(kb):
        return fmt(kb / 1048576., ' GiB') if isinstance(kb, (int, float)) else 'N/A'
    vm_text = '；'.join('%s %s' % (name, fmt(r.get('vm', {}).get(key), digits=0)) for name, key in
        [('换入页', 'pswpin_delta'), ('换出页', 'pswpout_delta'), ('重大缺页', 'pgmajfault_delta'),
         ('直接回收页', 'pgscan_direct_delta'), ('OOM kill', 'oom_kill_delta')])
    out.append(table(['资源', '当前容量 / 状态', '含义'], [
        ['CPU', '%s 逻辑核；窗口忙碌 %s；I/O wait %s；Load1 %s' % (fmt(cpu.get('logical_cpus'), digits=0), fmt(cpu.get('busy_percent'), '%'), fmt(cpu.get('iowait_percent'), '%'), fmt(cpu.get('load1'))), '来自 /proc/stat 前后增量；Load1 是1分钟负载，并非3秒均值'],
        ['内存', '总量 %s；MemAvailable %s（%s）；窗口最低 %s' % (gib(memory.get('total_kb')), gib(memory.get('available_kb')), fmt(memory.get('available_percent'), '%'), fmt(memory.get('minimum_available_percent'), '%')), '可用内存采用 MemAvailable，不用 MemFree 代替'],
        ['Swap', '总量 %s；剩余 %s' % (gib(memory.get('swap_total_kb')), gib(memory.get('swap_free_kb'))), '无 Swap 不等于异常；增量缺失显示 N/A'],
        ['内存回收 / OOM', vm_text, '计数回退或窗口不足不计算增量']
    ], html))
    out.append(table(['文件系统', '挂载点', '容量', '剩余', '空间使用率', 'inode使用率'], [
        [x['device'], x['mount'], gib(x['total_kb']), gib(x['available_kb']), fmt(x.get('used_percent'), '%'), fmt(x.get('inode_used_percent'), '%')]
        for x in r.get('filesystems', [])], html, title='文件系统容量与 inode'))
    out.append(table(['磁盘/块设备', '类型', '容量', '读取', '写入', 'IOPS', '平均请求耗时', 'I/O忙碌', '当前在途I/O'], [
        [x['name'], x.get('type'), gib(x['size_bytes']/1024.) if x.get('size_bytes') is not None else 'N/A',
         fmt(x['read_bytes_per_sec']/1048576., ' MiB/s') if x.get('read_bytes_per_sec') is not None else 'N/A',
         fmt(x['write_bytes_per_sec']/1048576., ' MiB/s') if x.get('write_bytes_per_sec') is not None else 'N/A',
         fmt(x.get('iops')), fmt(x.get('await_ms'), ' ms'), fmt(x.get('busy_percent'), '%'), x.get('in_flight')]
        for x in r.get('disks', [])], html, title='磁盘 I/O 明细'))
    out.append(table(['CPU/内存/I/O', '压力类型', '窗口停顿比例', '过去10秒平均', '过去60秒平均'], [
        [resource, kind, fmt(x.get('stall_percent'), '%'), fmt(x.get('avg10'), '%'), fmt(x.get('avg60'), '%')]
        for resource, kinds in r.get('pressure', {}).items() for kind, x in kinds.items()], html, title='PSI 资源压力'))
    out.append(table(['cgroup', '内存用量', '内存限额', '进程/线程数', '数量限额', 'OOM kill增量'], [
        [x['path'], gib(x['memory_current_bytes']/1024.) if x.get('memory_current_bytes') is not None else 'N/A',
         gib(x['memory_max_bytes']/1024.) if x.get('memory_max_bytes') is not None else '无限额或未知',
         x.get('pids_current'), x.get('pids_max'), x.get('oom_kill_delta')] for x in r.get('cgroups', [])], html, title='任务资源配额'))
    for note in r.get('limitations', []):
        out.append(paragraph(note, html))
    return ''.join(out) if html else '\n'.join(out)


def interrupt_tables(m,kind,html=False):
    from .metrics import EVENT_EXPLANATIONS, _explain_irq_name
    data=m.get(kind,{})
    key='names' if kind=='irq' else 'actions'
    label='硬中断来源' if kind=='irq' else '软中断类型'
    values=data.get('all_'+key,data.get('top_'+key,[]))
    rows=[[name,count,fmt(seconds*1000,' ms'),_explain_irq_name(name) if kind=='irq' else EVENT_EXPLANATIONS.get(name,'参考内核文档')] for name,count,seconds in values]
    headers=['中断源' if kind=='irq' else '软中断类型','次数','累计耗时','说明']
    if not rows: return ''
    out=[]
    if len(rows)>5:
        out.append(paragraph('按累计耗时排列的 Top 5；完整来源与次数保留在展开表格。',html))
        out.append(table(headers,rows[:5],html))
    out.append(table(headers,rows,html,title='全部'+label,force=html and len(rows)>5))
    return ''.join(out) if html else '\n'.join(out)
