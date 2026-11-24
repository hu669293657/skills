"""HTML-only output and concrete AI task binding plans on synthetic inputs."""
import json
import tempfile
import unittest
from hostbind.delivery import RUN_COMMAND, TEMPLATE, task_entry
from pathlib import Path


def fixture(narrow=False, events=None, extra_threads=()):
    from analyzer.parser import TraceData, Event
    from analyzer.metrics import compute_metrics
    procs=[];threads=[]
    for pid,ppid,name in [(3900,1,'torchrun'),(4001,3900,'python3'),(4101,4001,'pt_data_worker'),(9001,1,'python3')]:
        procs.append(dict(tgid=pid,ppid=ppid,name=name,cmdline=name+' train.py',starttime=pid*10,
                          namespace_pids=[pid],cpuset_cpus_effective=list(range(64))))
        threads.append(dict(tid=pid,tgid=pid,name=name,starttime=pid*10,namespace_pids=[pid],
                            allowed_cpus=list(range(64)),cpuset_cpus_effective=list(range(64))))
    threads.append(dict(tid=4002,tgid=4001,name='acl_thread',starttime=40020,namespace_pids=[4002],
                        allowed_cpus=[32] if narrow else list(range(64)),cpuset_cpus_effective=list(range(64))))
    threads.extend(extra_threads)
    td=TraceData();td.events=events if events is not None else [Event(0,32,'sched_switch',fields={'prev_pid':0,'next_pid':4001}),
                             Event(3,32,'sched_switch',fields={'prev_pid':4001,'next_pid':0})]
    batch=json.dumps(dict(processes=procs,threads=threads))
    td.snapshot={'hostbound/process_start.json':batch,'hostbound/process_end.json':batch,
                 'hostbound/npu_smi_info.txt':'| 0 0 | 4001 | python3 | 100 |',
                 'hostbound/npu_smi_topology.txt':'NPU CPU Affinity\nNPU0 X 32-35',
                 'hostbound/host_context.txt':'execution_context=host\nboot_id=fixture-boot\n',
                 'hostbound/cpu_online.txt':'0-63', 'trace_overrun.txt':'0',
                 'hostbound/lscpu_parse.txt':'\n'.join('%d,%d,0,0,Y'%(c,c) for c in range(64))}
    return compute_metrics(td)


class HtmlBindingWorkflowTests(unittest.TestCase):
    def test_ai_rankings_group_names_and_count_cross_process_preempt_switch_ins(self):
        from analyzer.diagnosis import diagnose
        from analyzer.parser import Event
        from analyzer.report import generate_reports

        def switch(ts, prev, nxt, state='S'):
            return Event(ts, 32, 'sched_switch', fields={
                'prev_pid': prev, 'next_pid': nxt, 'prev_state': state})

        events = [switch(0, 0, 9001), switch(1, 9001, 4002, 'R+'),
                  switch(2, 4002, 4003, 'R+'), switch(3, 4003, 9001),
                  switch(4, 9001, 4002, 'R'), switch(5, 4002, 9001, 'R+'),
                  switch(6, 9001, 4002, 'R+'), switch(7, 4002, 0)]
        threads = []
        for index, tid in enumerate(range(4003, 4015)):
            threads.append(dict(tid=tid, tgid=4001, name='acl_thread' if not index else 'worker-%02d' % index,
                                starttime=tid*10, namespace_pids=[tid], allowed_cpus=[32]))
            if index:
                events.extend((switch(8 + index*2, 0, tid), switch(9 + index*2, tid, 0)))
        m = fixture(events=events, extra_threads=threads)
        ranks = m['ai_rankings']
        threads = {row['name']: row for row in ranks['threads']}
        self.assertEqual(len(threads), 12)
        self.assertEqual(threads['acl_thread']['runtime_sec'], 4)
        self.assertEqual(threads['acl_thread']['cross_process_preemptions'], 2)
        self.assertEqual(threads['acl_thread']['preempted_tgids'], [9001])
        self.assertEqual(threads['acl_thread']['tids'], [4002, 4003])
        self.assertEqual(ranks['processes'][0]['name'], 'python3')
        self.assertEqual(ranks['processes'][0]['runtime_sec'], 15)
        self.assertEqual(ranks['processes'][0]['cross_process_preemptions'], 2)
        with tempfile.TemporaryDirectory() as d:
            html = Path(generate_reports(m, diagnose(m), d)).read_text(encoding='utf-8')
        runtime = html.split('CPU 运行时长 Top 10', 1)[1].split('跨进程抢占切入 Top 10', 1)[0]
        thread_table = runtime.split('按线程名称', 1)[1].split('</table>', 1)[0]
        self.assertEqual(thread_table.count('<td>acl_thread</td>'), 1)
        self.assertIn('<td>worker-09</td>', thread_table)
        self.assertNotIn('<td>worker-10</td>', thread_table)

    def test_different_cpusets_use_the_common_cpu_mask_in_report_and_script(self):
        from analyzer.binding_plan import build_binding_plan
        from analyzer.npu_affinity import analyze_npu_affinity
        from analyzer.npu_report import binding_html

        m = fixture()
        for phase in ('start', 'end'):
            key = 'hostbound/process_%s.json' % phase
            batch = json.loads(m['snapshot'][key])
            for row in batch['threads']:
                if row['tid'] == 4001:
                    row.update(allowed_cpus=[0, 1], cpuset_cpus_effective=[0, 1, 32, 33, 34])
                if row['tid'] == 4002:
                    row.update(allowed_cpus=[0, 1], cpuset_cpus_effective=[0, 1, 33, 34, 35],
                               starttime=None)
            m['snapshot'][key] = json.dumps(batch)
        m['npu_affinity'] = analyze_npu_affinity(m)
        plan = build_binding_plan(m)
        self.assertEqual(plan['design']['custom_bind'], [task_entry(4001, '33-34', True)])
        report = binding_html(m['npu_affinity'], plan=plan)
        self.assertLess(report.index(RUN_COMMAND), report.index('AI 进程与线程清单'))

    def test_blocked_pid_is_visible_next_to_commands(self):
        from analyzer.npu_report import binding_html
        m = fixture(narrow=True)
        report = binding_html(m['npu_affinity'], plan=m['binding_plan'])
        summary = report.split('AI 进程与线程清单', 1)[0]
        self.assertIn('PID 4001', summary)
        self.assertIn('掩码', summary)
        self.assertNotIn('复制执行命令', report)

    def test_unrelated_thread_without_tgid_is_not_a_report_error(self):
        from analyzer.parser import TraceData,Event
        from analyzer.metrics import compute_metrics
        td=TraceData();td.events=[Event(0,0,'sched_switch',fields={'prev_pid':0,'next_pid':1}),Event(1,0,'sched_switch',fields={'prev_pid':1,'next_pid':0})]
        td.snapshot={'hostbound/process_start.json':json.dumps({'processes':[],'threads':[{'tid':9001,'name':'unknown'}]})}
        m=compute_metrics(td)
        self.assertFalse(m['binding_plan']['commands'])

    def test_multi_device_distinct_pools_need_thread_assignment(self):
        from analyzer.npu_affinity import analyze_npu_affinity
        from analyzer.binding_plan import build_binding_plan
        m=fixture()
        m['snapshot']['hostbound/npu_smi_info.txt']+='\n| 1 0 | 4001 | python3 | 20 |'
        m['snapshot']['hostbound/npu_smi_topology.txt']+='\nNPU1 X 48-51'
        m['npu_affinity']=analyze_npu_affinity(m)
        plan=build_binding_plan(m)
        self.assertFalse(plan['commands'])
        self.assertTrue(any('TID/rank' in row['reason'] for row in plan['tasks']))

    def test_unsafe_npu_owner_blocks_inherited_family_commands(self):
        from analyzer.npu_affinity import analyze_npu_affinity
        from analyzer.binding_plan import build_binding_plan
        for mode in ('birth','namespace'):
            with self.subTest(mode=mode):
                m=fixture();key='hostbound/process_end.json';batch=json.loads(m['snapshot'][key])
                for p in batch['processes']:
                    if p['tgid']==4001:
                        if mode=='birth':p['starttime']=999
                        else:p['namespace_pids']=[999,4001]
                m['snapshot'][key]=json.dumps(batch)
                m['npu_affinity']=analyze_npu_affinity(m)
                plan=build_binding_plan(m)
                self.assertFalse(plan['commands'])

    def test_partial_thread_birth_data_preserves_known_narrow_masks(self):
        from analyzer.npu_affinity import analyze_npu_affinity
        from analyzer.binding_plan import build_binding_plan
        m=fixture(narrow=True)
        for phase in ('start','end'):
            key='hostbound/process_%s.json'%phase;batch=json.loads(m['snapshot'][key])
            for row in batch['threads']:
                if row['tid']==4001:row['starttime']=None
            m['snapshot'][key]=json.dumps(batch)
        m['npu_affinity']=analyze_npu_affinity(m)
        plan=build_binding_plan(m)
        self.assertFalse(plan['commands'])
        self.assertTrue(any('掩码不同' in row['reason'] for row in plan['tasks']))

    def test_html_downloads_match_the_offline_config_and_original_script(self):
        import re
        from urllib.parse import unquote_to_bytes
        from analyzer.npu_report import binding_html
        from hostbind.delivery import design_text
        plan = fixture()['binding_plan']
        downloads = dict((name, unquote_to_bytes(encoded)) for name, encoded in
                         re.findall(r'download="([^"]+)" href="data:[^,]+,([^"]+)"',
                                    binding_html({}, plan=plan)))
        self.assertEqual(downloads['cpu_binder_simple.py'], TEMPLATE.read_bytes())
        self.assertEqual(downloads['bind_design.json'], design_text(plan['design']).encode('utf-8'))
        self.assertEqual(plan['design']['custom_bind'], [task_entry(4001, '32-35', True)])

    def test_section_two_only_shows_affinity_summary(self):
        from analyzer.npu_report import affinity_html
        html=affinity_html(fixture()['npu_affinity'])
        self.assertNotIn('依据、来源、内存与线程证据',html)
        self.assertNotIn('建议CPU / NUMA',html)
        self.assertNotIn('npu-evidence',html)
        self.assertIn('NPU 0',html)

    def test_plan_lists_ai_family_but_only_binds_main_pid(self):
        m=fixture();plan=m.get('binding_plan',{})
        self.assertTrue(plan.get('tasks'))
        tids={r['tid'] for r in plan['tasks']}
        self.assertTrue({3900,4001,4002,4101}<=tids)
        self.assertNotIn(9001,tids)
        self.assertEqual(plan['design']['custom_bind'], [task_entry(4001, '32-35', True)])
        from analyzer.npu_report import binding_html
        html=binding_html(m['npu_affinity'],plan=m['binding_plan'])
        self.assertIn('AI 进程与线程清单',html)
        self.assertIn('下发',html)
        self.assertIn('数据加载',html)
        self.assertIn('cpu_binder_simple.py',html)

    def test_missing_cpuset_uses_existing_thread_affinity_as_a_lower_bound(self):
        from analyzer.npu_affinity import analyze_npu_affinity
        from analyzer.binding_plan import build_binding_plan
        m=fixture()
        for phase in ('start','end'):
            key='hostbound/process_%s.json'%phase;batch=json.loads(m['snapshot'][key])
            for row in batch['threads']+batch['processes']:row.pop('cpuset_cpus_effective',None)
            m['snapshot'][key]=json.dumps(batch)
        m['npu_affinity']=analyze_npu_affinity(m)
        plan=build_binding_plan(m)
        self.assertEqual(plan['design']['custom_bind'], [task_entry(4001, '32-35', True)])

    def test_legacy_raw_snapshot_produces_guarded_process_commands(self):
        from tests.test_npu_affinity import metrics,snapshots
        m=metrics(snapshots());plan=m.get('binding_plan',{})
        self.assertIn(task_entry(4115, '144-167', True), plan['design']['custom_bind'])
        self.assertEqual(plan['script'].encode('utf-8'), TEMPLATE.read_bytes())

    def test_output_is_html_json_and_binding_script_without_markdown(self):
        from analyzer.report import generate_reports
        from analyzer.diagnosis import diagnose
        m=fixture()
        with tempfile.TemporaryDirectory() as d:
            generate_reports(m,diagnose(m),d)
            names={p.name for p in Path(d).iterdir()}
            self.assertEqual(names,{'hostbound_report.html','report_data.json','cpu_binder_simple.py','bind_design.json'})
            self.assertEqual(json.loads((Path(d)/'bind_design.json').read_bytes()), m['binding_plan']['design'])
