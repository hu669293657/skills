#!/usr/bin/env python3
"""Generate explicitly synthetic 256-CPU/8-NUMA/8-NPU evidence and offline report.

No Linux interfaces are read or modified. Refuses to overwrite existing output.
"""
import argparse
import json
import sys
import tarfile
from pathlib import Path

if __package__ in (None,''):
    sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from analyzer.analyze_cpu_trace import run_analysis


def generate(output):
    output=Path(output)
    capture=output/'synthetic_capture'/'cpu_trace'
    capture.mkdir(parents=True,exist_ok=False)
    hb=capture/'hostbound';hb.mkdir()
    def write(name,text):
        p=capture/name;p.parent.mkdir(parents=True,exist_ok=True);p.write_text(text,encoding='utf-8')
    def js(name,obj):write('hostbound/'+name,json.dumps(obj,ensure_ascii=False))
    events=[]
    def ev(ts,cpu,event,**fields):
        rest=' '.join('%s=%s'%(k,v) for k,v in fields.items())
        events.append((ts,cpu,'test-1 [%03d] .... %.9f: %s: %s'%(cpu,ts,event,rest)))
    def sw(ts,cpu,prev,nxt,state='S'):
        ev(ts,cpu,'sched_switch',prev_pid=prev,next_pid=nxt,prev_comm='t%d'%prev,next_comm='t%d'%nxt,prev_state=state)
    processes=[];threads=[]
    def process(pid,ppid,name,cmd,cgroup='/ascend/job',kernel=False):
        processes.append({'tgid':pid,'ppid':ppid,'name':name,'cmdline':cmd,'cgroup':cgroup,'starttime':pid*100,'kernel_thread':kernel,
                          'cgroup_cpu':[{'path':'/sys/fs/cgroup'+cgroup,'version':2,'cpu.max':'400000 100000' if cgroup=='/ascend/job' else 'max 100000','cpu.stat':'nr_throttled 10\nthrottled_usec 10000'}]})
    def thread(tid,tgid,name,cpus):
        threads.append({'tid':tid,'tgid':tgid,'ppid':next(p['ppid'] for p in processes if p['tgid']==tgid),'name':name,
                        'starttime':tid*100,'last_cpu':cpus[0],'allowed_cpus':cpus,'mems_allowed_list':str(cpus[0]//32),'state':'S', 'utime_ticks':100,'stime_ticks':20})
    process(3000,1,'python3','python -m vllm.entrypoints.openai.api_server')
    thread(3000,3000,'python3',[2,3])
    process(3100,3000,'EngineCore','vllm EngineCore');thread(3100,3100,'EngineCore',[4,5])
    for n in range(8):
        p=4000+n;t=5000+n;cpu=n*32
        process(p,3100,'python3','vllm worker --rank %d'%n);thread(p,p,'python3',list(range(cpu,cpu+32)));thread(t,p,'acl_thread',list(range(cpu,cpu+16)))
        process(8000+n,1,'kworker' if n==4 else 'backup-agent','background synthetic CPU task','/system.slice/backup',kernel=n==4)
        thread(9000+n,8000+n,'kworker' if n==4 else 'backup',list(range(cpu,cpu+16)))
        for sec in range(10):
            at=1000+sec
            sw(at,cpu,0 if sec==0 or n!=4 else 9000+n,9000+n)
            ev(at+.02,cpu,'sched_waking',pid=t,target_cpu=cpu,comm='acl_thread')
            ev(at+.021,cpu,'sched_wakeup',pid=t,target_cpu=cpu,comm='acl_thread')
            sw(at+.06,cpu,9000+n,t)
            sw(at+.11,cpu,t,9000+n,'R+')
            sw(at+.14,cpu,9000+n,t)
            sw(at+.2,cpu,t,9000+n if n==4 else 0)
            ev(at+.01,cpu,'irq_handler_entry',irq=32+n,name='hns3-rxtx');ev(at+.015,cpu,'irq_handler_exit',irq=32+n)
            ev(at+.025,cpu,'softirq_entry',vec=3,action='NET_RX');ev(at+.04,cpu,'softirq_exit',vec=3)
        sw(1010,cpu,9000+n if n==4 else 0,0)
        write('hostbound/pid_%d_status.txt'%p,'Name:\tpython3\nTgid:\t%d\nPid:\t%d\nThreads:\t2\nCpus_allowed_list:\t%d-%d\n'%(p,p,cpu,cpu+31))
        write('hostbound/pid_%d_threads.txt'%p,'PID TID PSR %%CPU STAT COMMAND\n%d %d %d 0 S python3\n%d %d %d 0 S acl_thread\n'%(p,p,cpu,p,t,cpu))
        write('hostbound/pid_%d_thread_affinity.txt'%p,"pid %d's current affinity list: %d-%d\npid %d's current affinity list: %d-%d\n"%(p,cpu,cpu+31,t,cpu,cpu+15))
        write('hostbound/pid_%d_starttime.txt'%p,str(p*100))
        write('hostbound/pid_%d_cpuset_effective.txt'%p,'%d-%d'%(cpu,cpu+31))
    for c in range(256):
        if c%32==0:continue
        pid=10000+c;process(pid,1,'service-%d'%c,'synthetic background work','/system.slice/normal');thread(pid,pid,'service-%d'%c,[c])
        fraction=.95 if c==129 else .05+(c%7)*.015
        for sec in range(10):
            sw(1000+sec,c,0,pid);sw(1000+sec+fraction,c,pid,0)
            if c==129:
                ev(1000+sec+.1,c,'irq_handler_entry',irq=88,name='nvme0q1');ev(1000+sec+.3,c,'irq_handler_exit',irq=88)
                ev(1000+sec+.4,c,'softirq_entry',vec=4,action='BLOCK');ev(1000+sec+.65,c,'softirq_exit',vec=4)
        sw(1010,c,0,0)
    process(6000,4000,'python3','python dataloader.py');thread(6000,6000,'pt_data_worker',[6,7])
    process(6500,1,'tokenizer','tokenizer independent service');thread(6500,6500,'tokenizer',[8,9])
    ev(1002.2,2,'sched_process_fork',parent_pid=5000,child_pid=7000,child_comm='short_helper')
    sw(1002.21,2,0,7000);sw(1002.24,2,7000,0);ev(1002.25,2,'sched_process_exit',pid=7000,comm='short_helper')
    policies=[]
    for n in range(8):
        policies.append({'id':'policy%d'%(n*32),'cpus':list(range(n*32,n*32+32)),'unit':'kHz','related_cpus':'%d-%d'%(n*32,n*32+31),
                         'scaling_cur_freq':2600000,'cpuinfo_max_freq':2600000,'scaling_min_freq':1000000,'scaling_max_freq':2600000,'scaling_governor':'ondemand'})
        for at,value in [(1000.1,1200000 if n==4 else 2400000),(1005,1200000 if n==4 else 2600000),(1009,2600000)]:
            ev(at,3,'cpu_frequency',cpu_id=n*32,state=value)
    events.append((1000,0,'test-1 [000] .... 1000.000000000: tracing_mark_write: CPU_HOST_WINDOW_START'))
    events.append((1010,0,'test-1 [000] .... 1010.000000000: tracing_mark_write: CPU_HOST_WINDOW_END'))
    write('trace.txt','# SYNTHETIC TEST DATA, not a server capture\n'+'\n'.join(e[2] for e in sorted(events,key=lambda x:(x[0],x[1])))+'\n')
    write('metadata.txt','collect_tool: synthetic_generator\ndata_origin: SYNTHETIC_TEST_DATA\ntrace_clock: mono\nfrequency_trace_unit: kHz\nnproc: 256\nhostbound_pid: 3000\nkernel: synthetic-openEuler-fixture\nhostname: SYNTHETIC-NOT-A-REAL-HOST\nminimal_mode: 0\n')
    write('cpu_online.txt','0-255');write('trace_overrun.txt','0');write('hostbound/collection_cpus.txt','0-255')
    write('numa.txt','\n'.join('== /sys/devices/system/node/node%d/cpulist ==\n%d-%d'%(n,n*32,n*32+31) for n in range(8)))
    write('hostbound/numactl.txt','\n'.join('node %d cpus: %s'%(n,' '.join(map(str,range(n*32,n*32+32)))) for n in range(8)))
    write('hostbound/lscpu_parse.txt','# CPU,Core,Socket,Node,Online\n'+'\n'.join('%d,%d,%d,%d,Y'%(c,c,c//128,c//32) for c in range(256)))
    write('hostbound/npu_smi_info.txt','\n'.join('| %d 0 | %d | python3 | 26415 |'%(n,4000+n) for n in range(8)))
    write('hostbound/npu_smi_info_start.txt',(hb/'npu_smi_info.txt').read_text())
    write('hostbound/npu_smi_topology.txt','NPU CPU Affinity\n'+'\n'.join('NPU%d X HCCS %d-%d'%(n,n*32,n*32+31) for n in range(8)))
    write('hostbound/host_context.txt','execution_context=unknown\nboot_id=SYNTHETIC\n')
    js('process_start.json',{'ts':999.9,'processes':processes,'threads':threads})
    finish=json.loads(json.dumps(processes))
    for p in finish:
        if p['cgroup']=='/ascend/job':p['cgroup_cpu'][0]['cpu.stat']='nr_throttled 14\nthrottled_usec 90000'
    js('process_end.json',{'ts':1010.1,'processes':finish,'threads':threads})
    js('frequency_start.json',{'ts':999.95,'policies':policies});js('frequency_end.json',{'ts':1010.01,'policies':policies})
    write('hostbound/frequency_samples.jsonl','\n'.join(json.dumps({'ts':1000+i,'policies':[dict(p,scaling_cur_freq=1200000 if p['id']=='policy128' and i<9 else 2600000) for p in policies]}) for i in range(11)))
    eventnames=['sched/sched_switch','sched/sched_wakeup','sched/sched_wakeup_new','sched/sched_migrate_task','sched/sched_process_fork','sched/sched_process_exec','sched/sched_process_exit','irq/irq_handler_entry','irq/irq_handler_exit','irq/softirq_entry','irq/softirq_exit','power/cpu_frequency']
    js('capabilities.json',{'events':{e:{'exists':True,'writable':True,'requested':True,'enabled':True} for e in eventnames}})
    js('device_windows.json',{'clock':'mono','data_origin':'SYNTHETIC_TEST_DATA','windows':[{'npu_id':4,'start':1000.021,'end':1000.06,'kind':'synthetic_device_gap'}]})
    archive=output/'synthetic_256_capture.tar.gz'
    with tarfile.open(str(archive),'w:gz') as tf:tf.add(str(capture),arcname='cpu_trace')
    run_analysis(str(archive),str(output),quiet=False)
    return archive


if __name__=='__main__':
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True)
    generate(ap.parse_args().output)
