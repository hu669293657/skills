import json
import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics


def fixture(processes, threads, events=(), end=None, npu=''):
    td = TraceData()
    td.events = [Event(0,0,'sched_switch', fields={'prev_pid':0,'next_pid':101}), Event(10,0,'sched_switch',fields={'prev_pid':101,'next_pid':0})] + list(events)
    td.snapshot = {'hostbound/process_start.json': json.dumps({'ts': 0, 'processes':processes, 'threads':threads}),
                   'hostbound/process_end.json': json.dumps(end or {'ts':10,'processes':processes,'threads':threads}),
                   'hostbound/npu_smi_info.txt': npu}
    return compute_metrics(td)


def proc(pid=100, ppid=1, cmd='python unrelated.py', **kw):
    p={'tgid':pid,'ppid':ppid,'name':'python3','cmdline':cmd,'starttime':10,'cgroup': '/job'}
    p.update(kw)
    return p


def thread(tid=101, tgid=100, **kw):
    t={'tid':tid,'tgid':tgid,'name':'worker','starttime':20,'allowed_cpus':[0], 'last_cpu':0}
    t.update(kw)
    return t


class ProcessDiscoveryTests(unittest.TestCase):
    def test_tid_maps_to_npu_tgid(self):
        m=fixture([proc()], [thread(),thread(102)], npu='| 2 0 | 100 | python3 | 1234 |')
        t=next(x for x in m['process_analysis']['tasks'] if x['tid']==101)
        self.assertEqual(t['tgid'],100)
        self.assertEqual(t['npu_ids'],[2])
        self.assertEqual(t['role'],'NPU_WORKER')

    def test_parent_children_dataloader(self):
        m=fixture([proc(50,1,'torchrun train.py'),proc(100,50),proc(200,100,'python data.py')], [thread(),thread(201,200,name='pt_data_worker')], npu='| 0 0 | 100 | python3 | 12 |')
        tasks=m['process_analysis']['tasks']
        loader=next(x for x in tasks if x['tid']==201)
        self.assertEqual(loader['role'],'DATA_LOADER')
        self.assertEqual(loader['npu_ids'],[0])
        self.assertIn(50,m['process_analysis']['related_tgids'])

    def test_python_alone_unknown(self):
        t=next(x for x in fixture([proc()],[thread()])['process_analysis']['tasks'] if x['tid']==101)
        self.assertEqual(t['role'],'UNKNOWN')
        self.assertFalse(t['related'])

    def test_multinpu_and_independent_helper(self):
        m=fixture([proc(100),proc(200),proc(300,1,'tokenizer service',cgroup='/other')], [thread(),thread(201,200),thread(301,300,name='tokenizer')], npu='| 0 0 | 100 | python | 1 |\n| 1 0 | 200 | python | 2 |')
        t=next(x for x in m['process_analysis']['tasks'] if x['tid']==301)
        self.assertEqual(t['npu_ids'],[])
        self.assertEqual(t['role'],'UNKNOWN')

    def test_short_lived_task_unknown_tgid(self):
        ev=[Event(1,0,'sched_process_fork', fields={'parent_pid':101,'child_pid':999,'child_comm':'child'}),Event(2,0,'sched_process_exit',pid=999,fields={'pid':999})]
        t=next(x for x in fixture([proc()],[thread()],ev)['process_analysis']['tasks'] if x['tid']==999)
        self.assertIsNone(t['tgid'])
        self.assertEqual(t['end_ts'],2)
        self.assertIn('sched_process_fork', ' '.join(t['evidence']))

    def test_pid_reuse_no_silent_merge(self):
        end={'ts':10,'processes':[proc()], 'threads':[thread(starttime=200)]}
        tasks=fixture([proc()],[thread()],end=end)['process_analysis']['tasks']
        generations=[t for t in tasks if t['tid']==101 and t.get('starttime')]
        self.assertEqual(len(generations),2)
        self.assertNotEqual(generations[0]['identity'],generations[1]['identity'])
        self.assertTrue(all(t['identity_ambiguous'] for t in generations))
    def test_root_cgroup_is_not_workload_relationship(self):
        m=fixture([proc(100,cgroup='0::/'),proc(200,cgroup='0::/')],[thread(),thread(201,200)],npu='| 0 0 | 100 | python | 1 |')
        self.assertNotIn(200,m['process_analysis']['related_tgids'])

    def test_system_ancestor_does_not_select_unrelated_siblings(self):
        m=fixture([proc(50,1,'sshd',cgroup='/sshd'),proc(100,50,cgroup='/job'),proc(200,50,cgroup='/else')],[thread(),thread(201,200)],npu='| 0 0 | 100 | python | 1 |')
        self.assertIn(50,m['process_analysis']['related_tgids'])
        self.assertNotIn(200,m['process_analysis']['related_tgids'])

    def test_fork_and_newtask_one_identity_hex_clone_flags(self):
        ev=[Event(1,0,'task_newtask',pid=101,fields={'pid':999,'clone_flags':'10000','comm':'acl_thread'}),Event(1.001,0,'sched_process_fork',fields={'parent_pid':101,'child_pid':999,'child_comm':'acl_thread'}),Event(2,0,'sched_process_exit',pid=999,fields={'pid':999})]
        tasks=[t for t in fixture([proc()],[thread()],ev)['process_analysis']['tasks'] if t['tid']==999]
        self.assertEqual(len(tasks),1)
        self.assertEqual(tasks[0]['tgid'],100)
        self.assertFalse(tasks[0]['identity_ambiguous'])
    def test_root_pid_selects_children_without_npu(self):
        td=TraceData();td.metadata={'hostbound_pid':'100'};td.events=[Event(0,0,'sched_switch',fields={'prev_pid':0,'next_pid':101}),Event(1,0,'sched_switch',fields={'prev_pid':101,'next_pid':0})]
        td.snapshot={'hostbound/process_start.json':json.dumps({'processes':[proc(100),proc(200,100)],'threads':[thread(),thread(201,200)]})}
        tasks=compute_metrics(td)['process_analysis']['tasks']
        self.assertTrue(next(t for t in tasks if t['tid']==201)['related'])

    def test_comm_hint_does_not_override_acl_role_with_api_cmdline(self):
        t=next(t for t in fixture([proc(cmd='vllm serve')],[thread(name='acl_thread')],npu='| 0 0 | 100 | python | 1 |')['process_analysis']['tasks'] if t['tid']==101)
        self.assertEqual(t['role'],'ACL_THREAD')
