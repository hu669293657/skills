#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Complete-file streaming triage, standard library only; companion to log_analyzer.py.
No early stop after repeated hits. --retain bounds examples, not scanning.
"""
import argparse
import collections
import datetime
import gzip
import heapq
import json
import os
import re

PATTERNS = {
    "host_oom": r"Out of memory: Killed process|Memory cgroup out of memory|oom-kill|oom_kill|OOMKilled",
    "process_exit": r"ChildFailedError|SIGSEGV|SIGABRT|SIGKILL|SIGTERM|Segmentation fault|exitcode|exit code|Killed process",
    "k8s_lifecycle": r"Evicted|FailedScheduling|FailedMount|ImagePullBackOff|CrashLoopBackOff|Preempted",
    "network_control": r"gai error|Name or service not known|Temporary failure in name resolution|rendezvous|Connection refused|Address already in use",
    "network_data": r"ERROR CQE|LINK DOWN|link is down|wait socket establish timeout|network unreachable|No route to host",
    "hccl_timeout": r"HCCL.{0,110}(?:timeout|timed out)|(?:timeout|timed out).{0,110}HCCL|Notify Wait Timeout|STUCK|LOST",
    "device_fault": r"uncorrectable.{0,30}ECC|device.{0,40}(?:fault|reset|offline)|PCIe.{0,40}(?:AER|fatal)",
    "resource": r"No space left on device|Too many open files|out of memory|CANN.*memory allocation failed",
    "python_error": r"Traceback \(most recent call last\)|[A-Za-z_]+(?:Error|Exception):",
    "generic_error": r"\b(?:ERROR|FATAL|CRITICAL)\b|\b[Ee]rror:",
}
TIME = re.compile(r"(20\d{2}[-/]\d{2}[-/]\d{2}[ T-]\d{2}:\d{2}:\d{2}(?:\.\d+)?)")
RANK = re.compile(r"(?i)(?:\brank[_ :=-]*|\[rank)(\d+)")
PID = re.compile(r"(?i)\bpid[_ :=-]*(\d+)\b")
SKIP = {".git", "__pycache__", "analysis_out", "node_modules"}
COMP = [(k,re.compile(v,re.I)) for k,v in PATTERNS.items()]


def category(line):
    for key, pat in COMP:
        if pat.search(line):
            return key
    return None


def iter_paths(root):
    for base, dirs, files in os.walk(root, followlinks=False):
        dirs[:] = sorted(d for d in dirs if d not in SKIP and not os.path.islink(os.path.join(base,d)))
        for name in sorted(files):
            path=os.path.join(base,name)
            if os.path.islink(path) or not (name.lower().endswith((".log",".txt",".out",".gz",".jsonl")) or name in ("messages","syslog","dmesg","nohup.out")):
                continue
            yield path


def opener(p):
    return gzip.open(p,"rt",encoding="utf-8",errors="replace") if p.lower().endswith(".gz") else open(p,"r",encoding="utf-8",errors="replace")


def scan(root, retain=25):
    matched=collections.Counter()
    scanned=collections.Counter()
    examples=[]
    rank_events=collections.Counter()
    errors=[]
    total=0
    for path in iter_paths(root):
        rel=os.path.relpath(path,root).replace(os.sep,"/")
        try:
            with opener(path) as stream:
                for lineno,line in enumerate(stream,1):
                    scanned[rel]+=1
                    kind=category(line)
                    if not kind: continue
                    total+=1
                    matched[kind]+=1
                    if len(examples)<retain:
                        ts=TIME.search(line)
                        rk=RANK.search(line)
                        pid=PID.search(line)
                        evt={"file":rel,"line":lineno,"kind":kind,"timestamp_raw":ts.group(1) if ts else None,
                             "rank":int(rk.group(1)) if rk else None,"pid":int(pid.group(1)) if pid else None,
                             "quote":line.rstrip("\r\n")[:500]}
                        examples.append(evt)
                    rk=RANK.search(line)
                    if rk: rank_events["rank:"+rk.group(1)]+=1
        except (OSError,EOFError) as exc:
            errors.append({"file":rel,"error":str(exc)})
    return {"schema_version":1,"root":os.path.abspath(root),"generated_at":datetime.datetime.now().isoformat(),
            "files_scanned":len(scanned),"lines_scanned":sum(scanned.values()),"total_matches":total,
            "matched_by_category":dict(matched),"rank_mentions_on_matches":dict(rank_events),
            "retained_examples":len(examples),"retained_examples_limit":retain,
            "coverage":{"full_text_scan":not bool(errors),"examples_truncated":total>len(examples),
                        "note":"Counts cover complete readable input; examples are a front sample, NOT guaranteed earliest root cause."},
            "events":examples,"read_errors":errors}


def main():
    ap=argparse.ArgumentParser(description="Ascend full-stack exhaustive log triage; never stops on hit cap")
    ap.add_argument("--dir",required=True)
    ap.add_argument("--out",required=True)
    ap.add_argument("--retain",type=int,default=25)
    a=ap.parse_args()
    if not os.path.isdir(a.dir): ap.error("--dir must be a directory")
    if a.retain<0: ap.error("--retain must be non-negative")
    result=scan(a.dir,a.retain)
    os.makedirs(os.path.dirname(os.path.abspath(a.out)),exist_ok=True)
    with open(a.out,"w",encoding="utf-8") as f:json.dump(result,f,ensure_ascii=False,indent=2)
    print("Scanned",result["files_scanned"],"files;",result["lines_scanned"],"lines;",result["total_matches"],"matches.")
    print("Examples",result["retained_examples"],"/",result["total_matches"],"; full scan:",result["coverage"]["full_text_scan"])
    if result["read_errors"]: raise SystemExit(2)

if __name__=="__main__":main()
