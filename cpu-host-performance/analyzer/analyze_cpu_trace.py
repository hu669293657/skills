#!/usr/bin/env python3
"""Dependency-free offline ftrace CPU Host report generator."""
import argparse,tarfile,tempfile,shutil,re,statistics,html
from pathlib import Path
L=re.compile(r'^\s*(.+?)-(\d+)\s+(?:\([^)]*\)\s+)?\[(\d+)\].*?\s(\d+\.\d+):\s+([\w:]+):\s*(.*)$');K=re.compile(r'(\w+)=([^\s]+)')
def q(a,p):
 a=sorted(a);i=(len(a)-1)*p/100;return a[int(i)]+(a[min(int(i)+1,len(a)-1)]-a[int(i)])*(i-int(i))
def main():
 p=argparse.ArgumentParser();p.add_argument('input');p.add_argument('--output',default='report');a=p.parse_args();src=Path(a.input);tmp=None
 if src.suffixes[-2:]==['.tar','.gz']:
  tmp=Path(tempfile.mkdtemp());t=tarfile.open(src);members=t.getmembers();any(x.name.startswith('/') or '..' in Path(x.name).parts for x in members)and(_ for _ in ()).throw(SystemExit('unsafe archive'));t.extractall(tmp);src=next(tmp.rglob('trace.txt')).parent
 trace=src/'trace.txt';events=[]
 for line in trace.open(errors='replace'):
  m=L.match(line)
  if m: events.append((float(m[4]),int(m[3]),m[5].split(':')[-1],dict(K.findall(m[6]))))
 ts=[x[0]for x in events];dur=max(ts)-min(ts)if len(ts)>1 else 0;sw=sum(x[2]=='sched_switch'for x in events);wk=sum(x[2]in('sched_wakeup','sched_waking','sched_wakeup_new')for x in events);wake={};lat=[]
 for t,c,n,k in events:
  if n in ('sched_wakeup','sched_waking','sched_wakeup_new')and k.get('pid'):wake.setdefault(k['pid'],t)
  if n=='sched_switch'and k.get('next_pid')in wake:lat.append(t-wake.pop(k['next_pid']))
 p99=q(lat,99)*1000 if lat else None;status='CRITICAL'if p99 and p99>=20 else 'WARNING'if p99 and p99>=5 else 'HEALTHY'if len(events)>=10 else 'UNKNOWN';issue='Wakeup-to-run scheduling latency is elevated'if status in('CRITICAL','WARNING')else 'No strong CPU Host contention evidence found'if status=='HEALTHY'else 'Trace has insufficient usable events';rate=lambda n:n/max(dur,.001);e=f'Parsed events: {len(events)}; trace duration: {dur:.3f}s; context switches: {rate(sw):.0f}/s; wakeups: {rate(wk):.0f}/s; matched wakeup P99: {(str(round(p99,2))+" ms")if p99 else "unavailable"}.';actions='Check IRQ/SoftIRQ distribution and IRQ affinity; review worker CPU affinity/oversubscription; repeat capture during the actual slowdown.'
 md=f'# CPU Host 性能分析报告\n\n## 1. Executive Summary\n\n### Host 状态\n\n{status}\n\n### 核心问题\n\n{issue}\n\n### 影响\n\nHigh Host scheduling delay can delay training/inference work submission; this capture alone does not prove NPU waiting.\n\n### 解决方案\n\n{actions}\n\n## 2. 问题证据\n\n{e}\n\n## 3. CPU 总体状态\n\nStatic /proc data is point-in-time only. CPU utilization cannot be inferred reliably from one snapshot.\n\n## 5. Scheduler 分析\n\nLatency matches a wakeup PID to its next sched_switch. Missing/repeated events are excluded.\n\n## 6. IRQ / SoftIRQ\n\nUse trace.txt and interrupts.txt for detailed per-CPU attribution.\n\n## 9. Affinity / NUMA\n\nEvidence insufficient to diagnose NUMA placement without task affinity and topology correlation.\n\n## 13. 结论\n\n{status}: {issue}.\n\n## 14. 数据说明\n\n{e}\n'
 out=Path(a.output);out.mkdir(parents=True,exist_ok=True);(out/'report.md').write_text(md);(out/'report.html').write_text('<!doctype html><meta charset=utf-8><style>body{font:16px system-ui;max-width:1000px;margin:40px auto;background:#f4f6f8;color:#18212b}main{background:white;padding:32px;border-radius:10px}h1{color:#101820}.status{font-size:28px;color:#b42318;font-weight:bold}</style><main><h1>CPU Host 性能分析报告</h1><div class=status>'+status+'</div><pre style="white-space:pre-wrap;font:inherit">'+html.escape(md)+'</pre></main>');print(out/'report.md');print(out/'report.html');tmp and shutil.rmtree(tmp)
if __name__=='__main__':main()
