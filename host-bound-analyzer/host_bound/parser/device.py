# -*- coding: utf-8 -*-
"""设备（NPU/GPU）文件解析：静态快照 + 窗口循环采样。

文件与格式（collector/npu.py 产出）:
  device/npu_smi.txt           静态 `npu-smi info` 原始 stdout（或 unavailable 前缀）
  device/nvidia_smi.txt        静态 `nvidia-smi` 原始 stdout（或 unavailable 前缀）
  device/nvidia_smi.csv        `--query-gpu=timestamp,index,utilization.gpu,
                               memory.used,memory.total --format=csv`（带表头）
  device/npu_smi_samples.txt   `=== snapshot npu ts=X ===` 包裹的逐 tick npu-smi info
  device/gpu_samples.csv       `=== snapshot gpu ts=X ===` 包裹的逐 tick noheader CSV

特征产出:
  device.util_pct       窗口内主加速器利用率均值（NPU/GPU 同时存在时取较高者）
                       → host_bound/cpu_saturation/io/dataloader 四组规则引用
  device.npu_available  检测到 NPU 且输出可解析
  device.gpu_available  检测到 GPU 且输出可解析
  device.npu_name / device.gpu_name   设备型号（尽力解析，仅作报告展示）
  device.mem_used_pct   显存使用率均值（参考指标，无规则引用）

npu-smi info 表格中 AICore(%) 位于含 PCI 总地址（0000:xx:xx.x）的 Chip 行，
其后的第一个数字即为 AICore 利用率；采用 PCI 行启发式，避免表头对齐的版本差异。
"""

import re

from host_bound.parser import common

_PCI_RE = re.compile(r"^[0-9a-fA-F]{4}:[0-9a-fA-F]{2}:[0-9a-fA-F]{2}\.[0-9a-fA-F]$")
# nvidia-smi 表格首列型号行，如 "|   0  A100-SXM4-40GB    Off  |"
_GPU_NAME_RE = re.compile(r"\|\s*\d+\s+(.+?)\s+(?:Off|On)\s+\|")
# GPU-Util 单元格，如 "     45%      Default"
_GPU_UTIL_CELL_RE = re.compile(r"^\s*(\d+(?:\.\d+)?)\s*%\s+\S+$")
# npu-smi 型号 token，如 910B4 / Atlas 300 / Ascend310P
_NPU_MODEL_PREFIX = ("910", "920", "310", "Atlas", "Ascend", "Blade")


def _npu_chip_utils(lines):
    """从 npu-smi info 输出行提取每 Chip 的 AICore(%)（PCI 行启发式）。

    返回 [util, ...]；解析不到返回 []。
    """
    utils = []
    for ln in lines:
        cells = [c.strip() for c in ln.split("|")]
        for i, cell in enumerate(cells):
            if not _PCI_RE.match(cell):
                continue
            # PCI 行之后找第一个可解析数字 → AICore(%)
            val = None
            for rest in cells[i + 1:]:
                for tok in rest.split():
                    v = common.fnum(tok)
                    if v is not None:
                        val = v
                        break
                if val is not None:
                    break
            if val is not None and 0 <= val <= 100:
                utils.append(val)
            break  # 每行最多取一个 Chip 值
    return utils


def _gpu_row_values(line):
    """解析 noheader CSV 行 `ts, index, util, used, total` → (util, used_pct) 或 None。"""
    parts = [p.strip() for p in line.split(",")]
    if len(parts) < 3:
        return None
    util = common.fnum(parts[2]) if parts[2] else None
    used_pct = None
    if len(parts) >= 5:
        used = common.fnum(parts[3])
        total = common.fnum(parts[4])
        if used is not None and total and total > 0:
            used_pct = used * 100.0 / total
    if util is None and used_pct is None:
        return None
    return util, used_pct


def _set_main_util(result, rel, lines_n, snippet, mean_util, tag, other_note):
    """登记主加速器利用率：与已有值比较取较高者，note 记录另一方。"""
    feats = result.features
    old = feats.get("device.util_pct")
    if old is not None and mean_util <= old:
        return "已有 device.util_pct=%.1f 不低于 %s=%.1f，保留原值" % (
            old, tag, mean_util)
    result.set_feat("device.util_pct", round(mean_util, 1), unit="%",
                    rel=rel, lines=lines_n, snippet=snippet,
                    note=other_note or ("%s 窗口均值" % tag))
    return "device.util_pct=%.1f（%s）" % (mean_util, tag)


class NpuSmiSamplesParser(common.BaseParser):
    """device/npu_smi_samples.txt：逐 tick npu-smi info（窗口利用率权威来源）。"""

    name = "npu_smi_samples"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        blocks = common.iter_snapshots(text)
        tick_utils = []
        ts_list = []
        last_utils = []
        for rel_ts, _epoch, lines in blocks:
            utils = _npu_chip_utils(lines)
            if utils:
                tick_utils.append(sum(utils) / len(utils))
                ts_list.append(rel_ts if rel_ts is not None else len(ts_list))
                last_utils = utils
        if not tick_utils:
            result.record(rel, self.name, status="empty",
                          note="未从 npu-smi 表格解析到 AICore(%)")
            return
        mean_util = sum(tick_utils) / len(tick_utils)
        result.set_feat("device.npu_available", True, unit="",
                        rel=rel, lines="1", snippet="npu-smi info 采样 %d tick"
                                                     % len(tick_utils))
        msg = _set_main_util(result, rel, "1-%d" % len(text.splitlines()),
                             "npu-smi AICore 采样均值", mean_util, "NPU",
                             "NPU AICore 窗口均值，%d tick，单芯片峰值 %.1f%%"
                             % (len(tick_utils), max(last_utils)))
        result.features.add_series(
            "device", "util",
            list(zip(ts_list, [round(v, 2) for v in tick_utils])))
        result.record(rel, self.name, events=len(tick_utils), status="ok",
                      note=msg + "，tick 数=%d" % len(tick_utils))


class GpuSamplesParser(common.BaseParser):
    """device/gpu_samples.csv：逐 tick noheader CSV（GPU 窗口利用率权威来源）。"""

    name = "gpu_samples"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        blocks = common.iter_snapshots(text)
        tick_utils = []
        ts_list = []
        mem_pcts = []
        n_rows = 0
        for rel_ts, _epoch, lines in blocks:
            utils = []
            for ln in lines:
                if "utilization.gpu" in ln:      # 误入的表头行
                    continue
                rv = _gpu_row_values(ln)
                if rv is None:
                    continue
                util, used_pct = rv
                n_rows += 1
                if util is not None:
                    utils.append(util)
                if used_pct is not None:
                    mem_pcts.append(used_pct)
            if utils:
                tick_utils.append(sum(utils) / len(utils))
                ts_list.append(rel_ts if rel_ts is not None else len(ts_list))
        if not tick_utils:
            result.record(rel, self.name, status="empty",
                          note="未解析到 GPU 利用率行")
            return
        mean_util = sum(tick_utils) / len(tick_utils)
        result.set_feat("device.gpu_available", True, unit="",
                        rel=rel, lines="1", snippet="nvidia-smi 采样 %d tick"
                                                     % len(tick_utils))
        msg = _set_main_util(result, rel, "1-%d" % len(text.splitlines()),
                             "nvidia-smi 利用率采样均值", mean_util, "GPU",
                             "GPU 利用率窗口均值，%d tick" % len(tick_utils))
        if mem_pcts:
            result.set_feat("device.mem_used_pct", round(sum(mem_pcts) /
                                                         len(mem_pcts), 1),
                            unit="%", rel=rel, lines="1",
                            note="参考指标：显存使用率窗口均值")
        result.features.add_series(
            "device", "util",
            list(zip(ts_list, [round(v, 2) for v in tick_utils])))
        result.record(rel, self.name, events=len(tick_utils), status="ok",
                      note=msg + "，行数=%d" % n_rows)


class NpuSmiParser(common.BaseParser):
    """device/npu_smi.txt：静态单次 npu-smi info（可用性 + 兜底利用率）。"""

    name = "npu_smi"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = text.splitlines()
        utils = _npu_chip_utils(lines)
        # 型号：首列单元格的第二个 token（如 "0  910B4"）
        model = None
        for ln in lines:
            if "|" not in ln:
                continue
            head = [c.strip() for c in ln.split("|")][0]
            toks = head.split()
            if len(toks) >= 2 and toks[1].startswith(_NPU_MODEL_PREFIX):
                model = toks[1]
                break
        feats = result.features
        if not feats.has("device.npu_available"):
            result.set_feat("device.npu_available", True, unit="",
                            rel=rel, lines="1", snippet=(lines[0] if lines
                                                         else "").strip())
        if model and not feats.has("device.npu_name"):
            result.set_feat("device.npu_name", model, unit="", rel=rel,
                            lines="1", snippet=model, note="尽力解析")
        note_parts = []
        if utils:
            single = sum(utils) / len(utils)
            if not feats.has("device.util_pct"):
                result.set_feat("device.util_pct", round(single, 1), unit="%",
                                rel=rel, lines="1-%d" % len(lines),
                                snippet="AICore 静态快照",
                                note="静态单次快照（无窗口采样），仅供参考")
                note_parts.append("AICore=%.1f%%（静态）" % single)
        result.record(rel, self.name, events=len(utils), status="ok",
                      note="；".join(note_parts) if note_parts else
                      ("检测到 NPU（%s）" % model if model else "检测到 NPU"))


class NvidiaSmiTxtParser(common.BaseParser):
    """device/nvidia_smi.txt：静态 nvidia-smi 表格（可用性/型号/兜底利用率）。"""

    name = "nvidia_smi_txt"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = text.splitlines()
        feats = result.features
        name = None
        util = None
        for ln in lines:
            if name is None:
                m = _GPU_NAME_RE.search(ln)
                if m:
                    cand = m.group(1).strip()
                    if cand and not _PCI_RE.match(cand) and cand != "N/A":
                        name = cand
            if util is None:
                for cell in [c.strip() for c in ln.split("|")]:
                    m = _GPU_UTIL_CELL_RE.match(cell)
                    if m:
                        v = common.fnum(m.group(1))
                        if v is not None and 0 <= v <= 100:
                            util = v
                            break
        if not feats.has("device.gpu_available"):
            result.set_feat("device.gpu_available", True, unit="",
                            rel=rel, lines="1", snippet=(lines[0] if lines
                                                         else "").strip())
        if name and not feats.has("device.gpu_name"):
            result.set_feat("device.gpu_name", name, unit="", rel=rel,
                            lines="1", snippet=name, note="尽力解析")
        note_parts = []
        if name:
            note_parts.append("型号=%s" % name)
        if util is not None and not feats.has("device.util_pct"):
            result.set_feat("device.util_pct", util, unit="%",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="GPU-Util 静态快照",
                            note="静态单次快照（无窗口采样），仅供参考")
            note_parts.append("GPU-Util=%.1f%%（静态）" % util)
        result.record(rel, self.name, status="ok",
                      note="；".join(note_parts) if note_parts else "检测到 GPU")


class NvidiaSmiCsvParser(common.BaseParser):
    """device/nvidia_smi.csv：静态结构化查询（带表头，可用性 + 兜底利用率）。"""

    name = "nvidia_smi_csv"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        lines = [ln for ln in text.splitlines() if ln.strip()]
        if not lines:
            result.skip(rel, "empty")
            return
        utils = []
        mem_pcts = []
        start = 1 if "utilization.gpu" in lines[0] else 0
        for ln in lines[start:]:
            rv = _gpu_row_values(ln)
            if rv is None:
                continue
            util, used_pct = rv
            if util is not None:
                utils.append(util)
            if used_pct is not None:
                mem_pcts.append(used_pct)
        feats = result.features
        if not feats.has("device.gpu_available"):
            result.set_feat("device.gpu_available", True, unit="",
                            rel=rel, lines=str(start + 1),
                            snippet=lines[0].strip()[:80])
        note_parts = ["GPU 行数=%d" % len(utils)]
        if utils and not feats.has("device.util_pct"):
            mean_util = sum(utils) / len(utils)
            result.set_feat("device.util_pct", round(mean_util, 1), unit="%",
                            rel=rel, lines="1-%d" % len(lines),
                            snippet="静态 CSV 均值",
                            note="静态单次快照（无窗口采样），仅供参考")
            note_parts.append("util 均值=%.1f%%（静态）" % mean_util)
        if mem_pcts and not feats.has("device.mem_used_pct"):
            result.set_feat("device.mem_used_pct",
                            round(sum(mem_pcts) / len(mem_pcts), 1), unit="%",
                            rel=rel, lines="1", note="参考指标")
        result.record(rel, self.name, events=len(utils), status="ok",
                      note="；".join(note_parts))
