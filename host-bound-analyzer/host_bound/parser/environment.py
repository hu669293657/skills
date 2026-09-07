# -*- coding: utf-8 -*-
"""环境静态信息解析器：cpuinfo / uname / cpu_freqs / sched_features / swaps /
kernel 命令行 / kernel_params.json。

输入契约（collector/system.py + environment.py）：
- environment/*.txt 为静态单样本；uname.txt 为 "key=value" 行；cpu_freqs.txt 为 "cpuN: value"
- kernel_params.json: {proc路径: 值}
原则：capabilities.json 是核数/型号的权威来源，本目录仅兜底（has() 守卫）。
"""

import re

from ..util import stats as ustats
from . import common


class CpuinfoParser(common.BaseParser):
    """cpuinfo.txt：核数兜底、型号、插座数、AVX-512 能力。"""

    name = "cpuinfo"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        kv = {}
        ncpu = 0
        sockets = set()
        for ln in (text or "").splitlines():
            if ":" not in ln:
                continue
            k, v = ln.split(":", 1)
            k, v = k.strip(), v.strip()
            if k == "processor":
                ncpu += 1
            elif k == "physical id" and v:
                sockets.add(v)
            elif k not in kv and v:
                kv[k] = v
        if ncpu == 0 and not kv:
            result.record(rel, self.name, status="partial", note="无 cpuinfo 键值行")
            return
        feats = result.features
        if ncpu > 0 and not feats.has("cpu.cores_logical"):
            result.set_feat("cpu.cores_logical", ncpu, unit="个", rel=rel,
                            lines="1-%d" % len((text or "").splitlines()),
                            snippet="processor 条目数=%d" % ncpu,
                            note="cpuinfo 兜底（capabilities.json 缺失）")
        if kv.get("model name") and not feats.has("cpu.model"):
            result.set_feat("cpu.model", kv["model name"], unit="", rel=rel,
                            lines="1-%d" % len((text or "").splitlines()),
                            snippet=kv["model name"][:200], note="cpuinfo 兜底")
        if sockets and not feats.has("cpu.sockets"):
            result.set_feat("cpu.sockets", len(sockets), unit="个", note="physical id 去重数")
        flags = kv.get("flags", "") or kv.get("Features", "")
        if flags and not feats.has("cpu.avx512"):
            result.set_feat("cpu.avx512", ("avx512" in flags.lower()), unit="",
                            note="flags 含 avx512*")
        result.record(rel, self.name, status="ok", note="processor=%d" % ncpu)


class UnameParser(common.BaseParser):
    """uname.txt：host 元信息兜底（capabilities 权威）。"""

    name = "uname"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        kv = {}
        for ln in (text or "").splitlines():
            if "=" in ln:
                k, v = ln.split("=", 1)
                if v.strip():
                    kv[k.strip()] = v.strip()
        if not kv:
            result.record(rel, self.name, status="partial", note="无 key=value 行")
            return
        feats = result.features
        if kv.get("node") and not feats.has("host.hostname"):
            result.set_feat("host.hostname", kv["node"], rel=rel, lines="1",
                            snippet=(text or "")[:200], note="uname 兜底")
        if kv.get("release") and not feats.has("host.kernel"):
            result.set_feat("host.kernel", kv["release"], note="uname 兜底")
        if kv.get("machine") and not feats.has("host.arch"):
            result.set_feat("host.arch", kv["machine"], note="uname 兜底")
        result.record(rel, self.name, status="ok", note="键=%d" % len(kv))


class CpuFreqsParser(common.BaseParser):
    """cpu_freqs.txt：逐核当前频率（kHz），用于降频/温控迹象佐证。"""

    name = "cpu_freqs"

    _LINE_RE = re.compile(r"^cpu\d+\s*:\s*(.+)$")

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        khz = []
        for ln in (text or "").splitlines():
            m = self._LINE_RE.match(ln.strip())
            if not m:
                continue
            v = common.fnum(m.group(1))
            if v is not None and v > 0:
                khz.append(v)
        if not khz:
            result.record(rel, self.name, status="partial", note="无有效频率值")
            return
        result.set_feat("cpu.freq_mhz_mean", round(ustats.mean(khz) / 1000.0, 1), unit="MHz",
                        rel=rel, lines="1-%d" % len((text or "").splitlines()),
                        snippet=(text or "").splitlines()[0][:200],
                        note="逐核 cpuinfo_cur_freq 均值，核数=%d" % len(khz))
        if len(khz) >= 4:
            result.set_feat("cpu.freq_mhz_cv", round(ustats.cv(khz), 3), unit="",
                            note="逐核频率变异系数（显著>0 提示核间降频不一致）")
        result.record(rel, self.name, status="ok", note="核数=%d" % len(khz))


class SchedFeaturesParser(common.BaseParser):
    """sched_features.txt：仅留证。"""

    name = "sched_features"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = [l for l in (text or "").splitlines() if l.strip()]
        result.record(rel, self.name, status="ok", note="开关数=%d（仅留证）" % len(lines))


class SwapsParser(common.BaseParser):
    """swaps.txt：swap 设备清单 + 用量汇总（meminfo 为权威）。"""

    name = "swaps"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        used_kb = 0
        n = 0
        for ln in (text or "").splitlines():
            toks = ln.split()
            if len(toks) >= 5 and toks[0] != "Filename":
                n += 1
                v = common.inum(toks[3])
                if v:
                    used_kb += v
        if n == 0:
            result.record(rel, self.name, status="partial", note="无 swap 设备行")
            return
        result.set_feat("mem.swap_devices", n, unit="个", rel=rel,
                        lines="1-%d" % len((text or "").splitlines()),
                        snippet=(text or "").splitlines()[0][:200],
                        note="/proc/swaps 设备数")
        result.set_feat("mem.swap_used_kb_total", used_kb, unit="kB", note="/proc/swaps 汇总")
        result.record(rel, self.name, status="ok", note="设备=%d used=%dkB" % (n, used_kb))


class KernelCmdlineParser(common.BaseParser):
    """cmdline_kernel.txt：内核启动参数，仅留证 + 少量要点特征。"""

    name = "kernel_cmdline"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        cmdline = (text or "").strip()
        if not cmdline:
            result.record(rel, self.name, status="partial", note="空")
            return
        result.set_feat("host.cmdline", cmdline[:300], rel=rel, lines="1",
                        snippet=cmdline[:200], note="内核启动参数（截断300）")
        if "isolcpus" in cmdline:
            result.set_feat("kernel.isolcpus", True, rel=rel, lines="1",
                            snippet=cmdline[:200], note="检测到 isolcpus（有核被隔离）")
        result.record(rel, self.name, status="ok", note="长度=%d" % len(cmdline))


class KernelParamsParser(common.BaseParser):
    """kernel_params.json：关键 sysctl 抽取 + 全量留证。"""

    name = "kernel_params"

    def parse(self, path, rel, ctx, result):
        obj = fsio_read_json(path)
        if obj is None:
            result.skip(rel, "无法读取/JSON 无效")
            return
        if not isinstance(obj, dict) or not obj:
            result.record(rel, self.name, status="partial", note="JSON 非键值结构")
            return
        kv = {}
        for k, v in obj.items():
            kl = k.lower()
            if "swappiness" in kl:
                iv = common.inum(v)
                if iv is not None:
                    result.set_feat("kernel.swappiness", iv, rel=rel, lines="1",
                                    snippet="%s=%s" % (k, v), note="vm.swappiness")
            elif "overcommit_memory" in kl:
                iv = common.inum(v)
                if iv is not None:
                    result.set_feat("kernel.overcommit_memory", iv, note="vm.overcommit_memory")
            elif "numa_balancing" in kl:
                iv = common.inum(v)
                if iv is not None:
                    result.set_feat("kernel.numa_balancing", iv, note="kernel.numa_balancing")
            kv[k] = v
        result.record(rel, self.name, status="ok", note="参数数=%d" % len(kv))


def fsio_read_json(path):
    from ..util import fsio
    return fsio.read_json(path)
