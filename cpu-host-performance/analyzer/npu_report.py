# -*- coding: utf-8 -*-
"""Shared NPU evidence content for the offline HTML and Markdown reports."""
from html import escape
from .npu_affinity import STATE_CN
from .report_explanation import collection_actions_html, collection_actions_md
from .report_tables import disclose_table


_EXPLANATION = ('allowed CPU 是调度许可范围；PSR 是采样时最近运行核，ftrace 是窗口内观测核。'
                '绿色表示全量现有线程的许可范围落在 NPU 亲和 CPU 内；橙色表示可跨节点或证据不足；'
                '红色表示观测到非亲和 CPU/绑定范围偏离。CPU 绑定与内存页 NUMA 分布分别核验，'
                '亲和性风险不等于已确认的 HostBound 根因。主进程按占卡显存最高推定，所有占卡 PID 均保留。')
_OPERATIONS = ('在原宿主机以有权限的账户执行，PID/TID 与采集时身份及 cpuset 需保持一致。'
               'taskset -apc 作用于指定进程的所有现有线程；已知子进程单独列出，taskset -pc 作用于指定线程。'
               '新建线程/子进程通常继承创建者当时的 affinity；taskset 不迁移既有内存页。')


def affinity_html(data, integrity=None):
    integrity=integrity or data.get("collection_integrity",{})
    rows = data.get('rows', [])
    if not rows:
        return '<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">当前无可确认的NPU任务映射；采集状态与处理路径见下方。</p>'+collection_actions_html(integrity)
    parts = ['<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">%s</p>' % escape(_EXPLANATION)]
    if len(rows)>12: parts.append('<details class="npu-details"><summary>全部NPU任务亲和明细（共 %d 条记录，展开）</summary>'%len(rows))
    groups = {}
    for row in rows:
        label = 'NUMA '+','.join(map(str, row['numa'])) if row['numa'] else 'NUMA 待核验'
        groups.setdefault(label, []).append(row)
    for label, members in sorted(groups.items()):
        parts.append('<section class="numa-group"><div class="numa-title">%s <span>%d 张 NPU</span></div><div class="npu-grid">' % (escape(label), len({r['npu'] for r in members})))
        cards = {}
        for row in members:
            cards.setdefault(row['npu'], []).append(row)
        for npu, card_rows in sorted(cards.items()):
            tone = 'danger' if any(r['state'].startswith('REMOTE') for r in card_rows) else ('good' if all(r['state'] == 'LOCAL' for r in card_rows) else 'caution')
            parts.append('<article class="npu-card %s"><b>NPU %d</b>' % (tone, npu))
            for row in card_rows:
                parts.append(_process_html(row))
            parts.append('</article>')
        parts.append('</div></section>')
    if len(rows)>12: parts.append('</details>')
    if data.get('children'):
        parts.append('<h4>已识别的子进程</h4><table><thead><tr><th>宿主机 PID</th><th>AI 父进程</th><th>建议 CPU</th><th>证据/阻碍</th></tr></thead><tbody>')
        for row in data['children']:
            parts.append('<tr><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>' % tuple(escape(str(v)) for v in
                (row['pid'], row['parent_pid'], row['target'], '；'.join(row['blockers']) or '稳定父子关系、身份及逐线程 cpuset 已核验')))
        parts.append('</tbody></table>')
    for note in data.get('notes', []):
        parts.append('<p class="note">%s</p>' % escape(note))
    parts.append('<p class="note">证据：hostbound/npu_smi_info.txt、npu_smi_topology.txt、numactl.txt/lscpu_parse.txt、pid_*_{status,threads,thread_affinity,cpuset_effective}.txt；运行观测与配置快照可能位于不同采样时点。</p>')
    return ''.join(parts)


def _process_html(row):
    parts = []
    state = row['state']
    tone = 'good' if state == 'LOCAL' else ('danger' if state.startswith('REMOTE') else 'caution')
    parts.append('<div class="npu-process-entry %s"><div class="npu-heading"><span>Chip %s</span><span class="aff-state">%s</span></div>' % (tone, row['chip'] if row['chip'] is not None else '—', escape(STATE_CN[state])))
    parts.append('<div class="npu-process">%s <strong>PID %s</strong><small>%s · %d 个线程 · %s MB</small></div>' % (escape(row['name']), row['pid'] if row['pid'] else '—', escape(row['role']), row['threads'], row['memory'] if row['memory'] is not None else '—'))
    locality = {'LOCAL_OBSERVED': '运行观测位于亲和 NUMA', 'REMOTE_OBSERVED': '观测到非亲和 NUMA 运行', 'UNKNOWN': '运行 NUMA 待核验'}[row['numa_locality']]
    numa_tone = {'LOCAL_OBSERVED': 'local-numa', 'REMOTE_OBSERVED': 'remote-numa', 'UNKNOWN': 'unknown-numa'}[row['numa_locality']]
    parts.append('<p class="numa-verdict %s">%s</p>' % (numa_tone, locality))
    parts.append('<div class="aff-line"><span>运行观测 NUMA → 亲和 NUMA</span><code>%s → %s</code></div>' % (escape(','.join(map(str, row['observed_numa'])) or '未知'), escape(','.join(map(str, row['numa'])) or '未知')))
    for key, value in [('当前 allowed CPU（线程并集）', row['allowed']), ('ftrace 观测 CPU', row['trace'])]:
        parts.append('<div class="aff-line"><span>%s</span><code>%s</code></div>' % (key, escape(value or '未提供')))
    parts.append('<div class="aff-target"><span>建议CPU / NUMA →</span><code>%s / %s</code></div>' % (escape(str(row.get('recommended_cpu') or row['target'] or '待补证据')),escape(str(row.get('recommended_numa','待核验')))))
    parts.append('<details class="npu-evidence"><summary>依据、来源、内存与线程证据（展开）</summary>')
    for key,value in [('NPU 亲和 CPU',row['affinity']),('PSR 最近运行 CPU',row['psr'])]:
        parts.append('<div class="aff-line"><span>%s</span><code>%s</code></div>'%(key,escape(value or '未提供')))
    if row.get('sources'):
        parts.append('<p class="note"><b>证据来源：</b>%s</p>'%escape('；'.join(map(str,row['sources']))))
    memory=row.get('memory_locality',{})
    if not isinstance(memory,dict): memory={}
    if memory:
        parts.append('<div class="aff-line"><span>内存许可节点（Mems_allowed）</span><code>%s</code></div>'%escape(str(memory.get('allowed_nodes','未知'))))
        parts.append('<div class="aff-line"><span>实际页分布 / 节点</span><code>%s / %s</code></div>'%(escape(str(memory.get('actual_pages_by_node','未知'))),escape(str(memory.get('actual_nodes','未知')))))
        parts.append('<p class="note">内存证据：%s；%s。许可节点不代表实际页驻留；跨节点页存在也不等于已证实远端访问性能损失。</p>'%(escape(str(memory.get('source','未提供'))),escape(str(memory.get('status','UNKNOWN')))))
    for key,label in [('mapping_status','映射状态'),('identity_status','进程身份'),('recommendation_status','建议状态'),('reasons','原因'),('risks','风险'),('expected_improvement','预期改善'),('verification','验证方法'),('rollback','回滚路径')]:
        value=row.get(key)
        if value:
            parts.append('<p class="note"><b>%s：</b>%s</p>'%(label,escape('；'.join(map(str,value)) if isinstance(value,list) else str(value))))
    thread_rows=row.get('thread_rows',[])
    if thread_rows:
        headers=sorted(set(k for entry in thread_rows for k in entry))
        markup='<table><thead><tr>'+''.join('<th>%s</th>'%escape(str(x)) for x in headers)+'</tr></thead><tbody>'+''.join('<tr>'+''.join('<td>%s</td>'%escape(str(entry.get(x,'N/A'))) for x in headers)+'</tr>' for entry in thread_rows)+'</tbody></table>'
        parts.append(disclose_table(markup,'此PID全部线程亲和证据',len(thread_rows),True))
    if row['blockers']:
        parts.append('<p class="note">方案待核验：%s</p>' % escape('；'.join(row['blockers'])))
    parts.append('</details>')
    parts.append('</div>')
    return ''.join(parts)


def affinity_md(data):
    lines = ['### 2.3 NPU 任务与 NUMA 亲和性', '', _EXPLANATION, '',
             '| NPU/Chip | 主进程/其他进程 | 宿主机 PID | 线程数 | 亲和 NUMA / CPU | 当前 allowed CPU | 观测 NUMA | ftrace / PSR | CPU 亲和判断 | 建议分区 |',
             '|---|---|---:|---:|---|---|---|---|---|---|']
    for r in data.get('rows', []):
        lines.append('| %d/%s | %s（%s） | %s | %d | %s / %s | %s | %s | %s / %s | %s | %s |' % (
            r['npu'], r['chip'] if r['chip'] is not None else '—', r['name'].replace('|', '\\|'), r['role'], r['pid'] or '—', r['threads'],
            ','.join(map(str, r['numa'])) or '待核验', r['affinity'] or '未知', r['allowed'] or '未知',
            ','.join(map(str, r['observed_numa'])) or '未知',
            r['trace'] or '未提供', r['psr'] or '未提供', STATE_CN[r['state']], r['target'] or '待补证据'))
    if not data.get('rows'):
        lines.append('\n当前无可确认的NPU任务映射；采集清单未记录的行为不能视为成功。')
        lines.append(collection_actions_md(data.get('collection_integrity',{})))
    for note in data.get('notes', []):
        lines.append('\n> '+note)
    for r in data.get('rows', []):
        if r['blockers']:
            lines.append('\n- NPU %d / PID %s：%s' % (r['npu'], r['pid'], '；'.join(r['blockers'])))
        for key,label in [('mapping_status','映射状态'),('identity_status','进程身份'),('recommendation_status','建议状态'),('recommended_cpu','建议CPU'),('recommended_numa','建议NUMA'),('reasons','原因'),('risks','风险'),('expected_improvement','预期改善'),('verification','验证方法'),('rollback','回滚路径')]:
            value=r.get(key)
            if value:
                lines.append('\n- NPU %d / PID %s · %s：%s'%(r['npu'],r['pid'],label,'；'.join(map(str,value)) if isinstance(value,list) else value))
        memory=r.get('memory_locality',{})
        if memory:
            lines.append('\n- NPU %d / PID %s · Mems_allowed许可节点 %s；实际页分布 %s；来源 %s；状态 %s。'%(r['npu'],r['pid'],memory.get('allowed_nodes'),memory.get('actual_pages_by_node'),memory.get('source'),memory.get('status')))
    if data.get('children'):
        lines += ['', '#### 已识别的子进程', '', '| 宿主机 PID | AI 父进程 | 建议 CPU | 证据/阻碍 |', '|---:|---:|---|---|']
        for row in data['children']:
            lines.append('| %s | %s | %s | %s |' % (row['pid'], row['parent_pid'], row['target'], '；'.join(row['blockers']) or '稳定父子关系、身份及逐线程 cpuset 已核验'))
    lines += ['', '证据来源：hostbound/npu_smi_*.txt、numactl.txt/lscpu_parse.txt、pid_* 状态/线程/affinity/cpuset；ftrace 运行观测与配置快照可能位于不同采样时点。']
    return '\n'.join(lines)


def binding_md(data):
    lines = ['### NPU / NUMA 绑核命令（宿主机执行）', '', _OPERATIONS, '']
    if data.get('commands'):
        lines += ['```bash', data['commands'], '```']
    else:
        lines += ['当前没有证据充分且需要变更的绑核项；请参阅第二部分的证据与缺项。']
    return '\n'.join(lines)


def binding_html(data, integrity=None):
    parts = ['<h3>NPU / NUMA 绑核命令（宿主机执行）</h3><p>%s</p>' % escape(_OPERATIONS)]
    if data.get('commands'):
        parts.append('<div class="code-panel"><button type="button" class="copy-code" onclick="copyBinding(this)">复制绑核命令</button><pre><code>%s</code></pre></div>' % escape(data['commands']))
    else:
        parts.append('<p>当前没有证据充分且需要变更的绑核项；请参阅第二部分的证据与缺项。</p>')
    return ''.join(parts)
