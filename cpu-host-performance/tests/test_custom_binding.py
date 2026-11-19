"""Offline proposal invariants; these tests never change host affinity."""
import copy
import unittest
import tempfile
from pathlib import Path
from hostbind.custom import recommend
from hostbind.network import interrupt_delta, capture_inventory


def assessment():
    return dict(schema_version=1, workload='inference', evidence=dict(
        identity_verified=True, topology_verified=True, load_representative=True,
        source='synthetic fixture', window='three steady windows'),
        nodes=[dict(node=0, cores=[[i, i+16] for i in range(16)],
                    protected_cpus=[0,16], system_cores=1,
                    network_core_equivalents_p95=1.2)],
        nics=[dict(name='eth0', node=0, irqs=[90,91])],
        workers=[dict(id='npu0-job1', node=0, npus=[0], pids=[100],
                      eligible_cpus=list(range(32)), cpu_equivalents_p95=1.5,
                      parallel_cores=2, role='engine')])


class CustomPlanningTests(unittest.TestCase):
    def test_whole_smt_groups_and_disjoint_capacity(self):
        p=recommend(assessment())
        self.assertEqual(p['status'],'PROPOSAL')
        n=p['nodes'][0]
        self.assertEqual(n['network_physical_cores'],2)
        self.assertEqual(n['network_percent'],12.5)
        pools=[n['protected_cpus'],n['system_cpus'],n['network_cpus'],
               n['remaining_cpus'],p['workers'][0]['cpus']]
        flat=[c for pool in pools for c in pool]
        self.assertEqual(sorted(flat),list(range(32)))
        for pool in pools:
            self.assertTrue(all((c+16 if c<16 else c-16) in pool for c in pool))
        self.assertEqual(p['execution_supported'],False)

    def test_overcommit_has_no_partial_actions(self):
        a=assessment();a['workers'][0]['cpu_equivalents_p95']=20
        p=recommend(a)
        self.assertEqual(p['status'],'BLOCKED')
        self.assertFalse(p['workers']);self.assertFalse(p['nodes'])

    def test_no_load_does_not_invent_percentage(self):
        a=assessment();a['nodes'][0]['network_core_equivalents_p95']=None
        self.assertEqual(recommend(a)['status'],'INSUFFICIENT_EVIDENCE')

    def test_worker_eligibility_restricts_network_pool(self):
        a=assessment();a['workers'][0]['eligible_cpus']=[1,2,3,17,18,19]
        p=recommend(a)
        self.assertEqual(p['status'],'PROPOSAL')
        self.assertTrue(set(p['workers'][0]['cpus']) <= {1,2,3,17,18,19})

    def test_invalid_inputs_are_not_proposals(self):
        for change in ('nan','duplicate_core','duplicate_pid','unknown_nic','split_smt','multi_card'):
            with self.subTest(change=change):
                a=assessment()
                if change=='nan':a['nodes'][0]['network_core_equivalents_p95']=float('nan')
                if change=='duplicate_core':a['nodes'][0]['cores'].append([0,16])
                if change=='duplicate_pid':a['workers'].append(copy.deepcopy(a['workers'][0]))
                if change=='unknown_nic':a['nics'][0]['node']=-1
                if change=='split_smt':a['workers'][0]['eligible_cpus']=[1,2,3]
                if change=='multi_card':a['workers'][0]['npus']=[0,1]
                self.assertNotEqual(recommend(a)['status'],'PROPOSAL')

    def test_training_is_explicit_and_demand_driven(self):
        a=assessment();a['workload']='training'
        a['workers'][0]['role']='rank+loader';a['workers'][0]['parallel_cores']=5
        self.assertEqual(recommend(a)['workers'][0]['physical_cores'],5)
        a['workload']='guess'
        self.assertEqual(recommend(a)['status'],'INSUFFICIENT_EVIDENCE')

    def test_network_more_than_twenty_percent_is_not_capped(self):
        a=assessment();a['nodes'][0]['network_core_equivalents_p95']=3
        self.assertEqual(recommend(a)['nodes'][0]['network_percent'],31.25)

    def test_malformed_schema_returns_blocked_instead_of_crashing(self):
        for value in ([], None, {'schema_version':1,'workload':'inference','evidence':[]}):
            self.assertEqual(recommend(value)['status'],'BLOCKED')

    def test_network_demand_requires_device_mapping(self):
        a=assessment();a['nics']=[]
        self.assertEqual(recommend(a)['status'],'INSUFFICIENT_EVIDENCE')

    def test_two_nodes_use_nic_local_cpus(self):
        a=assessment();a['nodes'].append(dict(node=2,cores=[[i] for i in range(40,48)],
            protected_cpus=[],system_cores=1,network_core_equivalents_p95=.6))
        a['nics'].append(dict(name='eth1',node=2,irqs=[200]))
        p=recommend(a)
        self.assertEqual(p['status'],'PROPOSAL')
        self.assertIn(p['nics'][1]['irq_candidates'][0]['cpu'],range(40,48))

    def test_unknown_customer_reservation_is_not_empty(self):
        for value in (None,False,0,''):
            a=assessment();a['nodes'][0]['protected_cpus']=value
            self.assertNotEqual(recommend(a)['status'],'PROPOSAL')
        a=assessment();del a['nodes'][0]['protected_cpus']
        self.assertNotEqual(recommend(a)['status'],'PROPOSAL')

    def test_multiple_nics_share_node_round_robin(self):
        a=assessment();a['nodes'][0]['network_core_equivalents_p95']=2.4
        a['nics'].append(dict(name='eth1',node=0,irqs=[92,93]))
        p=recommend(a)
        cpus=[q['cpu'] for nic in p['nics'] for q in nic['irq_candidates']]
        self.assertEqual(len(set(cpus)),4)


class InterruptDeltaTests(unittest.TestCase):
    def test_broken_per_cpu_row_is_reported_but_global_errors_are_allowed(self):
        bad=' CPU0 CPU8\n90: 5 BAD PCI eth0\n'
        self.assertTrue(interrupt_delta(bad,bad,1)['issues'])
        good=' CPU0 CPU8\n90: 5 6 PCI eth0\nERR: 0\nMIS: 0\n'
        self.assertFalse(interrupt_delta(good,good,1)['issues'])

    def test_nic_inventory_keeps_numa_unknown_and_records_effective_mask(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp);sys=root/'sys';proc=root/'proc'
            files={'class/net/eth0/device/numa_node':'-1',
                   'class/net/eth0/device/local_cpulist':'0-3',
                   'class/net/eth0/device/msi_irqs/90':'msix',
                   'class/net/eth0/queues/rx-0/rps_cpus':'0',
                   'class/net/eth0/ifindex':'2'}
            for name,value in files.items():
                path=sys/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
            for name,value in {'smp_affinity_list':'0-3','effective_affinity_list':'2'}.items():
                path=proc/'irq/90'/name;path.parent.mkdir(parents=True,exist_ok=True);path.write_text(value)
            inv=capture_inventory(proc,sys)
            self.assertEqual(inv['nics'][0]['numa_node'],-1)
            self.assertEqual(inv['nics'][0]['irqs']['90']['effective_affinity_list'],'2')
            self.assertEqual(inv['nics'][0]['queues']['rx-0']['rps_cpus'],'0')

    def test_sparse_cpu_header_and_ignored_local_interrupts(self):
        before=' CPU0 CPU8\n90: 10 20 PCI-MSI eth0-TxRx-0\nIPI0: 1 2 rescheduling\n2: 20 30 GIC arch_timer\n'
        after=' CPU0 CPU8\n90: 20 50 PCI-MSI eth0-TxRx-0\nIPI0: 11 22 rescheduling\n2: 30 60 GIC arch_timer\n'
        d=interrupt_delta(before,after,2)
        self.assertEqual(d['rows'][0]['per_cpu'],{'0':5.0,'8':15.0})
        self.assertEqual(d['rows'][0]['rate_per_sec'],20)
        self.assertEqual([x['id'] for x in d['movable_candidates']],['90'])

    def test_reset_hotplug_and_irq_reuse_are_not_rates(self):
        before=' CPU0\n90: 10 PCI eth0\n'
        for after in (' CPU0\n90: 1 PCI eth0\n',
                      ' CPU0 CPU1\n90: 20 30 PCI eth0\n',
                      ' CPU0\n90: 20 PCI eth1\n'):
            d=interrupt_delta(before,after,1)
            self.assertFalse(d['movable_candidates'])
            self.assertTrue(d['issues'])


if __name__=='__main__':unittest.main()
