"""Compile measured hostbound sidecars into a frozen per-customer affinity plan."""
import json
import math
import re
from collections import defaultdict
from itertools import chain, combinations
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from analyzer.npu_affinity import cpu_list
from .custom import recommend
from .linux import LinuxHost, cpulist
from .network import interrupt_delta
from .planner import digest, role


def require(condition, message):
    if not condition:
        raise ValueError(message)


def index(rows, key):
    result = {row[key]: row for row in rows}
    require(len(result) == len(rows), 'Duplicate identity: ' + key)
    return result


def topology(folder):
    online = cpulist((folder / 'cpu_online.txt').read_text())
    nodes, groups = {}, defaultdict(list)
    for line in (folder / 'lscpu_parse.txt').read_text().splitlines():
        if not line.strip() or line.startswith('#'):
            continue
        cpu, core, socket, node, enabled = line.strip().split(',')
        if enabled.upper() not in ('Y', 'YES', '1'):
            continue
        cpu, core, socket, node = map(int, (cpu, core, socket, node))
        require(min(cpu, core, socket, node) >= 0 and str(cpu) not in nodes, 'Unknown/duplicate CPU topology')
        nodes[str(cpu)] = node
        groups[socket, core].append(cpu)
    require(set(map(int, nodes)) == set(online), 'lscpu and CPU online disagree')
    require(all(len({nodes[str(cpu)] for cpu in group}) == 1 for group in groups.values()), 'SMT crosses NUMA')
    return dict(online=online, cpu_node=nodes, siblings={str(cpu):sorted(group) for group in groups.values() for cpu in group})


def validate_pool_budget(proposal, topo, owners, selected, threads, rows):
    other_pids=set(owners)-set(selected)
    reserved=set(cpu for worker in proposal['workers'] for cpu in worker['cpus'])
    for pid in other_pids:
        require(not (set(owners[pid]) & {npu for worker in proposal['workers'] for npu in worker['npus']}), 'Unaccounted co-owner on selected NPU')
        for tid in threads.get(pid,[]):
            row=rows.get(tid)
            if row and row['original'] and len(row['original'])<=2:
                require(not any(set(topo['siblings'][str(cpu)]) & reserved for cpu in row['original'] if str(cpu) in topo['siblings']),
                        'Another NPU job has a narrow reservation on the proposed core; add protected_cpus')


def ancestor(pid, owners, processes):
    seen = set()
    while pid in processes and pid not in seen:
        if pid in owners:
            return pid
        seen.add(pid)
        pid = processes[pid]['ppid']
    return None


def complete_lineage(pid, processes):
    seen=set()
    while pid>1 and pid not in seen and pid in processes:
        seen.add(pid); pid=processes[pid]['ppid']
    return pid==1


def capture(folder):
    def text(name): return (folder / name).read_text(encoding='utf-8-sig').strip()
    context = dict(line.split('=', 1) for line in text('host_context.txt').splitlines() if '=' in line)
    require(context.get('execution_context') == 'host' and context.get('boot_id') and
            context.get('pid_namespace') and context['pid_namespace'] == context.get('init_pid_namespace'), 'Missing host boot/PID namespace proof')
    mapping, locality = text('npu_smi_map.txt'), text('npu_smi_topology.txt')
    devices, owners = LinuxHost.decode_npu(mapping, locality, text('npu_smi_info.txt'))
    require((devices, owners) == LinuxHost.decode_npu(mapping, locality, text('npu_smi_info_start.txt')), 'NPU ownership changed in window')
    topo = topology(folder); topo['npu_cpus'] = devices
    before, after = [json.loads(text('process_'+phase+'.json')) for phase in ('start','end')]
    require(before['schema_version'] == after['schema_version'] == 2 and before['clock'] == after['clock'] == 'boottime'
            and after['ts'] > before['end_ts'] and before['clock_ticks'] == after['clock_ticks'] > 0, 'Need comparable process windows')
    leaders = [index(snap['processes'], 'tgid') for snap in (before,after)]
    threads = [index(snap['threads'], 'tid') for snap in (before,after)]
    end_rows = defaultdict(list); gaps = []; rows = {}
    error_ids={str(error.get('id')) for snap in (before,after) for error in snap.get('errors',[]) if isinstance(error,dict)}
    for tid, last in threads[1].items():
        pid = last['tgid']; end_rows[pid].append(tid)
        first = threads[0].get(tid); proc = leaders[1].get(pid); old = leaders[0].get(pid)
        if not (str(tid) not in error_ids and str(pid) not in error_ids and first and old and proc and all(first.get(key) == last.get(key) for key in
                ('tgid','starttime','name','cgroup','allowed_cpus','cpuset_cpus_effective')) and
                all(old.get(key) == proc.get(key) for key in ('starttime','ppid','cgroup','kernel_thread'))):
            gaps.append(tid); continue
        seconds = last['identity_started_at'] - first['identity_started_at']
        ticks = [last[key]-first[key] for key in ('utime_ticks','stime_ticks')]
        if not (seconds > 0 and min(ticks) >= 0):
            gaps.append(tid); continue
        rows[tid] = dict(pid=pid, tid=tid, name=last['name'], role=role(last['name'],tid,pid),
                         process_starttime=str(proc['starttime']), thread_starttime=str(last['starttime']),
                         ppid=proc['ppid'], cgroup=last.get('cgroup'), original=last['allowed_cpus'],
                         ceiling=last.get('cpuset_cpus_effective') if last.get('cgroup_source')=='task' else None,
                         kernel=proc.get('kernel_thread') is not False or bool(last.get('flags',0) & 0x200000),
                         utilization=round(sum(ticks) / after['clock_ticks'] / seconds, 5))
    return context, topo, owners, leaders, threads, end_rows, rows, gaps


def inspect(folder, hot_fraction=.20):
    context,topo,owners,leaders,threads,membership,rows,gaps=capture(Path(folder))
    def listing(pid):
        return [rows.get(tid,dict(tid=tid,status='NO_COMPARABLE_WINDOW')) for tid in sorted(membership[pid])]
    owner_rows=[dict(pid=pid,npus=cards,threads=listing(pid)) for pid,cards in sorted(owners.items())]
    others=sorted((row for row in rows.values() if row['pid'] not in owners and row['utilization']>=hot_fraction),
                  key=lambda row:(-row['utilization'],row['tid']))
    return dict(context=context,topology=topo,owners=owner_rows,hot_others=others,
                descendants=[dict(pid=pid,owner_pid=ancestor(pid,owners,leaders[1])) for pid in leaders[1]
                             if pid not in owners and ancestor(pid,owners,leaders[1]) is not None],
                unsampled_tids=gaps, exited_tids=sorted(set(threads[0])-set(threads[1])),hot_fraction=hot_fraction,
                note='Review only; a name or a single hot window does not establish job ownership or permission to move a task.')


def distribute(members, pool, weights, siblings, desired, cap, repin=False):
    candidates = {row['tid']: set(pool).intersection(row['ceiling'] or [], pool if repin else row['original'] or []) for row in members}
    require(all(candidates.values()), 'No eligible CPU for TID(s): '+str([tid for tid, cpus in candidates.items() if not cpus]))
    def core(cpu): return tuple(siblings[str(cpu)])
    choices = sorted(set.union(*candidates.values()), key=lambda cpu:(weights[core(cpu)],cpu))
    constrained = any(len(allowed) < len(choices) for allowed in candidates.values())
    if not constrained and cap==2:
        choices = list(dict.fromkeys(core(cpu) for cpu in choices))[:2]
        choices = [min(set(group) & candidates[members[0]['tid']]) for group in choices]
    for size in sorted(range(1,cap+1), key=lambda size:(abs(size-desired),size)):
        groups=combinations(choices,size)
        if size==2:
            groups=chain((pair for pair in combinations(choices,2) if core(pair[0])!=core(pair[1])),
                         (pair for pair in combinations(choices,2) if core(pair[0])==core(pair[1])))
        for group in groups:
            if not all(set(group) & allowed for allowed in candidates.values()): continue
            if sum(row['utilization'] for row in members)>len({core(cpu) for cpu in group})+.01: continue
            for row in sorted(members,key=lambda row:(len(candidates[row['tid']] & set(group)),-row['utilization'],row['tid'])):
                cpu = min(candidates[row['tid']] & set(group),key=lambda cpu:(weights[core(cpu)],cpu))
                weights[core(cpu)] += max(.001,row['utilization']); row['target'] = [cpu]
            return
    raise ValueError('Process cannot fit on one/two permitted CPUs without overriding existing masks: '+str(members[0]['pid']))


def build(folder, assessment, policy, network=None):
    context, topo, owners, leaders, threads, membership, rows, gaps = capture(Path(folder))
    caps={worker['id']:2*(len(worker['pids'])+sum(child['owner_pid'] in worker['pids'] for child in policy.get('children',[])))
          for worker in assessment['workers']}
    fixed=defaultdict(set)
    for worker in assessment['workers']:
        pids=set(worker['pids']) | {child['pid'] for child in policy.get('children',[]) if child['owner_pid'] in worker['pids']}
        for pid in pids:
            for tid in membership[pid]:
                row=rows.get(tid)
                if not row: continue
                allowed=set(row['original'] or []) & set(row['ceiling'] or []) & set(worker['eligible_cpus'])
                groups={tuple(topo['siblings'][str(cpu)]) for cpu in allowed if str(cpu) in topo['siblings']}
                if len(groups)==1: fixed[worker['id']].update(next(iter(groups)))
    proposal = recommend(assessment,worker_cpu_caps=caps,worker_required=fixed)
    require(proposal['status']=='PROPOSAL', '; '.join(proposal['issues']))
    require(policy.get('framework_binding') in ('absent','disabled'), 'Verify framework binding first')
    require(not set(policy)-{'framework_binding','customers','children','irqs','hot_fraction'}, 'Unknown policy key')
    for node in assessment['nodes']:
        for group in node['cores']:
            require(all(topo['cpu_node'].get(str(cpu))==node['node'] and topo['siblings'][str(cpu)]==sorted(group)
                        for cpu in group), 'Assessment contradicts captured NUMA/SMT')
    workers = {pid:worker for worker in proposal['workers'] for pid in worker['pids']}
    roots = set(workers)
    validate_pool_budget(proposal,topo,owners,roots,membership,rows)
    for pid,worker in workers.items():
        require(sorted(worker['npus']) == owners.get(pid), 'PID/NPU ownership mismatch: '+str(pid))
        require(all(set(worker['cpus']) <= set(topo['npu_cpus'][str(npu)]) for npu in worker['npus']), 'Worker outside NPU affinity')
    for item in policy.get('children',[]):
        pid, parent = item['pid'], item['owner_pid']
        require(item.get('reason') and parent in roots and pid not in workers and all(
            ancestor(pid,owners,table)==parent and table[pid].get('cgroup') and table[pid]['cgroup']==table[parent]['cgroup']
            for table in leaders), 'Unverified non-owner child: '+str(pid))
        workers[pid] = workers[parent]
    actions = []; weights = defaultdict(float); siblings = topo['siblings']
    def verified(tid,pid):
        row=rows.get(tid)
        require(row and pid>1 and row['pid']==pid and not row['kernel'] and row['cgroup'] and row['original'] and row['ceiling'],
                'Missing stable userspace identity, window, or cpuset for PID/TID '+str((pid,tid)))
        return dict(row)
    sizes = defaultdict(int)
    for worker in workers.values(): sizes[worker['id']] += 1
    ordered = sorted(workers, key=lambda pid:(-sum(rows[tid]['utilization'] for tid in membership[pid] if tid in rows),pid))
    for pid in ordered:
        worker = workers[pid]; tids = set(membership[pid]); old = {tid for tid,row in threads[0].items() if row['tgid']==pid}
        require(pid in tids and tids==old and all(table[pid]['threads']==len(tids) for table in leaders), 'Worker membership incomplete/changed: '+str(pid))
        members=[verified(tid,pid) for tid in sorted(tids)]
        basis=worker['demand_basis']; load=sum(row['utilization'] for row in members)
        peak = basis['cpu_equivalents_p95']; floor = basis['parallel_cores']
        require(load <= 2.01 and (sizes[worker['id']]>1 or (peak<=2 and floor<=2)),
                'Measured demand exceeds two CPUs for PID %d; do not silently throttle or widen' % pid)
        need = 2 if load > basis['target_utilization'] or (sizes[worker['id']]==1 and (peak>basis['target_utilization'] or floor>1)) else 1
        distribute(members,worker['cpus'],weights,siblings,need,2)
        for row in members: row.update(kind='business', worker=worker['id'], npus=worker['npus'])
        actions.extend(members)
    threshold=policy.get('hot_fraction',.20)
    require(type(threshold) in (int,float) and math.isfinite(threshold) and 0<threshold<=1,'Invalid hot_fraction')
    others=sorted([row for row in rows.values() if row['pid'] not in workers and row['utilization']>=threshold],
                  key=lambda row:(-row['utilization'],row['tid']))
    nodes=index(proposal['nodes'],'node'); seen=set()
    for item in policy.get('customers',[]):
        tid,pid=item['tid'],item['pid']; row=verified(tid,pid)
        require(item.get('reason') and tid not in seen and row['utilization']>=threshold and pid not in workers and
                all(complete_lineage(pid,table) and ancestor(pid,owners,table) is None and
                    not any(ancestor(owner,{pid},table)==pid for owner in owners) for table in leaders),
                'Not a reviewed independent hot customer TID: '+str(tid))
        repin=item.get('allow_repin',False); require(type(repin) is bool,'allow_repin must be boolean')
        pool=nodes[item['node']]['remaining_cpus']; distribute([row],pool,weights,siblings,1,1,repin)
        row.update(kind='customer',reason=item['reason'],allow_repin=repin); actions.append(row); seen.add(tid)
    observed=defaultdict(float)
    for action in actions: observed[tuple(siblings[str(action['target'][0])])]+=action['utilization']
    require(all(load<=1.01 for load in observed.values()),'Static placement overloads a physical core in the measured window; review constraints/peak or split workers')
    irq_actions,irq_review=plan_irqs(policy,network,context,topo,proposal)
    return dict(kind='customer_affinity_v1', workload=assessment['workload'], evidence=proposal['evidence'],
                framework_binding=policy['framework_binding'], context=context, topology=topo, ownership={str(k):v for k,v in owners.items()},
                nodes=proposal['nodes'], workers=proposal['workers'], members={str(pid):sorted(membership[pid]) for pid in workers},
                children=policy.get('children',[]), actions=actions, irq_actions=irq_actions, irq_review=irq_review,
                hot_others=others, unsampled_tids=gaps, hot_fraction=threshold)


def plan_irqs(policy, network, context, topo, proposal):
    requests=policy.get('irqs',[])
    if network is None:
        require(not requests and not proposal['nics'], 'NIC/IRQ planning requires a network evidence window')
        return [], {'status':'NOT_COLLECTED','note':'No IRQ isolation verified'}
    require(network['kind']=='network_evidence','Unexpected network evidence')
    before,after=network['before'],network['after']; first,last=before['raw'],after['raw']
    for raw in (first,last):
        require(raw['/proc/sys/kernel/random/boot_id'].strip()==context['boot_id'] and
                cpulist(raw['/sys/devices/system/cpu/online'])==topo['online'],'Network evidence boot/CPU mismatch')
    delta=interrupt_delta(first['/proc/interrupts'],last['/proc/interrupts'],after['monotonic']-before['monotonic'])
    candidates={int(row['id']):row for row in delta['movable_candidates']}; actions=[]; used=set()
    start,end=(index(network[key]['nics'],'name') for key in ('inventory_before','inventory_after'))
    pci_start,pci_end=(index(network[key].get('pci_irq_devices',[]),'bdf') for key in ('inventory_before','inventory_after'))
    planned={nic['name']:nic for nic in proposal['nics']}
    loads=defaultdict(float); nodes=index(proposal['nodes'],'node')
    for item in sorted(requests,key=lambda entry:-candidates.get(entry['irq'],{}).get('rate_per_sec',0)):
        irq=item['irq']; key=str(irq); name=item.get('nic'); bdf=item.get('pci_bdf'); row=candidates.get(irq)
        require(bool(name)!=bool(bdf) and type(irq) is int and item.get('reason') and irq not in used and row and
                sum(key in device['irqs'] for device in list(end.values())+list(pci_end.values()))==1,'No unique measured device IRQ: '+key)
        if name:
            device=end[name]; require(start.get(name)==device and name in planned and device['numa_node']==planned[name]['node'], 'NIC identity/NUMA mismatch')
            require(irq in {entry['irq'] for entry in planned[name]['irq_candidates']},'IRQ not in reviewed NIC budget')
            pool=nodes[device['numa_node']]['network_cpus']
        else:
            require(re.fullmatch(r'[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-7]', bdf), 'Invalid PCI BDF')
            device=pci_end[bdf]; require(pci_start.get(bdf)==device and device['numa_node'] in nodes,'PCI identity/NUMA mismatch')
            pool=nodes[device['numa_node']]['system_cpus']
        require(device['pci_device'] and (key in device.get('msi_irqs',[]) or ('msi_irqs' not in device and 'msi' in row['name'].lower())),
                'Only verified MSI/MSI-X vectors may move')
        choices=set(pool) & set(cpulist(device['local_cpulist'] or '')); require(choices,'No local IRQ CPU')
        def core(cpu): return tuple(topo['siblings'][str(cpu)])
        cpu=min(choices,key=lambda cpu:(loads[core(cpu)],cpu)); loads[core(cpu)]+=row['rate_per_sec']
        masks=device['irqs'][key]; used.add(irq)
        actions.append(dict(kind='irq',irq=irq,nic=name,pci_bdf=bdf,pci=device['pci_device'],node=device['numa_node'],name=row['name'],
                            original=cpulist(masks['smp_affinity_list'] or ''),effective=cpulist(masks['effective_affinity_list'] or ''),
                            target=[cpu],rate_per_sec=row['rate_per_sec'],reason=item['reason']))
    return actions,dict(status='MEASURED',interval_seconds=delta['interval_seconds'],issues=delta['issues'],
                        ranked=delta['movable_candidates'], note='Positive deltas ranked, not all hot or movable; unverified IRQs are review only.')


def render(plan, directory):
    directory=Path(directory); root=Path(__file__).resolve().parents[1]
    directory.mkdir(exist_ok=False)
    payload=json.dumps(plan,ensure_ascii=False,indent=2,allow_nan=False).encode('utf-8')
    (directory/'customer_plan.json').write_bytes(payload)
    entries=[dict(pid=[row['tid']],cpu_list=[cpu_list(row['target'])],bind_sub_process=False,mem_bind=False) for row in plan['actions']]
    entries.extend(dict(irq_id=[row['irq']],cpu_list=[cpu_list(row['target'])]) for row in plan['irq_actions'])
    (directory/'bind_design.review.json').write_text(json.dumps(dict(custom_bind=entries),indent=2),encoding='utf-8')
    with ZipFile(directory/'bind_customer.pyz','x',ZIP_DEFLATED) as archive:
        for filename in ('hostbind/__init__.py','hostbind/linux.py','hostbind/transaction.py','hostbind/planner.py',
                         'hostbind/network.py','hostbind/customer_runtime.py','analyzer/__init__.py','analyzer/npu_affinity.py',
                         'analyzer/npu_parser.py','analyzer/evidence.py','analyzer/process_evidence.py'):
            archive.write(root/filename, filename)
        archive.writestr('customer_plan.json',payload)
        archive.writestr('__main__.py', 'from hostbind.customer_runtime import main\nraise SystemExit(main())\n')
    script=(root/'templates/bind_customer.sh.in').read_text(encoding='utf-8').replace('@PLAN_ID@',digest(plan))
    (directory/'bind_customer.sh').write_bytes(script.encode('utf-8'))
