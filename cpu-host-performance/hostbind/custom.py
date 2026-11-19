"""Evidence-gated, offline CPU budgets. Proposals are NOT executor plans."""
import math


class Missing(ValueError):
    pass


def number(value, label, positive=False):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise Missing(label + ': finite measured number required')
    if value < 0 or (positive and value == 0):
        raise ValueError(label + ': invalid range')
    return value


def integer(value, label, minimum=0):
    number(value, label)
    if not isinstance(value, int) or value < minimum:
        raise ValueError(label + ': integer out of range')
    return value


def cpu_set(value):
    if not isinstance(value, list) or not value:
        raise Missing('Nonempty explicit CPU list required')
    for cpu in value:integer(cpu, 'CPU')
    if len(set(value)) != len(value):raise ValueError('Duplicate CPU')
    return set(value)


def recommend(data, worker_cpu_caps=None, worker_required=None):
    """Input schema and physical-core calibration: references/custom_binding.md."""
    result=dict(schema_version=1, kind='custom_binding_proposal', execution_supported=False,
                status='INSUFFICIENT_EVIDENCE', nodes=[], workers=[], nics=[], issues=[])
    try:
        if not isinstance(data,dict):raise ValueError('Assessment must be an object')
        if data.get('schema_version') != 1:raise Missing('Unsupported assessment schema')
        mode=data.get('workload')
        if mode not in ('inference','training','mixed'):raise Missing('Declare workload mode')
        evidence=data.get('evidence',{})
        if not isinstance(evidence,dict):raise ValueError('Evidence must be an object')
        for key in ('nodes','workers','nics'):
            if not isinstance(data.get(key),list) or any(not isinstance(v,dict) for v in data[key]):
                raise ValueError(key+': list of objects required')
        for key in ('identity_verified','topology_verified','load_representative'):
            if evidence.get(key) is not True:raise Missing('Unverified evidence: '+key)
        for key in ('source','window'):
            if not isinstance(evidence.get(key),str) or not evidence[key].strip():
                raise Missing('Evidence provenance required: '+key)
        result.update(workload=mode,evidence=evidence)
        net_target=number(data.get('network_target_utilization',.60),'network utilization',True)
        app_target=number(data.get('worker_target_utilization',.70),'worker utilization',True)
        if net_target>1 or app_target>1:raise ValueError('Utilization must be <= 1')
        nodes={};all_cpus=set()
        for node in data['nodes']:
            nid=integer(node['node'],'node')
            if nid in nodes:raise ValueError('Duplicate NUMA node')
            groups=[]
            for values in node['cores']:
                group=cpu_set(values)
                if group & all_cpus:raise ValueError('Overlapping physical cores')
                all_cpus |= group;groups.append(group)
            if not groups:raise Missing('No complete available physical cores')
            if node.get('protected_cpus') is None:
                raise Missing('Explicit protected_cpus required; [] means verified no reservation')
            if not isinstance(node['protected_cpus'],list):
                raise ValueError('protected_cpus must be an explicit list')
            protected=cpu_set(node['protected_cpus']) if node['protected_cpus'] else set()
            if not protected<=set().union(*groups):raise ValueError('Protected CPUs outside node')
            reserved=[g for g in groups if g & protected]
            free=[g for g in groups if not g & protected]
            system=integer(node['system_cores'],'system cores',1)
            load=number(node['network_core_equivalents_p95'],'network load')
            nodes[nid]=dict(groups=groups,free=free,protected=reserved,system_count=system,
                            network_count=math.ceil(load/net_target),network_load=load,workers=[])
        if not nodes:raise Missing('No nodes')
        names=set();irqs=set()
        for nic in data.get('nics',[]):
            if not isinstance(nic.get('name'),str) or not nic['name'] or nic['name'] in names:
                raise ValueError('NIC name missing/duplicate')
            names.add(nic['name'])
            integer(nic.get('node'),'NIC node')
            if nic.get('node') not in nodes:raise Missing('Unknown NIC NUMA: '+nic['name'])
            if not isinstance(nic.get('irqs'),list) or not nic['irqs']:
                raise Missing('Verified NIC IRQ mapping required')
            for irq in nic['irqs']:
                integer(irq,'IRQ')
                if irq in irqs:raise ValueError('IRQ shared across NIC records; consolidate physical device')
                irqs.add(irq)
            nodes[nic['node']]['network_count']=max(1,nodes[nic['node']]['network_count'])
        for nid,node in nodes.items():
            if node['network_load']>0 and not any(n['node']==nid for n in data['nics']):
                raise Missing('Network load without verified NIC mapping on node %s'%nid)
        ids=set();pids=set();device_owner={}
        for worker in data['workers']:
            wid=worker['id']
            if not isinstance(wid,str) or not wid or wid in ids:raise ValueError('Worker ID missing/duplicate')
            ids.add(wid)
            nid=worker['node']
            integer(nid,'worker node')
            if nid not in nodes:raise Missing('Worker node unknown')
            if mode=='mixed' and worker.get('workload') not in ('inference','training'):
                raise Missing('Mixed workloads require worker workload labels')
            if not isinstance(worker.get('pids'),list) or not worker['pids']:
                raise Missing('Verified owner PIDs required')
            for pid in worker['pids']:
                integer(pid,'PID',1)
                if pid in pids:raise ValueError('PID cannot be split between worker pools')
                pids.add(pid)
            npus=worker.get('npus')
            if not isinstance(npus,list) or not npus:raise Missing('Verified NPU ownership required')
            if len(npus)>1 and worker.get('shared_device_pool_verified') is not True:
                raise Missing('Multi-device worker needs verified common pool; cannot infer TID ownership')
            for npu in npus:
                integer(npu,'NPU')
                if npu in device_owner:raise ValueError('Consolidate PIDs of the same NPU into one worker pool')
                device_owner[npu]=wid
            eligible=cpu_set(worker['eligible_cpus'])
            local=set().union(*nodes[nid]['groups'])
            if not eligible<=local:raise ValueError('Worker eligibility crosses declared NUMA/available CPUs')
            load=number(worker['cpu_equivalents_p95'],'worker load')
            parallel=integer(worker['parallel_cores'],'validated parallel core floor',1)
            demand=max(parallel,math.ceil(load/app_target))
            if worker_cpu_caps is not None:
                cap=integer(worker_cpu_caps[wid],'process CPU budget',1)
                if parallel>cap or load>cap:
                    raise ValueError('Worker %s measured demand exceeds the one/two-CPU process limit'%wid)
                demand=min(demand,cap)
            choices=[g for g in nodes[nid]['free'] if g<=eligible]
            mandatory=(worker_required or {}).get(wid,set())
            fixed=[group for group in choices if group & mandatory]
            if not mandatory<=set().union(*fixed):raise ValueError('Required thread core is unavailable: '+wid)
            demand=max(demand,len(fixed))
            if worker_cpu_caps and demand>worker_cpu_caps[wid]:raise ValueError('Existing narrow masks exceed process CPU budget: '+wid)
            nodes[nid]['workers'].append(dict(raw=worker,demand=demand,choices=choices,fixed=fixed))
        if not ids:raise Missing('No workers')
        proposed_nodes=[];proposed_workers=[]
        for nid,node in sorted(nodes.items()):
            # Constrained workers first; network/system receive the flexible local remainder.
            # Deterministic greedy placement is a candidate, not an optimal matching solver.
            free=list(node['free'])
            for w in sorted(node['workers'],key=lambda w:(len(w['choices'])-w['demand'],w['raw']['id'])):
                others=[group for other in node['workers'] if other is not w for group in other['fixed']]
                if any(g in others or g not in free for g in w['fixed']):raise ValueError('Conflicting fixed worker core: '+w['raw']['id'])
                choices=w['fixed']+[g for g in free if g in w['choices'] and g not in others and g not in w['fixed']]
                if len(choices)<w['demand']:
                    raise ValueError('Node %s worker %s: insufficient eligible whole cores; revise placement or capacity' % (nid,w['raw']['id']))
                chosen=choices[:w['demand']];free=[g for g in free if g not in chosen]
                proposed_workers.append(dict(id=w['raw']['id'],node=nid,npus=w['raw']['npus'],
                    pids=w['raw']['pids'],physical_cores=len(chosen),cpus=sorted(set().union(*chosen)),
                    demand_basis=dict(cpu_equivalents_p95=w['raw']['cpu_equivalents_p95'],
                                      parallel_cores=w['raw']['parallel_cores'],target_utilization=app_target)))
            count=node['network_count']+node['system_count']
            if len(free)<count:
                raise ValueError('Node %s short by %s physical cores after customer/worker reservations' % (nid,count-len(free)))
            net=free[:node['network_count']];system=free[node['network_count']:count]
            flatten=lambda groups:sorted(set().union(*groups)) if groups else []
            proposed_nodes.append(dict(node=nid,available_physical_cores=len(node['groups']),
                protected_cpus=flatten(node['protected']),system_cpus=flatten(system),network_cpus=flatten(net),
                remaining_cpus=flatten(free[count:]),network_physical_cores=len(net),
                network_percent=round(100*len(net)/len(node['groups']),2),
                network_core_equivalents_p95=node['network_load'],network_target_utilization=net_target))
        planned={n['node']:n for n in proposed_nodes}
        irq_offsets={nid:0 for nid in nodes}
        for nic in data.get('nics',[]):
            groups=[g for g in nodes[nic['node']]['groups'] if g<=set(planned[nic['node']]['network_cpus'])]
            offset=irq_offsets[nic['node']]
            result['nics'].append(dict(name=nic['name'],node=nic['node'],
                irq_candidates=[dict(irq=irq,cpu=min(groups[(offset+i)%len(groups)])) for i,irq in enumerate(nic['irqs'])],
                note='Round-robin review candidate only; weight actual queues, check managed IRQ/effective affinity/RPS/RFS and irqbalance.'))
            irq_offsets[nic['node']]+=len(nic['irqs'])
        result.update(status='PROPOSAL',nodes=proposed_nodes,workers=proposed_workers,
            warnings=['No execution plan or isolation guarantee. Existing locality apply does not consume this proposal.',
                      'Complete core groups are reservations, not a promise of SMT throughput.',
                      'Remaining CPUs are a capacity pool, not authorization to move unrelated processes.'])
    except Missing as exc:
        result.update(status='INSUFFICIENT_EVIDENCE',issues=[str(exc)],nodes=[],workers=[],nics=[])
    except (ValueError,KeyError,TypeError,OverflowError) as exc:
        result.update(status='BLOCKED',issues=[str(exc)],nodes=[],workers=[],nics=[])
    return result
