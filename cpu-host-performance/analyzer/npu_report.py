# -*- coding: utf-8 -*-
"""NPU affinity summaries and concrete AI task binding plans for HTML."""
from html import escape
from .environment_analysis import ENV_CATEGORIES
from .npu_affinity import STATE_CN
from .report_explanation import collection_actions_html
from .report_tables import DISPLAY_LIMIT


_EXPLANATION = ('allowed CPU 是调度许可范围；PSR 是采样时最近运行核，ftrace 是窗口内观测核。'
                '绿色表示全量现有线程的许可范围落在 NPU 亲和 CPU 内；橙色表示可跨节点或证据不足；'
                '红色表示观测到非亲和 CPU/绑定范围偏离。CPU 绑定与内存页 NUMA 分布分别核验，'
                '亲和性风险不等于已确认的 HostBound 根因。主进程按占卡显存最高推定，所有占卡 PID 均保留。')
_OPERATIONS = ('在原宿主机以有权限的账户执行，PID/TID 与采集时身份及 cpuset 需保持一致。'
               'taskset -apc 作用于指定进程的所有现有线程。现有子进程与辅助任务仅作清单展示。'
               '新建线程/子进程通常继承创建者当时的 affinity；taskset 不迁移既有内存页。')


def affinity_html(data, integrity=None):
    integrity=integrity or data.get("collection_integrity",{})
    total = len(data.get('rows', []))
    rows = data.get('rows', [])[:DISPLAY_LIMIT]
    if not rows:
        actions = [item for item in integrity.get('actions', []) if item.get('category') not in ENV_CATEGORIES]
        return ('<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">当前无可确认的NPU任务映射；'
                '采集状态与处理路径见下方。</p>' + collection_actions_html({'actions': actions}))
    parts = ['<h3>2.3 NPU 任务与 NUMA 亲和性</h3><p class="note">%s</p>' % escape(_EXPLANATION)]
    if total > DISPLAY_LIMIT:
        parts.append('<p>仅展示前100 / %d条进程亲和记录；完整记录见 report_data.json。</p>' % total)
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
    parts.append('</div>')
    return ''.join(parts)


def _id_preview(values):
    text = ', '.join(map(str, values[:8]))
    return text + (' 等 %d 项' % len(values) if len(values) > 8 else '')


def _ai_rankings_html(data, table):
    parts = []
    for metric, title in (('runtime_sec', 'CPU 运行时长 Top 10'),
                          ('cross_process_preemptions', '跨进程抢占切入 Top 10')):
        parts.append('<h4>%s</h4>' % title)
        is_runtime = metric == 'runtime_sec'
        if is_runtime:
            note = '按同名实例累计运行时长排序。进程行汇总其已观测线程。采集窗口 %.3f 秒。' % data['duration_sec']
        else:
            note = ('统计 R+ 调度切换中切入且与切出线程 TGID 不同的次数。'
                    '该指标记录调度切换及其两端，业务影响需结合等待和延迟。')
            if data['preempt_switches_unresolved']:
                note += '另有 %d 次切换缺少进程归属。' % data['preempt_switches_unresolved']
        if not data['complete']:
            note += '本次排名基于可归属的已观测样本。'
        parts.append('<p>%s</p>' % escape(note))
        for kind, label in (('processes', '按进程名称'), ('threads', '按线程名称')):
            parts.append('<h5>%s</h5>' % label)
            rows = sorted((row for row in data[kind] if row[metric] > 0),
                          key=lambda row: (-row[metric], row['name']))[:10]
            if not rows:
                empty = '本次缺少可归属的 AI 运行时长。'
                if not is_runtime:
                    empty = ('本次未观测到 R+ 切换。' if not data['preempt_switches']
                             else '本次未观测到可归属的 AI 跨进程抢占切入。')
                parts.append('<p>%s</p>' % empty)
                continue
            headers = ['名称', '同名实例数', 'PID/TGID']
            if kind == 'threads':
                headers.append('TID')
            headers.extend(['运行时长（CPU·秒）'] if is_runtime else ['切入次数', '切出方 PID/TGID'])
            values = []
            for row in rows:
                item = [row['name'], row['instance_count'], _id_preview(row['tgids'])]
                if kind == 'threads':
                    item.append(_id_preview(row['tids']))
                item.extend(['%.3f' % row[metric]] if is_runtime
                            else [row[metric], _id_preview(row['preempted_tgids'])])
                values.append(item)
            parts.append(table(headers, values, True, title=label + ' ' + title))
    return ''.join(parts)


def binding_html(data, integrity=None, plan=None, rankings=None):
    from urllib.parse import quote
    from .report_sections import table
    if plan is None:
        plan = data.get('binding_plan', {})
    tasks = plan.get('tasks', [])
    parts = ['<h3>具体绑核命令与脚本</h3><p>%s</p>' % escape(_OPERATIONS)]
    commands = ((plan.get('commands') if plan else data.get('commands')) or '').splitlines()
    if commands:
        parts.append('<div class="code-panel"><button type="button" class="copy-code" onclick="copyBinding(this)">复制绑核命令</button><pre><code>%s</code></pre></div>' % escape('\n'.join(commands[:DISPLAY_LIMIT])))
        if len(commands) > DISPLAY_LIMIT:
            parts.append('<p>仅展示前100 / %d条命令；完整脚本与JSON保留全部命令。</p>' % len(commands))
        if plan.get('script'):
            parts.append('<p><a download="bind_cpu.sh" href="data:text/x-shellscript;charset=utf-8,%s">下载绑核入口 bind_cpu.sh</a>（默认只预览。--apply 委托 Skill 的 scripts/ascend_bind.py，现场重新核验并生成计划、保存回滚日志、执行与读回；需 Python 3.8+ 及完整 Skill 目录。）</p>' % quote(plan['script'], safe=''))
            parts.append('<p>闭环顺序：本报告诊断 → plan 自动识别实际 NPU / Rank 标签 / 子进程线程 → NUMA 计划 → apply → 同负载复采及业务样本 verify → 无收益或异常 rollback。角色名称不能证明专核需求；Rank 标签不能代替实际设备归属。用 bash bind_cpu.sh --help 查看执行与回滚方式；详细指标格式见 Skill 的 references/binding_workflow.md。</p>')
    else:
        parts.append('<p>本次没有通过核验的绑核命令。</p>')
    blocked = [row for row in tasks if row['object_type'] == 'NPU 主进程' and row['status'] == 'BLOCKED']
    if blocked:
        parts.append('<h4>待核验的主进程</h4><ul>')
        parts.extend('<li>PID %d · %s</li>' % (row['pid'], escape(row['reason']))
                     for row in blocked[:DISPLAY_LIMIT])
        parts.append('</ul>')
        if len(blocked) > DISPLAY_LIMIT:
            parts.append('<p>显示前 %d / %d 个待核验 PID。完整清单见 report_data.json。</p>'
                         % (DISPLAY_LIMIT, len(blocked)))
    elif not commands:
        parts.append('<p>未确认占卡主 PID。可查看 <a href="#sec-2">NPU 任务与 NUMA 亲和性</a>和'
                     '<a href="#core-problems">核心问题与数据说明</a>。</p>')
    parts += ['<h3>AI 进程与线程清单</h3>', '<p>%s</p>' % escape(plan.get('note', '根据已采集的NPU、进程和线程关系整理。'))]
    if rankings:
        parts.append(_ai_rankings_html(rankings, table))
    if tasks:
        parts.append(table(['名称','NPU','PID/TGID','TID','父PID','Rank / Local Rank（环境标签）','对象','角色','当前CPU','绑定CPU','NUMA','状态 / 原因'], [
            [row['name'],row.get('npu_ids'),row['pid'],row['tid'],row.get('ppid'),'%s / %s'%(row.get('rank') or '未知',row.get('local_rank') or '未知'),row['object_type'],row['role'],row['current_cpu'],row['target_cpu'] or '无法确定',row['numa'],row['status']+'；'+row['reason']]
            for row in sorted(tasks, key=lambda item: str(item.get('name') or ''))], True, title='AI 进程与线程身份明细'))
    else:
        parts.append('<p>未识别到可关联的 AI 对象；不能用任意 Python 进程或示例 PID 代替。</p>')
    return ''.join(parts)
