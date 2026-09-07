# -*- coding: utf-8 -*-
"""框架探测与线程归类解析：framework/framework.json（权威）与
framework/framework.txt（兜底）。

规则驱动特征:
  thread.dataloader_workers     观测到的 DataLoader worker 线程数
                                （thread_class.counts["dataloader"]）
                                → 与 dl.workers_setting 交叉核对
  thread.omp_threads_observed   观测到的 OMP 线程数
                                （thread_class.counts["omp"]）
                                → 与 thread.omp_threads_setting 交叉核对
其余（framework.name/version/python_path 等）为参考指标，仅供报告展示。
说明：线程归类由采集端完成，解析端不重复归类，仅如实呈现。
"""

import json
import re

from host_bound.parser import common

# framework.txt 中 "  torch==2.1.0 (site-packages)" 行
_TXT_FRAMEWORK_RE = re.compile(r"^\s+([A-Za-z0-9_.\-]+)==([A-Za-z0-9_.\-]+)\s*\(")
# framework.txt 中 "thread classification: {'dataloader': 8, ...}" 行
_TXT_CLASS_RE = re.compile(r"thread classification:\s*(\{.*\})")
# Python repr 字典里的 'key': value 对
_TXT_PAIR_RE = re.compile(r"'([^']+)':\s*(\d+)")

# 需要提升为特征的观测计数类别（其余仅入备注）
_OBSERVED_KEYS = (("dataloader", "thread.dataloader_workers"),
                  ("omp", "thread.omp_threads_observed"))


def _fmt_counts(counts):
    """{类别: 计数} → 稳定排序的 "dataloader=8, omp=4" 字符串。"""
    return ", ".join("%s=%d" % (k, counts[k])
                     for k in sorted(counts) if counts.get(k))


class FrameworkJsonParser(common.BaseParser):
    """framework/framework.json：框架列表 + 线程归类计数（权威来源）。"""

    name = "framework_json"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        try:
            data = json.loads(text)
        except ValueError:
            result.skip(rel, "bad-json")
            return
        if not isinstance(data, dict):
            result.skip(rel, "bad-json")
            return
        feats = result.features
        note_parts = []

        # 1) 框架列表：首个为主框架，其余并列进备注
        frameworks = data.get("frameworks") or []
        fw_names = []
        for fw in frameworks:
            if isinstance(fw, dict) and fw.get("name"):
                fw_names.append("%s==%s" % (fw["name"], fw.get("version", "?")))
        if fw_names and not feats.has("framework.name"):
            primary = frameworks[0]
            result.set_feat("framework.name", primary.get("name", ""),
                            unit="", rel=rel, lines="1",
                            snippet=json.dumps(frameworks,
                                               ensure_ascii=False)[:400],
                            note="主框架（site-packages 探测）")
            result.set_feat("framework.version", primary.get("version", ""),
                            unit="", rel=rel, lines="1",
                            note="主框架版本")
            note_parts.append("frameworks: %s" % ", ".join(fw_names))

        # 2) cmdline 提示（脚本名命中，参考信息）
        hints = data.get("cmdline_hints") or []
        if hints:
            note_parts.append("cmdline hints: %s" % ", ".join(hints))

        # 3) 线程归类计数 → 观测特征
        cls = data.get("thread_class") or {}
        counts = cls.get("counts") or {}
        if counts:
            for label, feat_key in _OBSERVED_KEYS:
                n = counts.get(label)
                if n and not feats.has(feat_key):
                    result.set_feat(feat_key, int(n), unit="个",
                                    rel=rel, lines="1",
                                    snippet="thread_class.counts: %s"
                                            % _fmt_counts(counts)[:380],
                                    note="线程名归类观测值（采集端归类）")
            note_parts.append("thread_class: %s" % _fmt_counts(counts))
            unknown = cls.get("unknown_sample") or []
            if unknown:
                note_parts.append("未归类线程样本: %s"
                                  % ", ".join(str(x) for x in unknown[:8]))

        # 4) python 路径（参考）
        py_path = data.get("python_path") or ""
        if py_path and not feats.has("framework.python_path"):
            result.set_feat("framework.python_path", py_path, unit="",
                            rel=rel, lines="1", note="目标进程解释器路径")

        if not note_parts and not frameworks:
            result.record(rel, self.name, status="empty",
                          note="framework.json 无有效内容")
            return
        result.record(rel, self.name,
                      note="; ".join(note_parts) if note_parts
                      else "framework.json 已解析")


class FrameworkTxtParser(common.BaseParser):
    """framework/framework.txt：JSON 不可用时的兜底（仅填缺失特征）。"""

    name = "framework_txt"

    def parse(self, path, rel, ctx, result):
        text, _enc = common.read_file(path)
        if not text or not text.strip():
            result.skip(rel, "empty")
            return
        if common.is_unavailable_text(text):
            result.skip(rel, "unavailable")
            return
        feats = result.features
        note_parts = []

        # 1) 框架行（兜底，JSON 已给出时跳过）
        for ln in text.splitlines():
            m = _TXT_FRAMEWORK_RE.match(ln)
            if not m:
                continue
            fw_name, fw_ver = m.group(1), m.group(2)
            if not feats.has("framework.name"):
                result.set_feat("framework.name", fw_name, unit="",
                                rel=rel, lines=ln.strip(),
                                note="主框架（framework.txt 兜底）")
                result.set_feat("framework.version", fw_ver, unit="",
                                rel=rel, lines=ln.strip(), note="主框架版本")
            note_parts.append("framework: %s==%s" % (fw_name, fw_ver))

        # 2) 线程归类（兜底）
        for ln in text.splitlines():
            m = _TXT_CLASS_RE.search(ln)
            if not m:
                continue
            counts = dict((k, int(v))
                          for k, v in _TXT_PAIR_RE.findall(m.group(1)))
            if not counts:
                continue
            for label, feat_key in _OBSERVED_KEYS:
                n = counts.get(label)
                if n and not feats.has(feat_key):
                    result.set_feat(feat_key, n, unit="个",
                                    rel=rel, lines=ln.strip(),
                                    note="线程名归类观测值（framework.txt 兜底）")
            note_parts.append("thread_class: %s" % _fmt_counts(counts))

        # 3) cmdline 提示行（仅备注）
        for ln in text.splitlines():
            if "cmdline hints:" in ln:
                hints = ln.split("cmdline hints:", 1)[1].strip()
                if hints and hints != "none":
                    note_parts.append("cmdline hints: %s" % hints)

        if not note_parts:
            result.record(rel, self.name, status="empty",
                          note="framework.txt 无可解析内容")
            return
        result.record(rel, self.name,
                      note="; ".join(note_parts))
