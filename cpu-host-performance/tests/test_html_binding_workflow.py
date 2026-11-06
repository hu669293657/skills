"""HTML-only output and concrete AI task binding plans on synthetic inputs."""
import json
import tempfile
import os
import shutil
import subprocess
import unittest
from pathlib import Path


def fixture(narrow=False):
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
    td=TraceData();td.events=[Event(0,32,'sched_switch',fields={'prev_pid':0,'next_pid':4001}),
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

    def test_script_checks_all_identities_before_any_taskset(self):
        bash=shutil.which('bash') or 'C:/Program Files/Git/bin/bash.exe'
        m=fixture();plan=m['binding_plan']
        with tempfile.TemporaryDirectory() as d:
            base=Path(d);proc=base/'proc';sys=base/'sys';log=base/'taskset.log'
            def put(path,text):
                path.parent.mkdir(parents=True,exist_ok=True);path.write_text(text,encoding='utf-8')
            put(proc/'sys/kernel/random/boot_id','fixture-boot\n')
            put(sys/'devices/system/cpu/online','0-63\n')
            for action in plan['actions']:
                pid,tid=action['pid'],action['tid']
                for ident,birth in ((pid,action['process_starttime']),(tid,action['thread_starttime'])):
                    fields=['0']*40;fields[0]='S';fields[19]=str(birth)
                    put(proc/str(ident)/'stat','%d (worker) '%ident+' '.join(fields))
                put(proc/str(pid)/'task'/str(tid)/'status','Tgid:\t%d\nCpus_allowed_list:\t0-63\n'%pid)
            put(proc/'4001/task/4002/status','Tgid:\t4001\nCpus_allowed_list:\t0-63\n')
            bindir=base/'bin';put(bindir/'taskset','#!/bin/bash\nprintf "%s\\n" "$*" >> "$BIND_TEST_LOG"\n')
            put(bindir/'systemd-detect-virt','#!/bin/bash\nexit 1\n')
            path=('/'+bindir.drive[0].lower()+bindir.as_posix()[2:]) if os.name=='nt' else bindir.as_posix()
            script=plan['script'].replace('/proc/',proc.as_posix()+'/').replace('/sys/devices/',sys.as_posix()+'/devices/')
            script=script.replace('[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ]','true')
            wrapper='export PATH="%s:$PATH"\nexport BIND_TEST_LOG="%s"\nchmod +x "%s/"*\n'%(path,log.as_posix(),path)
            file=base/'bind.sh';file.write_text(wrapper+script,encoding='utf-8',newline='\n')
            result=subprocess.run([bash,str(file)],capture_output=True,text=True,encoding='utf-8',errors='replace',timeout=30)
            self.assertEqual(result.returncode,0,result.stdout+result.stderr)
            self.assertIn('taskset -apc 32-35 4001',result.stdout)
            self.assertFalse(log.exists(),'default preview must never change affinity')
            put(proc/'4001/task/4002/status','Tgid:\t4001\nCpus_allowed_list:\t32\n')
            result=subprocess.run([bash,str(file)],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(log.exists(),'all prechecks must precede any affinity change')
            # A matching boot is insufficient when the execution namespace differs.
            from analyzer.binding_plan import render_binding_script
            script=render_binding_script(plan['actions'],{'boot_id':'fixture-boot','pid_namespace':'pid:[expected]','init_pid_namespace':'pid:[expected]'})
            script=script.replace('/proc/',proc.as_posix()+'/').replace('/sys/devices/',sys.as_posix()+'/devices/').replace('[ ! -e /.dockerenv ] && [ ! -e /run/.containerenv ]','true')
            file.write_text(wrapper+'\nreadlink(){ echo "pid:[wrong]"; }\n'+script,encoding='utf-8',newline='\n')
            result=subprocess.run([bash,str(file)],capture_output=True,text=True,timeout=30)
            self.assertNotEqual(result.returncode,0)
            self.assertFalse(log.exists())

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
        self.assertEqual(plan['commands'],'taskset -apc 32-35 4001')
        from analyzer.npu_report import binding_html
        html=binding_html(m['npu_affinity'],plan=m['binding_plan'])
        self.assertIn('AI 进程与线程清单',html)
        self.assertIn('下发',html)
        self.assertIn('数据加载',html)
        self.assertIn('bind_cpu.sh',html)

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
        self.assertEqual(plan['commands'],'taskset -apc 32-35 4001')

    def test_legacy_raw_snapshot_produces_guarded_process_commands(self):
        from tests.test_npu_affinity import metrics,snapshots
        m=metrics(snapshots());plan=m.get('binding_plan',{})
        self.assertIn('taskset -apc 144-167 4115',plan.get('commands',''))
        self.assertIn('4115',plan.get('script',''))
        self.assertIn('check_process',plan.get('script',''))
        self.assertIn('test-boot',plan.get('script',''))

    def test_output_is_html_json_and_binding_script_without_markdown(self):
        from analyzer.report import generate_reports
        from analyzer.diagnosis import diagnose
        m=fixture()
        with tempfile.TemporaryDirectory() as d:
            generate_reports(m,diagnose(m),d)
            names={p.name for p in Path(d).iterdir()}
            self.assertEqual(names,{'hostbound_report.html','report_data.json','bind_cpu.sh'})
            self.assertIn('taskset -apc 32-35 4001',(Path(d)/'bind_cpu.sh').read_text(encoding='utf-8'))
