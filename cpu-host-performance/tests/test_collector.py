import importlib.util
import json
import tempfile
import unittest
from pathlib import Path

spec=importlib.util.spec_from_file_location('host_snapshot',Path(__file__).resolve().parents[1]/'scripts/host_snapshot.py')
collector=importlib.util.module_from_spec(spec);spec.loader.exec_module(collector)


def put(root,path,text):
    dest=Path(root)/path;dest.parent.mkdir(parents=True,exist_ok=True);dest.write_text(text,encoding='utf-8')


def stat(pid,start=100,cpu=3,ppid=1):
    fields=['0']*40;fields[0]='S';fields[1]=str(ppid);fields[11]='20';fields[12]='10';fields[19]=str(start);fields[36]=str(cpu)
    return '%d (name with ) paren) '%pid+' '.join(fields)


class CollectorTests(unittest.TestCase):
    def test_shared_policy_frequency_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            for c in (0,1):
                base='devices/system/cpu/cpu%d/cpufreq/'%c
                put(d,base+'related_cpus','0 1');put(d,base+'scaling_cur_freq','2400000')
            p=collector.frequency_snapshot(d)['policies']
            self.assertEqual(len(p),1)
            self.assertEqual(p[0]['cpus'],[0,1])
            self.assertEqual(p[0]['scaling_cur_freq'],2400000)
            self.assertNotIn('cpuinfo_cur_freq',p[0])
            self.assertIn('cpuinfo_cur_freq',p[0]['errors'])

    def test_proc_all_threads_and_cgroup_ancestors(self):
        with tempfile.TemporaryDirectory() as d:
            root=Path(d);procroot=root/'proc';sysroot=root/'sys'
            put(procroot,'100/stat',stat(100));put(procroot,'100/comm','python3');put(procroot,'100/cmdline','python\x00train.py\x00');put(procroot,'100/cgroup','0::/jobs/test')
            for tid in (100,101):
                put(procroot,'100/task/%d/stat'%tid,stat(tid,start=tid*10))
                put(procroot,'100/task/%d/status'%tid,'Name:\tacl_thread\nTgid:\t100\nPid:\t%d\nCpus_allowed_list:\t0-3,8\nMems_allowed_list:\t0-1'%tid)
            put(sysroot,'fs/cgroup/jobs/test/cpu.max','100000 100000');put(sysroot,'fs/cgroup/jobs/cpu.max','200000 100000');put(sysroot,'fs/cgroup/jobs/test/cpu.stat','nr_throttled 2\nthrottled_usec 100')
            snap=collector.process_snapshot(str(procroot),str(sysroot))
            self.assertEqual(len(snap['threads']),2)
            self.assertEqual(snap['threads'][1]['tgid'],100)
            self.assertEqual(snap['threads'][1]['allowed_cpus'],[0,1,2,3,8])
            self.assertEqual(snap['threads'][1]['starttime'],1010)
            self.assertEqual(snap['processes'][0]['cmdline'],'python train.py ')
            self.assertEqual(len(snap['processes'][0]['cgroup_cpu']),3)

    def test_unreadable_or_exited_proc(self):
        with tempfile.TemporaryDirectory() as d:
            (Path(d)/'100').mkdir()
            snap=collector.process_snapshot(d,d)
            self.assertFalse(snap['processes'])
            self.assertEqual(snap['errors'][0]['reason'],'stat_unreadable_or_exited')

    def test_stat_paren_and_units(self):
        s=collector.parse_stat(stat(1,start=200,cpu=128))
        self.assertEqual(s['last_cpu'],128)
        self.assertEqual(s['starttime'],200)
        self.assertEqual(s['utime_ticks'],20)

    def test_missing_policy_interfaces_empty(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertEqual(collector.frequency_snapshot(d)['policies'],[])
    def test_cgroup_v1_throttling_nanoseconds_to_microseconds(self):
        from analyzer.contention_analysis import cgroup_deltas
        def snap(value):return json.dumps({'processes':[{'tgid':100,'cgroup_cpu':[{'path':'/cpu/job','version':1,'cpu.cfs_quota_us':'200000','cpu.cfs_period_us':'100000','cpu.stat':'nr_throttled %d\nthrottled_time %d'%(value,value*1000000)}]}]})
        q=cgroup_deltas({'hostbound/process_start.json':snap(1),'hostbound/process_end.json':snap(3)})[0]
        self.assertEqual(q['quota_cpus'],2)
        self.assertEqual(q['nr_throttled_delta'],2)
        self.assertEqual(q['throttled_usec_delta'],2000)
