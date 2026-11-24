import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from analyzer.binding_plan import build_binding_plan
from analyzer.npu_affinity import analyze_npu_affinity
from analyzer.product_identity import detect_product
from hostbind.customer import build
from hostbind.placement import A3_POLICY, EXPLICIT
from tests.test_customer_script import fixture


def collected(ownership, masks=None, topology='', product='Atlas 900 A3 SuperPoD'):
    cpus = [4, 5, 10, 11]
    snap = {'dmi_product_name.txt': product, 'host_context.txt': 'execution_context=host\nboot_id=test',
            'numactl.txt': 'node 10 cpus: 10 11\nnode 2 cpus: 4 5\nnode 0 cpus: 0\nnode 7 cpus:',
            'cpu_online.txt': '4-5,10-11', 'npu_smi_topology.txt': topology,
            'npu_smi_info.txt': '\n'.join('| %d 0 | %d | python | 10 |' % pair for pair in ownership)}
    owners = sorted({pid for _, pid in ownership})
    batch = dict(processes=[dict(tgid=p, threads=1, name='python', starttime=20*p, ppid=1,
                                 cpuset_cpus_effective=cpus, namespace_pids=[p]) for p in owners],
                 threads=[dict(tid=p,tgid=p,name='python',starttime=20*p,allowed_cpus=cpus,
                               namespace_pids=[p], cpuset_cpus_effective=(masks or {}).get(p,cpus)) for p in owners])
    for phase in ('start','end'):
        snap['process_'+phase+'.json'] = json.dumps(batch)
    return snap


def report(snap):
    metrics = dict(snapshot=snap, product_identity=detect_product(snap))
    metrics['npu_affinity'] = analyze_npu_affinity(metrics)
    return build_binding_plan(metrics), metrics['npu_affinity']['rows']


class A3ReportPlacementTests(unittest.TestCase):
    def test_numeric_sparse_order_only_active_devices_and_shared_owners(self):
        snap = collected([(21,101),(10,110),(2,120),(2,121)], topology='NPU CPU Affinity\nNPU1 X N/A')
        plan, rows = report(snap)
        self.assertEqual({a['pid']:a['target'] for a in plan['actions']},
                         {120:'4-5',121:'4-5',110:'10-11',101:'4-5'})
        self.assertTrue(all(a['placement_source'] == A3_POLICY for a in plan['actions']))
        self.assertTrue(all(not r['affinity'] and not r['numa'] for r in rows))

    def test_sysfs_only_single_smt_sibling_allowed_and_explicit_precedence(self):
        snap = collected([(10,110),(2,120)], {110:[5]}, 'NPU CPU Affinity\nNPU2 X 10-11')
        snap.pop('numactl.txt')
        snap.update({'sysfs/node/node2/cpulist':'4-5', 'sysfs/node/node10/cpulist':'10-11'})
        plan, _ = report(snap)
        actions = {a['pid']:(a['target'],a['placement_source']) for a in plan['actions']}
        self.assertEqual(actions, {110:('5', A3_POLICY), 120:('10-11', EXPLICIT)})

    def test_conflicting_or_malformed_mapping_blocks_policy(self):
        for extra in ('9-3','unreadable','10-11'):
            snap = collected([(2,120)], topology='NPU CPU Affinity\nNPU2 X 4-5')
            snap['npu_smi_map.txt'] = '| NPU ID | CPU Affinity |\n| 2 | %s |' % extra
            self.assertFalse(report(snap)[0]['actions'])

    def test_unknown_other_products_and_multidevice_pid(self):
        for name in ('Atlas 800 A2', 'Atlas A5', 'generic server'):
            self.assertFalse(report(collected([(2,120)], product=name))[0]['actions'])
        self.assertFalse(report(collected([(2,120),(10,120)]))[0]['actions'])

    def test_changed_device_owner_blocks_only_affected_targets(self):
        snap = collected([(2,120),(10,110)])
        snap['npu_smi_info_start.txt'] = '| 7 0 | 120 | python | 10 |\n| 10 0 | 110 | python | 10 |'
        plan, _ = report(snap)
        self.assertEqual([a['pid'] for a in plan['actions']], [110])

    def test_unknown_coowner_and_disjoint_coowner_cpusets(self):
        for masks in ({120:[5],121:[11]}, {120:[],121:[11]}):
            self.assertFalse(report(collected([(2,120),(2,121)], masks))[0]['actions'])

    def test_alias_and_mixed_explicit_rows_use_logic_device(self):
        snap = collected([(2,120),(10,110)], topology='NPU CPU Affinity\nNPU1 X 4-5\nNPU3 X N/A')
        snap['npu_smi_map.txt'] = '| NPU ID | Chip ID | Chip Logic ID |\n| 10 | 0 | 1 |\n| 2 | 0 | 3 |'
        plan, _ = report(snap)
        self.assertEqual({a['pid']:(a['npu_ids'],a['target']) for a in plan['actions']},
                         {110:([1],'4-5'), 120:([3],'10-11')})


class A3CustomerPlacementTests(unittest.TestCase):
    def setup_capture(self, folder):
        assessment = fixture(folder)
        (folder/'dmi_product_name.txt').write_text('Atlas 900 A3 SuperPoD')
        (folder/'npu_smi_map.txt').write_text('| NPU ID | Chip ID | Chip Logic ID |\n| 9 | 0 | 2 |\n| 3 | 0 | 10 |')
        (folder/'npu_smi_topology.txt').unlink()
        for name in ('npu_smi_info_start.txt', 'npu_smi_info.txt'):
            with (folder/name).open('a') as stream:
                stream.write('\n| 9 0 | 40 | python | 55 |')
        other = list(range(24,32))
        (folder/'cpu_online.txt').write_text('0-15,24-31')
        with (folder/'lscpu_parse.txt').open('a') as stream:
            for cpu in other:
                stream.write('%d,%d,1,8,Y\n' % (cpu,cpu))
        for phase in ('start','end'):
            path = folder/('process_'+phase+'.json')
            batch = json.loads(path.read_text())
            for row in batch['threads']:
                row.update(allowed_cpus=list(range(16))+other,cpuset_cpus_effective=list(range(16))+other)
            path.write_text(json.dumps(batch))
        assessment['nodes'].append(dict(node=8,cores=[[cpu] for cpu in other],protected_cpus=[],
                                        system_cores=1,network_core_equivalents_p95=0))
        first = assessment['workers'][0]
        first.update(npus=[10])
        del first['eligible_cpus'], first['node']
        assessment['workers'].append(dict(id='rank2',npus=[2],pids=[40],cpu_equivalents_p95=.8,parallel_cores=1))
        return assessment

    def test_numeric_assignment_and_reviewed_budget(self):
        with TemporaryDirectory() as tmp:
            folder = Path(tmp)
            assessment = self.setup_capture(folder)
            policy = dict(framework_binding='absent',customers=[],children=[],irqs=[])
            plan = build(folder,assessment,policy)
            self.assertEqual({w['npus'][0]:w['node'] for w in plan['workers']}, {2:0,10:8})
            self.assertTrue(all(w['placement_source']==A3_POLICY for w in plan['workers']))
            self.assertEqual(plan['topology']['npu_cpus'], {'2':[], '10':[]})
            assessment['nodes'][0]['protected_cpus'] = list(range(8))
            assessment['nodes'][0]['protected_cpus'].pop()  # Last core is reserved for the system.
            self.assertEqual({w['node'] for w in build(folder,assessment,policy)['workers']}, {8})
            assessment['workers'][0]['eligible_cpus'] = [0,8]
            with self.assertRaisesRegex(ValueError, 'eligible A3'):
                build(folder,assessment,policy)
