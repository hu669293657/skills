# -*- coding: utf-8 -*-
"""解析器注册表：路径派发 + 单文件异常隔离 + 数据质量打标。

职责（对应需求第五/九条）：
1. 把采集产物目录中的每个文件派发给对应解析器（55 条精确映射）；
2. 单文件异常隔离：任何解析器崩溃只损失该文件，异常入台账并告警；
3. 未匹配专用解析器的文件交给 UnknownFileParser 登记（未知文件必须被识别）；
4. 解析完成后把逐文件台账同步到 DataQuality.files，并为核心数据源
   （cpu/mem/process/thread/scheduler）打 DQ mark —— 绝不虚报数据完整性。
"""
import os

from . import common
from . import device as _device
from . import environment as _environment
from . import framework as _framework
from . import generic as _generic
from . import meta as _meta
from . import numa as _numa
from . import perf as _perf
from . import proc as _proc
from . import process as _process
from . import threads as _threads
from . import tools_out as _tools_out


# ---------------------------------------------------------------------------
# 核心数据源 -> 派发文件映射（用于 DQ 打标，语义与 models/case.py.DataQuality 对齐）
# ---------------------------------------------------------------------------
CORE_SOURCE_FILES = {
    "cpu": ("system/proc_stat.txt", "tools/mpstat_P_ALL.txt"),
    "mem": ("system/meminfo.txt", "system/proc_vmstat.txt"),
    "process": ("process/target_summary.json", "process/status.txt",
                "process/process_tree.json"),
    "thread": ("threads/threads.csv", "process/ps_threads.txt"),
    "scheduler": ("system/pressure_cpu.txt", "environment/sched_features.txt"),
}

# 已知的二进制/无需解析文件：显式映射到兜底解析器，避免误报"未知文件"
_KNOWN_BINARY = ("perf/perf.data",)

_UNKNOWN = None       # UnknownFileParser 惰性单例
_TABLE = None         # rel -> parser 实例 惰性派发表


def _unknown_parser():
    global _UNKNOWN
    if _UNKNOWN is None:
        _UNKNOWN = _generic.UnknownFileParser()
    return _UNKNOWN


def _build_table():
    """构建 rel path -> 解析器实例 的精确派发表（路径分隔符统一为 /）。"""
    global _TABLE
    if _TABLE is not None:
        return _TABLE

    t = {}

    # ---- 根目录：采集元信息 ----
    t["capabilities.json"] = _meta.CapabilitiesParser()
    t["manifest.json"] = _meta.ManifestParser()

    # ---- system/：内核快照（/proc 采样与 PSI）----
    t["system/proc_stat.txt"] = _proc.ProcStatParser()
    t["system/proc_vmstat.txt"] = _proc.VmstatParser()
    t["system/loadavg.txt"] = _proc.LoadAvgParser()
    t["system/meminfo.txt"] = _proc.MeminfoParser()
    t["system/interrupts.txt"] = _proc.InterruptsParser()
    t["system/softirqs.txt"] = _proc.SoftirqsParser()
    t["system/pressure_cpu.txt"] = _proc.PsiParser("cpu")
    t["system/pressure_memory.txt"] = _proc.PsiParser("memory")
    t["system/pressure_io.txt"] = _proc.PsiParser("io")

    # ---- environment/：静态环境 ----
    t["environment/cpuinfo.txt"] = _environment.CpuinfoParser()
    t["environment/uname.txt"] = _environment.UnameParser()
    t["environment/cpu_freqs.txt"] = _environment.CpuFreqsParser()
    t["environment/sched_features.txt"] = _environment.SchedFeaturesParser()
    t["environment/swaps.txt"] = _environment.SwapsParser()
    t["environment/cmdline_kernel.txt"] = _environment.KernelCmdlineParser()
    t["environment/kernel_params.json"] = _environment.KernelParamsParser()

    # ---- threads/：目标线程级采样 ----
    t["threads/threads.csv"] = _threads.ThreadsCsvParser()

    # ---- process/：目标进程画像 ----
    t["process/target_summary.json"] = _process.TargetSummaryParser()
    t["process/cmdline.txt"] = _process.CmdlineParser()
    t["process/environ.txt"] = _process.EnvironParser()
    t["process/status.txt"] = _process.StatusParser()
    t["process/limits.txt"] = _process.LimitsParser()
    t["process/smaps_rollup.txt"] = _process.SmapsRollupParser()
    t["process/io.txt"] = _process.ProcIoParser()
    t["process/fd_count.txt"] = _process.FdCountParser()
    t["process/process_tree.json"] = _process.ProcessTreeParser()
    t["process/ps_threads.txt"] = _process.PsThreadsParser()
    t["process/ps_aux.txt"] = _process.RawSnapshotParser("ps_aux")
    t["process/pstree.txt"] = _process.RawSnapshotParser("pstree")
    t["process/top_batch.txt"] = _process.RawSnapshotParser("top_batch")
    t["process/process_tree.txt"] = _process.RawSnapshotParser("process_tree_txt")

    # ---- tools/：L2 系统工具输出 ----
    t["tools/vmstat.txt"] = _tools_out.VmstatToolsParser()
    t["tools/mpstat_P_ALL.txt"] = _tools_out.MpstatParser()
    t["tools/iostat_x.txt"] = _tools_out.IostatXParser()
    t["tools/pidstat_t.txt"] = _tools_out.PidstatParser()

    # ---- perf/：L3 perf 产物 ----
    t["perf/perf_stat.txt"] = _perf.PerfStatParser()
    t["perf/perf_report.txt"] = _perf.PerfReportParser()
    t["perf/perf_script.txt"] = _perf.PerfScriptParser()
    t["perf/perf_stat_err.txt"] = _perf.PerfErrParser()
    t["perf/perf_record_err.txt"] = _perf.PerfErrParser()

    # ---- numa/：NUMA 拓扑与统计 ----
    t["numa/nodes.txt"] = _numa.NumaNodesParser()
    t["numa/distance.txt"] = _numa.NumaDistanceParser()
    t["numa/numactl_hardware.txt"] = _numa.NumactlHardwareParser()
    t["numa/numastat_system.txt"] = _numa.NumastatSystemParser()
    t["numa/numastat_pid.txt"] = _numa.NumastatPidParser()

    # ---- device/：加速器观测 ----
    t["device/npu_smi.txt"] = _device.NpuSmiParser()
    t["device/npu_smi_samples.txt"] = _device.NpuSmiSamplesParser()
    t["device/nvidia_smi.txt"] = _device.NvidiaSmiTxtParser()
    t["device/nvidia_smi.csv"] = _device.NvidiaSmiCsvParser()
    t["device/gpu_samples.csv"] = _device.GpuSamplesParser()

    # ---- framework/：训练框架识别 ----
    t["framework/framework.json"] = _framework.FrameworkJsonParser()
    t["framework/framework.txt"] = _framework.FrameworkTxtParser()

    # ---- 已知二进制：登记但不告警 ----
    for rel in _KNOWN_BINARY:
        t[rel] = _unknown_parser()

    _TABLE = t
    return t


def parser_names():
    """调试辅助：返回派发表 {rel: parser_name}（按 rel 排序）。"""
    table = _build_table()
    return dict((rel, getattr(p, "name", "?")) for rel, p in sorted(table.items()))


def _collect_files(root):
    """递归收集采集目录下全部文件，返回排序后的 rel 列表（/ 分隔）。"""
    rels = []
    for dirpath, _dirnames, filenames in os.walk(root):
        for fn in filenames:
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root).replace("\\", "/")
            rels.append(rel)
    rels.sort()
    return rels


def _outcome_of(result, parser, rel, n_rec, n_skip):
    """根据台账增量归纳单文件解析结果 -> (status, ptype, note)。

    status 取值：ok / empty / unknown / error（与 DQ 对齐时 empty 归 missing）。
    """
    if len(result.parsed) > n_rec:
        entry = result.parsed[-1]
        st = entry.get("status") or "ok"
        return (st if st in ("ok", "empty") else "empty",
                entry.get("type") or getattr(parser, "name", "?"),
                entry.get("note") or "")
    if len(result.skipped) > n_skip:
        entry = result.skipped[-1]
        reason = str(entry.get("reason") or "")
        if reason.startswith("exception"):
            return ("error", getattr(parser, "name", "?"), reason)
        if parser is _unknown_parser():
            return ("unknown", "generic", reason or "未知产物")
        return ("empty", getattr(parser, "name", "?"), reason or "skipped")
    # 解析器既未 record 也未 skip（理论上不应发生，视为缺陷）
    return ("error", getattr(parser, "name", "?"), "解析器未产生台账")


def _mark_core_sources(ctx, outcomes):
    """核心数据源 DQ 打标（保守策略：empty 归 missing，绝不虚报完整性）。"""
    for source, rels in CORE_SOURCE_FILES.items():
        sts = []
        notes = []
        for rel in rels:
            oc = outcomes.get(rel)
            if oc is None:
                sts.append("missing")
                notes.append("%s 缺失" % rel)
                continue
            st, _ptype, note = oc
            if st == "ok":
                sts.append("present")
            elif st == "error":
                sts.append("error")
                notes.append("%s 解析异常" % rel)
            else:                      # empty / unknown 一律保守处理
                sts.append("missing")
                notes.append("%s 无有效数据（%s）" % (rel, note or "empty"))
        if "present" in sts:
            ctx.dq.mark(source, "present", "")
        elif "error" in sts:
            ctx.dq.mark(source, "error", "；".join(notes) or "解析异常")
        else:
            ctx.dq.mark(source, "missing", "；".join(notes) or "采集产物缺失")


def dispatch(root, ctx, result):
    """扫描采集目录 root 并派发解析。

    参数：
        root:   采集产物根目录（collector 输出或客户提供的 tar 解包目录）
        ctx:    parser.common.ParseContext
        result: parser.common.ParserResult（被原地填充）

    返回统计 dict：{"total", "ok", "empty", "unknown", "error"}。
    """
    table = _build_table()
    unknown = _unknown_parser()
    rels = _collect_files(root)

    stats = {"total": len(rels), "ok": 0, "empty": 0, "unknown": 0, "error": 0}
    outcomes = {}          # rel -> (status, ptype, note)

    for rel in rels:
        path = os.path.join(root, rel.replace("/", os.sep))
        parser = table.get(rel)
        is_unknown = parser is None
        if is_unknown:
            parser = unknown
            # 第九条：未知文件必须被识别并告警
            ctx.warn("未知产物 %s（未匹配专用解析器，已登记未解析）" % rel)
        n_rec, n_skip = len(result.parsed), len(result.skipped)
        try:
            parser.parse(path, rel, ctx, result)
        except Exception as exc:       # 单文件异常隔离：绝不中断整体解析
            result.skip(rel, "exception: %r" % (exc,))
            ctx.warn("解析器异常 %s (%s): %r" % (
                rel, getattr(parser, "name", "?"), exc))

        st, ptype, note = _outcome_of(result, parser, rel, n_rec, n_skip)
        if is_unknown and st != "error":
            st = "unknown"
        outcomes[rel] = (st, ptype, note)

    for st in outcomes.values():
        if st[0] == "ok":
            stats["ok"] += 1
        elif st[0] == "unknown":
            stats["unknown"] += 1
        elif st[0] == "error":
            stats["error"] += 1
        else:
            stats["empty"] += 1

    # 台账同步到 DataQuality.files（rel -> type/status/note）
    if ctx.dq is not None:
        for rel in sorted(outcomes):
            st, ptype, note = outcomes[rel]
            ctx.dq.files[rel] = {"type": ptype, "status": st, "note": note}
        _mark_core_sources(ctx, outcomes)

    result.note("解析完成：共 %d 个文件（ok=%d，empty=%d，未知=%d，异常=%d）" % (
        stats["total"], stats["ok"], stats["empty"],
        stats["unknown"], stats["error"]))
    return stats
