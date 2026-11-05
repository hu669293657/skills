"""User-facing collector, resource and report regressions on synthetic evidence."""
import json
import re
import unittest


class UpgradeTests(unittest.TestCase):
    def test_npu_cards_and_commands_also_obey_display_limit(self):
        from copy import deepcopy
        from analyzer.npu_report import affinity_html,binding_html
        from tests.test_npu_affinity import metrics,snapshots
        row=metrics(snapshots())['npu_affinity']['rows'][0]
        data={'rows':[dict(deepcopy(row),npu=i) for i in range(130)],'commands':'\n'.join('taskset -apc 32-63 %d'%pid for pid in range(4001,4131))}
        html=affinity_html(data)
        self.assertEqual(html.count('class="npu-process-entry '),100)
        self.assertIn('130',html)
        self.assertEqual(binding_html(data).count('taskset -apc 32-63'),100)
        self.assertEqual(len(data['rows']),130)
        self.assertEqual(len(data['commands'].splitlines()),130)

    def test_inventory_only_and_malformed_resources_do_not_break_report(self):
        from analyzer.resource_analysis import analyze_resources
        from analyzer.report_sections import resource_section
        r=analyze_resources({'snapshot':{'hostbound/resources/lsblk.txt':'NAME="sda" SIZE="1099511627776" TYPE="disk" ROTA="0" MOUNTPOINT="/data"'}})
        text=resource_section({'resources':r})
        self.assertIn('sda',text)
        self.assertIn('1024.00 GiB',text)
        for value in (123,['bad']):
            source={'ts':1,'clock':'boottime','files':{'/proc/meminfo':{'text':value}}}
            result=analyze_resources({'snapshot':{'hostbound/resource_start.json':json.dumps(source)}})
            self.assertIsNone(result['memory']['total_kb'])

    def test_unknown_peer_prevents_an_exclusive_partition_claim(self):
        from tests.test_npu_affinity import snapshots,add_complete_identity,workload_events
        from analyzer.parser import TraceData
        from analyzer.metrics import compute_metrics
        td=TraceData();td.snapshot=add_complete_identity(snapshots());td.events=workload_events()
        td.snapshot['trace_overrun.txt']='37'
        for phase in ('start','end'):
            key='hostbound/process_%s.json'%phase;batch=json.loads(td.snapshot[key])
            for row in batch['threads']:
                if row['tgid']==4117:row['cpuset_cpus_effective']=None
            td.snapshot[key]=json.dumps(batch)
        row=next(r for r in compute_metrics(td)['npu_affinity']['rows'] if r['pid']==4115)
        self.assertEqual(row['allocation_mode'],'SHARED_LOCALITY')

    def test_shared_fallback_rejects_unknown_pool_cpu_and_restricted_cpuset(self):
        from tests.test_npu_affinity import snapshots, add_complete_identity, workload_events
        from analyzer.parser import TraceData
        from analyzer.metrics import compute_metrics
        for mode in ('unknown_cpu','cpuset'):
            with self.subTest(mode=mode):
                td=TraceData();td.snapshot=add_complete_identity(snapshots());td.events=workload_events()
                td.snapshot['trace_overrun.txt']='20'
                if mode=='unknown_cpu':
                    key='hostbound/npu_smi_topology.txt'
                    td.snapshot[key]=td.snapshot[key].replace('144-167','144-167,999')
                else:
                    for phase in ('start','end'):
                        key='hostbound/process_%s.json'%phase;batch=json.loads(td.snapshot[key])
                        for row in batch['threads']:
                            if row['tgid'] in (4115,4117): row['cpuset_cpus_effective']=list(range(144,156))
                        td.snapshot[key]=json.dumps(batch)
                result=compute_metrics(td)['npu_affinity']
                for row in result['rows']:
                    if row['pid'] in (4115,4117):
                        self.assertTrue(row['blockers'])
                        self.assertNotIn(' %d'%row['pid'],result['commands'])

    def test_missing_or_reset_resource_counters_are_not_zero_rates(self):
        from analyzer.resource_analysis import analyze_resources
        self.assertFalse(analyze_resources({'snapshot':{}})['available'])
        samples=[{'ts':t,'clock':'boottime','files':{'/proc/diskstats':{'text':'8 0 sda 1 0 %d 1 1 0 20 1 0 2 3'%v}}} for t,v in [(1,100),(2,50)]]
        result=analyze_resources({'snapshot':{'hostbound/resource_start.json':json.dumps(samples[0]),'hostbound/resource_end.json':json.dumps(samples[1])}})
        self.assertIsNone(result['disks'][0]['read_bytes_per_sec'])

    def test_trace_loss_keeps_verified_shared_locality_without_equal_partition(self):
        from tests.test_npu_affinity import snapshots, add_complete_identity, workload_events
        from analyzer.parser import TraceData
        from analyzer.metrics import compute_metrics
        td=TraceData();td.snapshot=add_complete_identity(snapshots());td.events=workload_events()
        td.snapshot['trace_overrun.txt']='2496000'
        result=compute_metrics(td)['npu_affinity']
        for row in result['rows']:
            self.assertEqual(row['target'],row['affinity'])
            self.assertEqual(row['allocation_mode'],'SHARED_LOCALITY')
            self.assertTrue(row['recommended_numa'])
            self.assertIn('taskset -apc %s %d' % (row['affinity'],row['pid']),result['commands'])
        self.assertEqual(result['rows'][0]['target'],result['rows'][2]['target'])

    def test_per_table_display_limit_is_100_with_all_metrics_preserved(self):
        from analyzer.report_sections import core_table
        from analyzer.report_tables import enhance_long_tables
        from tests.test_reports import sample
        m=sample(256)
        html=enhance_long_tables(core_table(m,True))
        self.assertEqual(len(re.findall(r'<tbody>(.*?)</tbody>',html,re.S)[0].split('<tr>'))-1,100)
        self.assertIn('256',html)
        md=core_table(m)
        self.assertLessEqual(len([line for line in md.splitlines() if line.startswith('|')]),102)
        self.assertEqual(len(m['per_cpu']),256)

    def test_remove_thread_dump_and_render_empty_numa_as_text(self):
        from analyzer.npu_report import affinity_html
        from tests.test_npu_affinity import metrics,snapshots
        m=metrics(snapshots())
        html=affinity_html(m['npu_affinity'])
        self.assertNotIn('此PID全部线程亲和证据',html)
        self.assertNotRegex(html,r'建议CPU / NUMA[^<]*</span><code>[^<]*\[\]')

    def test_raw_resource_snapshots_compute_window_rates_and_capacity(self):
        from analyzer.resource_analysis import analyze_resources
        def snapshot(ts,busy,idle,available,sectors,io_ms,stalls):
            raw={'/proc/stat':'cpu %d 0 0 %d 0 0 0 0\ncpu0 0 0 0 0\n' % (busy,idle),
                 '/proc/meminfo':'MemTotal: 1000000 kB\nMemAvailable: %d kB\nSwapTotal: 100000 kB\nSwapFree: 50000 kB\n'%available,
                 '/proc/loadavg':'2.0 1.0 1.0 2/100 10\n',
                 '/proc/diskstats':'8 0 sda 10 0 %d 100 20 0 0 100 0 %d 500\n'%(sectors,io_ms),
                 '/proc/pressure/memory':'some avg10=0.0 avg60=0.0 avg300=0.0 total=%d\n'%stalls,
                 '/proc/vmstat':'pswpin 10\npswpout 20\noom_kill 0\n'}
            return json.dumps({'ts':ts,'clock':'boottime','files':{key:{'text':value,'status':'COLLECTED'} for key,value in raw.items()}})
        m={'snapshot':{'hostbound/resource_start.json':snapshot(10,100,100,100000,100,100,100),
                       'hostbound/resource_end.json':snapshot(13,190,110,40000,700,1600,300100),
                       'hostbound/resources/df_blocks.txt':'Filesystem 1024-blocks Used Available Capacity Mounted on\n/dev/sda 1000 980 20 98% /data\n',
                       'hostbound/resources/df_inodes.txt':'Filesystem Inodes IUsed IFree IUse% Mounted on\n/dev/sda 100 99 1 99% /data\n'},
           'topology':{'cpu_count':1},'n_cpus_report':1,'metadata':{}}
        result=analyze_resources(m)
        self.assertEqual(result['cpu']['busy_percent'],90)
        self.assertEqual(result['memory']['available_percent'],4)
        self.assertAlmostEqual(result['disks'][0]['read_bytes_per_sec'],102400)
        self.assertEqual(result['filesystems'][0]['used_percent'],98)
        self.assertEqual(result['filesystems'][0]['inode_used_percent'],99)
        self.assertAlmostEqual(result['pressure']['memory']['some']['stall_percent'],10)
        self.assertTrue(result['risks'])

