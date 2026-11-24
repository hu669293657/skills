import json
import tempfile
import unittest
from pathlib import Path

from hostbind.customer import build, inspect, render


def fixture(folder):
    cpus = list(range(16))
    def write(name, value):
        (folder / name).write_text(value if isinstance(value, str) else json.dumps(value), encoding='utf8')
    write('host_context.txt', 'execution_context=host\nboot_id=b1\npid_namespace=pid:[1]\ninit_pid_namespace=pid:[1]\n')
    write('cpu_online.txt', '0-15')
    write('lscpu_parse.txt', ''.join('%d,%d,0,0,Y\n' % (cpu, cpu % 8) for cpu in cpus))
    write('npu_smi_map.txt', '| NPU ID | Chip ID | Chip Logic ID |\n| 3 | 0 | 5 |')
    write('npu_smi_topology.txt', 'NPU CPU Affinity\nNPU5 X 0-15')
    for filename in ('npu_smi_info_start.txt', 'npu_smi_info.txt'):
        write(filename, '| 3 0 | 20 | python3 | 123 |')
    for end, phase in enumerate(('start', 'end')):
        rows = [dict(tgid=pid, tid=tid, starttime=tid*10, ppid=1, name=name,
                     allowed_cpus=cpus, cpuset_cpus_effective=cpus, cgroup_source='task',
                     cgroup='0::/job%s' % pid, flags=0, identity_started_at=10*end+1,
                     utime_ticks=ticks*end, stime_ticks=0)
                for pid, tid, name, ticks in [(20,20,'vllm',800),(20,21,'mystery',700),
                    (20,22,'RT_RECYCLE',5),(40,40,'python3',800),(50,50,'dev_worker',800)]]
        leaders = [dict(row, pid=row['tid'], threads=sum(t['tgid']==row['tid'] for t in rows),
                        kernel_thread=row['tid']==50) for row in rows if row['tgid']==row['tid']]
        write('process_'+phase+'.json', dict(schema_version=2, clock_ticks=100, clock='boottime',
              ts=10*end+1, end_ts=10*end+2, processes=leaders, threads=rows, errors=[]))
    return dict(schema_version=1, workload='inference', evidence=dict(source='test',window='steady',
        identity_verified=True, topology_verified=True, load_representative=True),
        nodes=[dict(node=0, cores=[[cpu,cpu+8] for cpu in range(8)], protected_cpus=[],
                    system_cores=1, network_core_equivalents_p95=0)], nics=[],
        workers=[dict(id='card5',node=0,npus=[5],pids=[20],eligible_cpus=cpus,
                      cpu_equivalents_p95=1.5, parallel_cores=2)])


class CustomerScriptTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.folder = Path(temp.name)
        self.assessment = fixture(self.folder)
        self.policy = dict(framework_binding='absent', customers=[], children=[], irqs=[])

    def plan(self):
        return build(self.folder, self.assessment, self.policy)

    def test_default_hot_review_and_process_cap(self):
        actions = self.plan()['actions']
        self.assertEqual({row['tid'] for row in actions}, {20,21,22})
        self.assertEqual(len({cpu for row in actions for cpu in row['target']}), 2)
        self.assertNotEqual(actions[0]['target'], actions[1]['target'])
        self.assertIn(40, [row['tid'] for row in self.plan()['hot_others']])

    def test_customer_is_opt_in_and_on_separate_physical_core(self):
        self.policy['customers'] = [dict(pid=40,tid=40,node=0,reason='confirmed external service')]
        rows = {row['tid']:row for row in self.plan()['actions']}
        self.assertNotIn(rows[40]['target'][0] % 8, {rows[tid]['target'][0] % 8 for tid in (20,21,22)})
        self.policy['customers'][0].update(pid=50,tid=50)
        with self.assertRaises(ValueError): self.plan()

    def test_reused_identity_blocks_export(self):
        path = self.folder / 'process_end.json'
        data = json.loads(path.read_text()); data['threads'][1]['starttime'] += 1
        path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.plan()

    def test_customer_pinned_on_ai_core_needs_explicit_repin(self):
        for phase in ('start','end'):
            path=self.folder/('process_'+phase+'.json'); data=json.loads(path.read_text())
            next(row for row in data['threads'] if row['tid']==40)['allowed_cpus']=[0]
            path.write_text(json.dumps(data))
        entry=dict(pid=40,tid=40,node=0,reason='confirmed customer move from AI core')
        self.policy['customers']=[entry]
        with self.assertRaises(ValueError): self.plan()
        entry['allow_repin']=True
        row=next(row for row in self.plan()['actions'] if row['tid']==40)
        self.assertEqual(row['original'],[0]); self.assertNotIn(row['target'][0],self.plan()['workers'][0]['cpus'])

    def test_target_collector_error_is_not_zero_load(self):
        path=self.folder/'process_end.json'; data=json.loads(path.read_text())
        data['errors']=[dict(id='21',reason='stat_unreadable_or_exited')]; path.write_text(json.dumps(data))
        with self.assertRaises(ValueError): self.plan()

    def test_unknown_card_cannot_be_relabelled(self):
        self.assessment['workers'][0]['npus'] = [3]
        with self.assertRaises(ValueError): self.plan()

    def test_card_mapping_uses_header_not_column_position(self):
        (self.folder/'npu_smi_map.txt').write_text('| NPU ID | Chip ID | Chip Phy ID | Chip Logic ID |\n| 3 | 0 | 6 | 5 |')
        self.assertEqual(self.plan()['ownership'],{'20':[5]})

    def test_two_numa_sparse_two_cards(self):
        other=list(range(24,32)); cpus=list(range(16))+other
        (self.folder/'cpu_online.txt').write_text('0-15,24-31')
        with (self.folder/'lscpu_parse.txt').open('a') as stream:
            for cpu in other: stream.write('%s,%s,1,1,Y\n'%(cpu,cpu))
        with (self.folder/'npu_smi_map.txt').open('a') as stream: stream.write('\n| 9 | 0 | 8 |')
        with (self.folder/'npu_smi_topology.txt').open('a') as stream: stream.write('\nNPU8 X 24-31')
        for name in ('npu_smi_info.txt','npu_smi_info_start.txt'):
            with (self.folder/name).open('a') as stream: stream.write('\n| 9 0 | 40 | python3 | 555 |')
        for phase in ('start','end'):
            path=self.folder/('process_'+phase+'.json'); data=json.loads(path.read_text())
            for thread in data['threads']:
                thread.update(allowed_cpus=cpus,cpuset_cpus_effective=cpus)
            path.write_text(json.dumps(data))
        self.assessment['nodes'].append(dict(node=1,cores=[[cpu] for cpu in other],protected_cpus=[],system_cores=1,network_core_equivalents_p95=0))
        self.assessment['workers'].append(dict(id='card8',node=1,npus=[8],pids=[40],eligible_cpus=other,cpu_equivalents_p95=.8,parallel_cores=1))
        plan=self.plan(); self.assertEqual(next(row for row in plan['actions'] if row['tid']==40)['target'],[24])
        self.assessment['workers'][1]['node']=0; self.assessment['workers'][1]['eligible_cpus']=list(range(16))
        with self.assertRaises(ValueError): self.plan()

    def test_descendant_needs_opt_in_and_matching_cgroup(self):
        self.policy['children']=[dict(pid=40,owner_pid=20,reason='confirmed tokenizer')]
        with self.assertRaises(ValueError): self.plan()
        for phase in ('start','end'):
            path=self.folder/('process_'+phase+'.json'); data=json.loads(path.read_text())
            for row in data['processes']+data['threads']:
                if row['tgid']==40: row.update(ppid=20,cgroup='0::/job20')
            path.write_text(json.dumps(data))
        row=next(row for row in self.plan()['actions'] if row['tid']==40)
        self.assertEqual(row['worker'],'card5')

    def test_budget_does_not_reserve_unused_third_core_for_one_pid(self):
        self.assertEqual(self.plan()['workers'][0]['physical_cores'],2)
        self.assessment['workers'][0]['parallel_cores']=3
        with self.assertRaises(ValueError): self.plan()

    def test_pin_constraints_influence_pool_before_assignment(self):
        for phase in ('start','end'):
            path=self.folder/('process_'+phase+'.json'); data=json.loads(path.read_text())
            data['threads'][1]['allowed_cpus']=[13]
            path.write_text(json.dumps(data))
        row=next(row for row in self.plan()['actions'] if row['tid']==21)
        self.assertEqual(row['target'],[13])

    def test_existing_light_sibling_pins_can_share_one_physical_core(self):
        for phase in ('start','end'):
            path=self.folder/('process_'+phase+'.json'); data=json.loads(path.read_text())
            for row in data['threads']:
                if row['tgid']==20:
                    row['allowed_cpus']=[8 if row['tid']==21 else 0]
                    row['utime_ticks']=100 if phase=='end' else 0
            path.write_text(json.dumps(data))
        self.assessment['workers'][0].update(parallel_cores=1,cpu_equivalents_p95=.3)
        plan=self.plan(); self.assertEqual(plan['workers'][0]['physical_cores'],1)
        self.assertEqual({row['target'][0] for row in plan['actions']},{0,8})

    def test_portable_artifact_executes_reviewed_thread_targets(self):
        import importlib.util
        from unittest.mock import patch
        from hostbind.delivery import TEMPLATE
        plan = self.plan()
        bundle = self.folder / 'deliver'
        render(plan, bundle)
        executor = bundle / 'cpu_binder_simple.py'
        self.assertEqual(executor.read_bytes(), TEMPLATE.read_bytes())
        self.assertEqual({p.name for p in bundle.iterdir()},
                         {'customer_plan.json', 'cpu_binder_simple.py', 'bind_design.json'})
        spec = importlib.util.spec_from_file_location('exported_binder', executor)
        binder = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(binder)
        design = json.loads((bundle / 'bind_design.json').read_bytes())
        with patch.object(binder, 'execute_command', return_value=('', 0)) as command:
            for item in binder.load_custom_bind(design):
                item.bind('', {})
        expected = [(['taskset', '-cp', ','.join(map(str, row['target'])), str(row['tid'])],)
                    for row in plan['actions']]
        self.assertEqual([call.args for call in command.call_args_list], expected)

    def test_inspect_separates_device_owner_unknown_thread_and_customer(self):
        report=inspect(self.folder)
        self.assertEqual([row['tid'] for row in report['owners'][0]['threads']], [20,21,22])
        self.assertEqual(report['owners'][0]['npus'],[5])
        self.assertEqual({row['tid'] for row in report['hot_others']},{40,50})

    def test_msi_irq_locality_and_rate(self):
        self.assessment['nics']=[dict(name='eth0',node=0,irqs=[90])]
        self.policy['irqs']=[dict(nic='eth0',irq=90,reason='repeated hot RX queue')]
        irq_masks={'smp_affinity_list':'0-15','effective_affinity_list':'4'}
        device=dict(name='eth0',pci_device='/sys/devices/pci0000:00/0000:00:01.0',node=0,numa_node=0,
                    irqs={'90':irq_masks},msi_irqs=['90'],local_cpulist='0-15')
        def snap(seconds):
            return dict(monotonic=seconds,raw={'/proc/sys/kernel/random/boot_id':'b1','/sys/devices/system/cpu/online':'0-15',
                '/proc/interrupts':' CPU0 CPU8\n90: %d 500 PCI-MSI eth0-rx\nIPI0: 100 200 ipi\n4: 400 900 GIC arch_timer\n'%(seconds*100)})
        network=dict(kind='network_evidence',before=snap(1),after=snap(6),inventory_before={'nics':[device]},inventory_after={'nics':[device]})
        plan=build(self.folder,self.assessment,self.policy,network)
        irq=plan['irq_actions'][0]; self.assertEqual(irq['rate_per_sec'],100);self.assertIn(irq['target'][0],plan['nodes'][0]['network_cpus'])
        device['numa_node']=-1
        with self.assertRaises(ValueError): build(self.folder,self.assessment,self.policy,network)
        device['numa_node']=0; self.assessment['nics']=[]
        for stage in ('inventory_before','inventory_after'):
            network[stage]={'nics':[], 'pci_irq_devices':[dict(device,bdf='0000:00:01.0')]}
        self.policy['irqs']=[dict(pci_bdf='0000:00:01.0',irq=90,reason='confirmed frequently firing storage queue')]
        plan=build(self.folder,self.assessment,self.policy,network)
        self.assertIn(plan['irq_actions'][0]['target'][0],plan['nodes'][0]['system_cpus'])
        bundle = self.folder / 'irq_output'
        render(plan, bundle)
        irq_entries = [entry for entry in json.loads((bundle / 'bind_design.json').read_bytes())['custom_bind']
                       if 'irq_id' in entry]
        self.assertEqual(irq_entries, [dict(irq_id=[90], cpu_list=[str(plan['irq_actions'][0]['target'][0])])])
