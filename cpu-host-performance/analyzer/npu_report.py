# -*- coding: utf-8 -*-
"""Shared NPU evidence content for the offline HTML and Markdown reports."""
from html import escape
from .npu_affinity import STATE_CN


_EXPLANATION = ('allowed CPU 是调度许可范围；PSR 是采样时最近运行核，ftrace 是窗口内观测核。'
                '绿色表示全量现有线程的许可范围落在 NPU 亲和 CPU 内；橙色表示可跨节点或证据不足；'
                '红色表示观测到非亲和 CPU/绑定范围偏离。CPU 绑定与内存页 NUMA 分布分别核验，'
                '亲和性风险不等于已确认的 HostBound 根因。主进程按占卡显存最高推定，所有占卡 PID 均保留。')
_OPERATIONS = ('以下方案仅在原物理机/宿主机执行，使用宿主机 PID；需要 sudo、taskset 和 python3。'
               'taskset -acp 覆盖指定 PID 的现有线程，已有子进程应逐 PID 处理；新线程通常继承创建线程的 affinity，'
               '业务可重新设置，执行后需复查。taskset 不迁移既有内存页，不提供跨重启持久化，也不独占 CPU；'
               '同机其他业务/IRQ 的 CPU 争用仍须结合负载核验。有效 cpuset 不允许的区间需先在宿主机调整资源配置，再重新采集。')


def affinity_html(data):
    rows = data.get('rows', [])
    if not rows:
        return '<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">未提供可解析的 NPU 进程/拓扑快照，无法判断当前任务；最小补采集见第三部分。</p>'
    parts = ['<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">%s</p>' % escape(_EXPLANATION)]
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
    for key, value in [('NPU 亲和 CPU', row['affinity']), ('当前 allowed CPU（线程并集）', row['allowed']),
                       ('ftrace 观测 CPU', row['trace']), ('PSR 最近运行 CPU', row['psr'])]:
        parts.append('<div class="aff-line"><span>%s</span><code>%s</code></div>' % (key, escape(value or '未提供')))
    parts.append('<div class="aff-target"><span>建议分区 →</span><code>%s</code></div>' % escape(row['target'] or '待补证据'))
    if row['blockers']:
        parts.append('<p class="note">方案待核验：%s</p>' % escape('；'.join(row['blockers'])))
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
        lines.append('\n未提供可解析的 NPU 进程/拓扑快照，无法判断当前任务。')
    for note in data.get('notes', []):
        lines.append('\n> '+note)
    for r in data.get('rows', []):
        if r['blockers']:
            lines.append('\n- NPU %d / PID %s：%s' % (r['npu'], r['pid'], '；'.join(r['blockers'])))
    lines += ['', '证据来源：hostbound/npu_smi_*.txt、numactl.txt/lscpu_parse.txt、pid_* 状态/线程/affinity/cpuset；ftrace 运行观测与配置快照可能位于不同采样时点。']
    return '\n'.join(lines)


def binding_md(data):
    lines = ['### NPU / NUMA 绑核方案（宿主机执行）', '', _OPERATIONS, '']
    if data.get('commands'):
        lines += ['脚本先核验主机/进程身份、在线 CPU 和有效 cpuset，逐 TID 保存原 affinity 后执行；输出备份目录与可运行的回滚命令。', '', '```bash', data['commands'], '```', '',
                  '执行后以相同负载复采，比较吞吐、Device 空泡、调度延迟与跨节点运行；无收益或出现副作用时按脚本输出回滚。']
    else:
        lines += ['当前没有证据完整且需要变更的可执行绑核项。已有线程若全部落在建议分区内，则无需重复绑定。']
    if not data.get('rows') or any(r['blockers'] for r in data.get('rows', [])):
        lines += ['', '待核验项不生成猜测命令。一次性最小补采集（在物理机/宿主机运行；PID 从 npu-smi 自动发现）：', '',
                  '```bash', 'sudo bash scripts/cpu_trace_collect.sh --duration 30 --output /tmp --hostbound', '```']
    return '\n'.join(lines)


def binding_html(data):
    parts = ['<h3>NPU / NUMA 绑核方案（宿主机执行）</h3><p>%s</p>' % escape(_OPERATIONS)]
    if data.get('commands'):
        parts += ['<p>先核验、逐 TID 备份，再绑定与验证；脚本输出备份目录及可运行的回滚命令。</p>',
                  '<div class="code-panel"><button type="button" class="copy-code" onclick="copyBinding(this)">复制完整方案</button><pre><code>%s</code></pre></div>' % escape(data['commands']),
                  '<p class="note">执行后同负载复采，对比吞吐、Device 空泡、调度延迟与跨节点运行；无收益或出现副作用时回滚。</p>']
    else:
        parts.append('<p>当前没有证据完整且需要变更的可执行绑核项；已符合建议分区的线程无需重复绑定。</p>')
    blockers = [r for r in data.get('rows', []) if r['blockers']]
    if not data.get('rows') or blockers:
        parts.append('<p class="note">待核验项不生成猜测命令。最小补采集：在物理机/宿主机运行，自动发现所有占卡 PID。</p><pre><code>sudo bash scripts/cpu_trace_collect.sh --duration 30 --output /tmp --hostbound</code></pre>')
    return ''.join(parts)
