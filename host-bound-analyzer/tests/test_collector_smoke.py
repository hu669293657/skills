# -*- coding: utf-8 -*-
"""collector 冒烟测试（不依赖 Linux）：验证环境探测、快照系列、日志幂等、
DQ 分级、脱敏、进程树（Windows 降级路径）、框架探测、工具任务构建，
以及端到端 run_collect 全链路（L1 + 脱敏 + manifest + tar.gz）。

可直接运行：python tests/test_collector_smoke.py
也可被 pytest 收集：pytest tests/ -q
"""

import json
import logging
import os
import shutil
import sys
import tarfile
import tempfile

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from host_bound.collector import common
from host_bound.collector import framework as fw_mod
from host_bound.collector import packer
from host_bound.collector import process as proc_mod
from host_bound.collector import tools as tools_mod
from host_bound.collector.environment import TOOL_LIST, probe_environment
from host_bound.collector import run_collect
from host_bound.collector.main import CollectParams, CollectorOrchestrator
from host_bound.collector.sanitize import Sanitizer, current_identity

_RESULTS = []


def _check(cond, msg):
    if not cond:
        raise AssertionError(msg)
    _RESULTS.append(msg)
    print("  ok: %s" % msg)


def _tmpdir():
    return tempfile.mkdtemp(prefix="hb_collector_test_")


def _cleanup(d):
    shutil.rmtree(d, ignore_errors=True)


# ---------------------------------------------------------------- 环境探测
def test_01_probe_environment():
    print("[1] probe_environment 能力探测")
    d = _tmpdir()
    try:
        cap = probe_environment()
        for key in ("os", "cpu", "numa", "container", "tools", "device",
                    "python", "perf_paranoid", "probed_at", "notes"):
            _check(key in cap, "capabilities 含键 %s" % key)
        _check(set(cap["tools"].keys()) == set(TOOL_LIST),
               "tools 覆盖全部 %d 个工具探测项" % len(TOOL_LIST))
        _check(all(v is None or isinstance(v, str) for v in cap["tools"].values()),
               "tools 值均为路径或 None")
        _check(cap["container"]["kind"] in
               ("docker", "podman", "kubernetes", "container", "host"),
               "container.kind 合法: %s" % cap["container"]["kind"])
        _check("value" in cap["perf_paranoid"] and "hint" in cap["perf_paranoid"],
               "perf_paranoid 结构完整")
        _check("npu" in cap["device"] and "gpu" in cap["device"],
               "device 探测含 npu/gpu")
        _check(cap["os"]["name"] == "Windows" or cap["os"]["name"] != "",
               "os.name=%s" % cap["os"]["name"])
        _check(isinstance(cap["cpu"]["count_logical"], int)
               and cap["cpu"]["count_logical"] >= 1,
               "cpu.count_logical=%s" % cap["cpu"]["count_logical"])
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 快照系列
def test_02_snapshot_series():
    print("[2] SnapshotSeries 快照头与 unavailable 降级")
    d = _tmpdir()
    try:
        path = os.path.join(d, "cpuinfo.txt")
        s = common.SnapshotSeries(path, "cpuinfo")
        _check(s.capture(0.0, 1000.0, lambda: "line-A\n") is True,
               "reader 有内容 → capture True")
        s.capture(1.0, 1001.0, lambda: "line-B\n")
        _check(s.count == 2 and s.available is True, "两次快照 count=2")
        text = open(path, encoding="utf-8").read()
        _check(text.count("=== snapshot cpuinfo") == 2, "快照头出现 2 次")
        _check("ts=0.000 epoch=1000.000" in text, "头部含 ts/epoch 字段")
        _check("line-A" in text and "line-B" in text, "正文按序追加")

        path2 = os.path.join(d, "missing.txt")
        s2 = common.SnapshotSeries(path2, "missing")

        def boom():
            raise IOError("not found")

        _check(s2.capture(0.0, 1000.0, boom) is False,
               "reader 抛异常 → capture False")
        s2.capture(1.0, 1001.0, boom)
        _check(s2.available is False and s2.count == 1,
               "unavailable 只落盘一次")
        t2 = open(path2, encoding="utf-8").read()
        _check(t2.count("unavailable") == 1, "文件含单个 unavailable 标记")
        _check(s2.capture(2.0, 1002.0, lambda: "back\n") is True,
               "恢复后继续正常采样")
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 日志幂等
def test_03_logger_idempotency():
    print("[3] setup_logger 幂等 + collector.log 落盘")
    lg = logging.getLogger("collector")
    for h in list(lg.handlers):
        lg.removeHandler(h)
    d = _tmpdir()
    try:
        lg2 = common.setup_logger()
        _check(not any(isinstance(h, logging.FileHandler)
                       for h in lg2.handlers),
               "首次无 outdir → 仅 stderr 句柄")
        common.setup_logger(d)
        fhs = [h for h in lg2.handlers if isinstance(h, logging.FileHandler)]
        _check(len(fhs) == 1, "补挂 outdir → 恰好 1 个文件句柄")
        lg2.info("hello-collector-log")
        for h in lg2.handlers:
            h.flush()
        logfile = os.path.join(d, "collector.log")
        _check(os.path.isfile(logfile), "collector.log 已落盘")
        content = open(logfile, encoding="utf-8").read()
        _check("hello-collector-log" in content, "日志内容写入文件")
        common.setup_logger(d)
        common.setup_logger()
        fhs2 = [h for h in lg2.handlers if isinstance(h, logging.FileHandler)]
        _check(len(fhs2) == 1, "重复调用不重复挂句柄")
    finally:
        for h in list(lg.handlers):
            h.close()
            lg.removeHandler(h)
        _cleanup(d)


# ---------------------------------------------------------------- DQ 分级
def test_04_compute_dq_grades():
    print("[4] compute_dq 三档分级")
    dev = {"npu": {"available": True, "tool": "npu-smi", "count": 8}}
    tool_res = {"vmstat": {"ok": True, "rc": 0}}
    dq = packer.compute_dq(True, True, 200, tool_res, dev,
                           {"perf_stat": {"ok": True}}, [])
    _check(dq["grade"] == "complete", "全核心+外围 → complete")
    _check(dq["groups"] == {"system": "present", "process": "present",
                            "threads": "present", "tools": "present",
                            "device": "present", "perf": "present"},
           "六组状态全 present")
    dq2 = packer.compute_dq(True, False, 0, {}, {}, {}, [])
    _check(dq2["grade"] == "partial" and dq2["groups"]["process"] == "missing",
           "部分核心 → partial")
    dq3 = packer.compute_dq(False, False, 0, {}, {}, {}, [])
    _check(dq3["grade"] == "insufficient", "全缺 → insufficient")
    dq4 = packer.compute_dq(True, True, 1, {}, {}, {}, [])
    _check(dq4["grade"] == "partial",
           "核心全但外围为 0 → 仍 partial（complete 需 ≥1 外围）")
    dq5 = packer.compute_dq(True, True, 0, {"vmstat": {"ok": False}},
                            {}, {}, [])
    _check(dq5["groups"]["tools"] == "missing" and dq5["groups"]["threads"] == "missing",
           "ok=False 的工具结果与 0 线程行均判 missing")


# ---------------------------------------------------------------- 脱敏
def test_05_sanitizer():
    print("[5] Sanitizer 替换规则 + sanitize_tree")
    san = Sanitizer("my-node-01", "zhangsan")
    t = san.sanitize_text(
        "host my-node-01 user=zhangsan\n"
        "addr 192.168.1.10 gw 10.0.0.1\n"
        "fqdn my-node-01.lab.example.com\n"
        "home /home/zhangsan/data and /root/secret\n")
    _check("192.168.1.10" not in t, "IP 被替换")
    _check("10.0.0." in t, "IP 替换为 10.0.0.x 段")
    _check("my-node-01" not in t, "主机名被替换（含 FQDN 前缀）")
    _check("zhangsan" not in t, "用户名被替换")
    _check("/home/zhangsan" not in t, "/home/<user> 路径被替换")
    _check("/root/secret" not in t, "/root 路径被替换")
    _check("/path/to" in t, "替换产物 /path/to 出现")
    # FQDN 规则针对本机真实 FQDN（伪造外部域名不在脱敏范围）
    real_host, real_user = current_identity()
    if real_host:
        san_r = Sanitizer(real_host, real_user)
        t_r = san_r.sanitize_text("node %s fqdn %s ok"
                                  % (real_host, san_r.fqdn or real_host))
        _check(real_host not in t_r, "本机真实主机名被替换")
        _check(san_r.fqdn not in t_r, "本机真实 FQDN 被替换")

    d = _tmpdir()
    try:
        p = os.path.join(d, "sub", "a.txt")
        os.makedirs(os.path.dirname(p))
        with open(p, "w", encoding="utf-8") as f:
            f.write("ip 172.16.0.5 host hostA user bob\n")
        san2 = Sanitizer("hostA", "bob")
        stats = san2.sanitize_tree(d)
        _check(stats["files"] >= 1 and stats["replacements"] >= 3,
               "sanitize_tree 统计 files/replacements")
        t2 = open(p, encoding="utf-8").read()
        _check("172.16.0.5" not in t2 and "hostA" not in t2
               and "bob" not in t2, "子目录文件内容全部替换")
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 进程树（Windows 降级）
def test_06_process_tree_and_ps():
    print("[6] capture_process_tree 无 /proc 时不抛错（修复验证）")
    d = _tmpdir()
    try:
        res = proc_mod.capture_process_tree(d)
        _check(isinstance(res, dict) and res.get("processes") == 0,
               "非 Linux 返回 processes=0")
        jt = os.path.join(d, "process", "process_tree.json")
        tt = os.path.join(d, "process", "process_tree.txt")
        _check(os.path.isfile(jt) and os.path.isfile(tt),
               "process_tree.json/txt 均写出")
        data = json.load(open(jt, encoding="utf-8"))
        _check(data["name"] == "root" and data["children"] == [],
               "空树 JSON 结构合法")
        _check(open(tt, encoding="utf-8").read() == "\n",
               "空树文本为单换行（NameError 已修复）")

        res2 = proc_mod.capture_ps(d, {t: None for t in
                                       ("ps", "top", "pstree")}, None)
        _check(isinstance(res2, dict), "capture_ps 无工具时安全返回")
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 框架探测
def test_07_framework_collect():
    print("[7] framework.collect 空提示与线程归类")
    d = _tmpdir()
    try:
        payload = fw_mod.collect(d, {}, [])
        _check(payload["frameworks"] == [], "无 exe → 无框架")
        _check(payload["cmdline_hints"] == [], "空 cmdline → 空 hints")
        txt = open(os.path.join(d, "framework", "framework.txt"),
                   encoding="utf-8").read()
        _check("cmdline hints: none" in txt,
               "空 hints 输出 none（运算符优先级已修复）")
        _check("thread classification" in txt, "含线程归类行")
        _check(os.path.isfile(os.path.join(d, "framework", "framework.json")),
               "framework.json 写出")
    finally:
        _cleanup(d)

    d2 = _tmpdir()
    try:
        payload2 = fw_mod.collect(d2, {"cmdline": ["python", "-m",
                                                   "torch.distributed.run",
                                                   "train.py"],
                                       "exe": ""}, [])
        _check("torch" in payload2["cmdline_hints"],
               "cmdline 框架关键词被提取: %s" % payload2["cmdline_hints"])
        cls = payload2["thread_class"]
        _check("counts" in cls and "unknown_sample" in cls,
               "classify_threads 结构完整")
    finally:
        _cleanup(d2)


# ---------------------------------------------------------------- 工具任务
def test_08_build_jobs():
    print("[8] build_jobs 任务构建与轮数计算")
    _check(tools_mod._count(60, 1.0) == 61, "count(60s,1s)=61")
    _check(tools_mod._count(10, 2) == 6, "count(10s,2s)=6")
    _check(tools_mod._count(0.5, 0.05) >= 2, "count 下限为 2")
    d = _tmpdir()
    try:
        jobs = tools_mod.build_jobs(d, {"vmstat": "/usr/bin/vmstat"},
                                    None, 10, 2)
        _check(len(jobs) == 1 and jobs[0][0] == "vmstat",
               "仅 vmstat 可用 → 1 个任务")
        name, cmd, outfile = jobs[0]
        _check(cmd[1] == "2" and cmd[2] == "6",
               "vmstat 参数 interval=2 count=6")
        _check(outfile.endswith(os.path.join("tools", "vmstat.txt")),
               "输出落在 tools/ 下")
        jobs_all = tools_mod.build_jobs(
            d, {"vmstat": "1", "mpstat": "1", "iostat": "1", "pidstat": "1"},
            4242, 30, 1)
        _check([j[0] for j in jobs_all] ==
               ["vmstat", "mpstat", "iostat", "pidstat"],
               "四工具齐全 → 4 个任务")
        pidjob = [j for j in jobs_all if j[0] == "pidstat"][0]
        _check("-p" in pidjob[1] and "4242" in pidjob[1],
               "pidstat 带 -p <pid>")
        _check(tools_mod.build_jobs(d, {}, None, 10, 1) == [],
               "无可用工具 → 空任务列表")
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 端到端
def test_09_end_to_end_collect():
    print("[9] run_collect 端到端（L1 + 脱敏 + manifest + tar.gz）")
    base = _tmpdir()
    try:
        params = CollectParams(duration=2, interval=0.5, pid=None,
                               auto_detect=False, level=1, output=base,
                               sanitize=True, no_tar=False)
        res = run_collect(params)
        _check(isinstance(res, dict), "run_collect 返回字典")
        _check(res["status"] in ("ok", "partial"),
               "status=%s（dq=%s）" % (res["status"], res["dq_grade"]))
        _check(res["dq_grade"] in ("complete", "partial", "insufficient"),
               "dq_grade 合法")
        outdir = res["outdir"]
        _check(outdir.startswith(base) and os.path.isdir(outdir),
               "outdir 在指定目录下创建")
        _check(os.path.isfile(os.path.join(outdir, "capabilities.json")),
               "capabilities.json 写出")
        _check(os.path.isfile(os.path.join(outdir, "collector.log")),
               "collector.log 落盘（幂等修复端到端验证）")
        _check(os.path.isfile(os.path.join(outdir, "manifest.json")),
               "manifest.json 写出")

        m = res["manifest"]
        _check(m["case_id"] == res["case_id"], "manifest.case_id 一致")
        _check(res["case_id"].startswith("hb-"), "case_id 前缀 hb-")
        _check(m["params"]["duration"] == 2.0 and m["params"]["level"] == 1,
               "manifest.params 记录 duration/level")
        _check(m["data_quality"]["grade"] == res["dq_grade"],
               "manifest.data_quality 与 dq_grade 一致")
        _check(m["sanitized"] is True, "manifest.sanitized 标记")
        _check(len(m["files"]) >= 5, "manifest.files 共 %d 项"
               % len(m["files"]))
        missing = [f["path"] for f in m["files"]
                   if not os.path.exists(os.path.join(outdir, f["path"]))]
        _check(not missing, "清单内全部文件真实存在（缺失=%s）" % missing[:3])
        _check(m["hostname"] in ("host-1", "") and
               "host-1" in res["case_id"], "脱敏后主机名进入 manifest/case_id")

        tar_path = res["tar"]
        _check(tar_path and os.path.isfile(tar_path), "tar.gz 已生成")
        with tarfile.open(tar_path, "r:gz") as tf:
            names = [n.replace("\\", "/") for n in tf.getnames()]
        roots = {n.split("/")[0] for n in names if n.strip()}
        _check(roots == {res["case_id"]},
               "tar 包内根目录唯一且等于 case_id")
        _check(any(n.endswith("manifest.json") for n in names),
               "tar 内含 manifest.json")
        _check(any(n.endswith("capabilities.json") for n in names),
               "tar 内含 capabilities.json")

        notes = res["notes"]
        _check(isinstance(notes, list), "notes 列表返回（%d 条）" % len(notes))
    finally:
        _cleanup(base)


# ---------------------------------------------------------------- 线程采集
def test_10_thread_sampler_makedirs():
    print("[10] ThreadSampler 自动创建 threads/ 子目录（回归：threads.csv 崩溃）")
    from host_bound.collector import thread as thr_mod
    d = _tmpdir()
    try:
        # 回归点：构造 ThreadSampler 必须立刻创建 <outdir>/threads/，
        # 否则 capture() 首次 open("w") 会 FileNotFoundError
        ts = thr_mod.ThreadSampler(d, 999999)  # pid 不存在也要先建目录
        _check(os.path.isdir(os.path.join(d, "threads")),
               "构造时创建 threads/ 子目录")
        _check(not ts.available(), "pid 不存在时 available()=False")
        _check(not ts.capture(0.0, 0.0), "pid 不存在时 capture() 安全返回 False")

        if os.path.isdir("/proc/self/task"):  # Linux：真实采集全链路
            ts2 = thr_mod.ThreadSampler(d, "self")
            _check(ts2.available(), "Linux 下 /proc/self/task 可用")
            _check(ts2.capture(1.0, 1000.0), "capture() 采集成功")
            _check(ts2.capture(2.0, 2000.0), "第二次 capture() 追加成功")
            with open(ts2.path, "r", encoding="utf-8") as f:
                lines = f.read().splitlines()
            _check(lines[0].startswith("ts,tid,name,state,utime"),
                   "表头正确: %s" % lines[0][:40])
            _check(len(lines) >= 3, "至少采到主线程数据（%d 行）" % len(lines))
            s = ts2.summary()
            _check(s["rows"] == ts2.rows and s["path"] == "threads/threads.csv",
                   "summary() 统计一致（rows=%d）" % s["rows"])
    finally:
        _cleanup(d)


# ---------------------------------------------------------------- 入口
ALL = [test_01_probe_environment, test_02_snapshot_series,
       test_03_logger_idempotency, test_04_compute_dq_grades,
       test_05_sanitizer, test_06_process_tree_and_ps,
       test_07_framework_collect, test_08_build_jobs,
       test_09_end_to_end_collect, test_10_thread_sampler_makedirs]


def main():
    failed = 0
    for fn in ALL:
        try:
            fn()
        except AssertionError as e:
            failed += 1
            print("  FAIL: %s" % e)
        except Exception as e:  # noqa: BLE001
            failed += 1
            print("  ERROR: %r" % e)
    print("\n共 %d 项检查通过，%d 个测试失败" % (len(_RESULTS), failed))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
