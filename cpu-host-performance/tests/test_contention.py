import json
import unittest
from analyzer.parser import TraceData, Event
from analyzer.metrics import compute_metrics


def switch(ts, prev, nxt, cpu=0, state='S'):
    return Event(ts,cpu,'sched_switch',fields={'prev_pid':prev,'next_pid':nxt,'prev_comm':'t%d'%prev,'next_comm':'t%d'%nxt,'prev_state':state})


def wake(ts, tid, cpu=0, kind='sched_wakeup'):
    return Event(ts,cpu,kind,fields={'pid':tid,'target_cpu':cpu,'comm':'worker'})


def analyze(events, threads=None, extra=None):
    td=TraceData();td.events=events
    procs=[{'tgid':100,'ppid':1,'name':'python','cmdline':'vllm worker','cgroup':'/job'}]
    th=threads or [{'tid':101,'tgid':100,'name':'worker','starttime':1,'allowed_cpus':[0]}]
    td.snapshot={'hostbound/process_start.json':json.dumps({'processes':procs,'threads':th}), 'hostbound/process_end.json':json.dumps({'processes':procs,'threads':th}), 'hostbound/npu_smi_info.txt':'| 2 0 | 100 | python | 123 |'}
    td.snapshot.update(extra or {})
    return compute_metrics(td)


class ContentionTests(unittest.TestCase):
    def test_high_util_alone_no_contender(self):
        m=analyze([switch(0,0,101),switch(10,101,0)])
        self.assertEqual(m['contention']['candidates'],[])
        self.assertEqual(m['thread_metrics'][0]['wait_samples'],0)
        self.assertIsNone(m['thread_metrics'][0]['wait_p99_ms'])

    def test_waking_is_not_successful_wakeup(self):
        m=analyze([switch(0,0,9),wake(1,101,kind='sched_waking'),wake(2,101),wake(3,101),switch(4,9,101),switch(10,101,0)])
        t=next(t for t in m['thread_metrics'] if t['tid']==101)
        self.assertEqual(t['wait_samples'],1)
        self.assertEqual(t['wait_p99_ms'],2000)
        self.assertEqual(t['duplicate_wakeups'],1)
        self.assertEqual(t['wakeups'],2)

    def test_wait_overlap_candidate_correlation_not_causality(self):
        m=analyze([switch(0,0,9),wake(1,101),switch(4,9,101),switch(10,101,0)])
        c=m['contention']['candidates'][0]
        self.assertEqual(c['target_tid'],101)
        self.assertEqual(c['contender_tid'],9)
        self.assertEqual(c['overlap_sec'],3)
        self.assertEqual(c['causality'],'UNPROVEN')
        self.assertEqual(c['cpus'],[0])

    def test_runnable_switch_out_is_separate_wait(self):
        m=analyze([switch(0,0,101),switch(1,101,9,state='R+'),switch(2,9,101),switch(3,101,0)])
        t=next(t for t in m['thread_metrics'] if t['tid']==101)
        self.assertEqual(t['runnable_switch_out'],1)
        self.assertEqual(t['wait_samples'],0)
        self.assertEqual(t['runqueue_wait_sec'],1)
        self.assertTrue(m['contention']['candidates'])

    def test_missing_switch_does_not_fabricate_runtime(self):
        m=analyze([switch(0,0,101),switch(2,7,8),switch(4,8,0)])
        t=next(t for t in m['thread_metrics'] if t['tid']==101)
        self.assertEqual(t['runtime_sec'],0)
        self.assertFalse(m['scheduler_quality']['complete'])

    def test_pending_wakeup_at_exit_discarded(self):
        m=analyze([switch(0,0,9),wake(1,101),Event(2,0,'sched_process_exit',pid=101,fields={'pid':101}),switch(3,9,0)])
        self.assertEqual(m['sched']['lat_count'],0)
        self.assertGreater(m['scheduler_quality']['unpaired_wakeups'],0)

    def test_explicit_migration_not_double_counted(self):
        m=analyze([switch(0,0,101),switch(1,101,0),Event(1.5,0,'sched_migrate_task',fields={'pid':101,'orig_cpu':0,'dest_cpu':1}),switch(2,0,101,cpu=1),switch(3,101,0,cpu=1)])
        t=next(t for t in m['thread_metrics'] if t['tid']==101)
        self.assertEqual(t['migrations'],1)

    def test_cgroup_delta(self):
        before={'processes':[{'tgid':100,'cgroup_cpu':[{'path':'/job','version':2,'cpu.max':'100000 100000','cpu.stat':'nr_throttled 4\nthrottled_usec 1000'}]}]}
        after={'processes':[{'tgid':100,'cgroup_cpu':[{'path':'/job','version':2,'cpu.max':'100000 100000','cpu.stat':'nr_throttled 7\nthrottled_usec 5000'}]}]}
        m=analyze([switch(0,0,101),switch(10,101,0)],extra={'hostbound/process_start.json':json.dumps(before),'hostbound/process_end.json':json.dumps(after)})
        q=m['contention']['cgroups'][0]
        self.assertEqual(q['nr_throttled_delta'],3)
        self.assertEqual(q['throttled_usec_delta'],4000)
        self.assertEqual(q['quota_cpus'],1)

    def test_affinity_overlap_not_enough(self):
        th=[{'tid':tid,'tgid':100,'name':'worker','starttime':tid,'allowed_cpus':[0]} for tid in (101,102)]
        m=analyze([switch(0,0,101),switch(5,101,102),switch(10,102,0)],threads=th)
        self.assertEqual(m['contention']['candidates'],[])
        self.assertTrue(m['contention']['affinity_overlaps'])

    def test_loss_marks_observed_wait_partial(self):
        m=analyze([switch(0,0,9),wake(1,101),switch(2,9,101),switch(3,101,0)],extra={'trace_overrun.txt':'2'})
        self.assertFalse(m['scheduler_quality']['complete'])
        self.assertEqual(m['contention']['candidates'][0]['confidence'],'LOW')
    def test_pid_reuse_cannot_pair_wakeup_across_generations(self):
        before={'processes':[{'tgid':100,'name':'worker'}],'threads':[{'tid':101,'tgid':100,'name':'worker','starttime':1}]}
        after={'processes':[{'tgid':100,'name':'different'}],'threads':[{'tid':101,'tgid':100,'name':'different','starttime':2}]}
        m=analyze([switch(0,0,9),wake(1,101),switch(8,9,101),switch(10,101,0)],extra={'hostbound/process_start.json':json.dumps(before),'hostbound/process_end.json':json.dumps(after)})
        self.assertEqual(m['sched']['lat_count'],0)
        self.assertFalse(m['scheduler_quality']['complete'])
        self.assertEqual(m['contention']['candidates'],[])

    def test_exit_without_final_switch_does_not_run_to_window_end(self):
        m=analyze([switch(0,0,101),Event(2,0,'sched_process_exit',pid=101,fields={'pid':101}),Event(10,1,'cpu_frequency',fields={'cpu_id':1,'state':2400000})])
        t=next(t for t in m['thread_metrics'] if t['tid']==101)
        self.assertEqual(t['runtime_sec'],2)
        self.assertFalse(m['scheduler_quality']['complete'])
    def test_overlap_scan_does_not_copy_unrelated_tail(self):
        from analyzer.contention_analysis import overlapping_intervals
        class NoSlice(list):
            def __getitem__(self,key):
                if isinstance(key,slice):raise AssertionError('copied interval tail')
                return super().__getitem__(key)
        spans=NoSlice([{'start':i,'end':i+1} for i in range(10000)])
        self.assertEqual(list(overlapping_intervals(spans,list(range(10000)),3.1,3.2)),[spans[3]])
    def test_worker_migration_wait_scope_splits_cpu(self):
        m=analyze([switch(0,0,9),switch(0,0,8,cpu=1),wake(1,101),Event(2,0,'sched_migrate_task',fields={'pid':101,'orig_cpu':0,'dest_cpu':1}),switch(3,8,101,cpu=1),switch(4,101,0,cpu=1),switch(4,9,0)],threads=[{'tid':101,'tgid':100,'name':'worker','starttime':1,'allowed_cpus':[0,1]}])
        pairs={c['contender_tid']:c for c in m['contention']['candidates']}
        self.assertEqual(pairs[9]['overlap_sec'],1)
        self.assertEqual(pairs[8]['overlap_sec'],1)
        self.assertEqual(pairs[8]['cpus'],[1])

    def test_full_cpu_with_low_wait_not_contention_diagnosis(self):
        from analyzer.diagnosis import diagnose
        m=analyze([switch(0,0,9),wake(1,101),switch(1.000001,9,101),switch(10,101,0)])
        findings=diagnose(m)['findings']
        self.assertFalse(any(f['type']=='CPU_CONTENTION_CANDIDATE' for f in findings))

    def test_average_low_hot_core_and_wait_remain_visible(self):
        from analyzer.diagnosis import diagnose
        events=[switch(0,0,9),wake(1,101),switch(2,9,101),switch(10,101,0)]
        for c in range(1,8):events += [switch(0,1,0,cpu=c),switch(10,0,0,cpu=c)]
        m=analyze(events);d=diagnose(m)
        self.assertLess(d['stats']['avg_util'],20)
        self.assertEqual(d['stats']['max_util'],100)
        self.assertTrue(m['contention']['candidates'])
    def test_two_npu_workers_share_one_cpu(self):
        processes=[{'tgid':pid,'ppid':1,'name':'python','cgroup':'/job'} for pid in (100,200)]
        threads=[{'tid':tid,'tgid':pid,'name':'worker','starttime':tid,'allowed_cpus':[0]} for tid,pid in ((101,100),(201,200))]
        batch=json.dumps({'processes':processes,'threads':threads})
        m=analyze([switch(0,0,101),wake(1,201),switch(2,101,201),switch(3,201,0)],extra={'hostbound/process_start.json':batch,'hostbound/process_end.json':batch,'hostbound/npu_smi_info.txt':'| 0 0 | 100 | python | 1 |\n| 1 0 | 200 | python | 1 |'})
        c=m['contention']['candidates'][0]
        self.assertEqual((c['target_tgid'],c['contender_tgid']),(200,100))
        self.assertEqual(c['npu_ids'],[1])

    def test_waking_only_cannot_create_latency(self):
        m=analyze([switch(0,0,9),wake(1,101,kind='sched_waking'),switch(2,9,101),switch(3,101,0)])
        self.assertIsNone(m['sched']['lat_p99'])

    def test_device_clock_mismatch_no_correlation_claim(self):
        m=analyze([switch(0,0,9),wake(1,101),switch(2,9,101),switch(3,101,0)],extra={'hostbound/device_windows.json':json.dumps({'clock':'realtime','windows':[{'npu_id':2,'start':1,'end':2}]})})
        self.assertEqual(m['contention']['device_correlation']['matches'],[])
