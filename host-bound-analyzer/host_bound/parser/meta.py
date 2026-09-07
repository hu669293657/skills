# -*- coding: utf-8 -*-
"""元信息解析器：capabilities.json 与 manifest.json。

输入契约（collector/environment.py + collector/packer.py）：
- capabilities.json:
  {probed_at, notes, os:{name,kernel,arch,hostname,distro},
   cpu:{count_logical,model,sockets,count_physical,avx512,governors},
   numa:{available,node_count,via}, container:{is_container,kind,evidence},
   tools:{name: which_path_or_None}, device:{npu:{available,tool,count},
   gpu:{available,tool,count}}, python:{path,version,bin},
   perf_paranoid:{value,hint}}
- manifest.json: {manifest_version, case_id, created_at, version:{...},
   params, hostname, username, target, capabilities, data_quality, sanitized, files}

原则：capabilities.json 是核数/拓扑/工具的**权威来源**；其它解析器一律 has() 守卫兜底。
"""

from . import common


def _load_json(path):
    from ..util import fsio
    return fsio.read_json(path)


class CapabilitiesParser(common.BaseParser):
    """capabilities.json：核数/拓扑/NUMA/工具/设备的权威特征来源。"""

    name = "capabilities"

    def parse(self, path, rel, ctx, result):
        obj = _load_json(path)
        if not isinstance(obj, dict) or not obj:
            result.skip(rel, "无法读取/JSON 无效")
            return
        feats = result.features
        cpu = obj.get("cpu") if isinstance(obj.get("cpu"), dict) else {}
        n_logical = common.inum(cpu.get("count_logical"))
        if n_logical and n_logical > 0:
            result.set_feat("cpu.cores_logical", n_logical, unit="个", rel=rel,
                            lines="1", snippet="count_logical=%s" % cpu.get("count_logical"),
                            note="capabilities 权威来源")
        if common.inum(cpu.get("count_physical")):
            result.set_feat("cpu.count_physical", common.inum(cpu.get("count_physical")),
                            unit="个", note="物理核数（去重 core id）")
        if common.inum(cpu.get("sockets")):
            result.set_feat("cpu.sockets", common.inum(cpu.get("sockets")),
                            unit="个", note="插座数")
        if cpu.get("model"):
            result.set_feat("cpu.model", str(cpu["model"]), unit="", rel=rel,
                            lines="1", snippet=str(cpu["model"])[:200], note="CPU 型号")
        if cpu.get("avx512") is not None:
            result.set_feat("cpu.avx512", bool(cpu["avx512"]), unit="",
                            note="AVX-512 支持")
        govs = cpu.get("governors")
        if isinstance(govs, list) and govs:
            uniq = sorted(set(str(g) for g in govs if g))
            if uniq:
                result.set_feat("cpu.governors", ",".join(uniq), unit="",
                                note="调频策略（去重）")

        numa = obj.get("numa") if isinstance(obj.get("numa"), dict) else {}
        n_node = common.inum(numa.get("node_count"))
        if n_node and n_node > 0:
            result.set_feat("numa.node_count", n_node, unit="个", rel=rel,
                            lines="1", snippet="node_count=%d" % n_node,
                            note="capabilities（权威）")
            result.set_feat("numa.available", bool(numa.get("available")), unit="",
                            note="NUMA 可用性")

        cont = obj.get("container") if isinstance(obj.get("container"), dict) else {}
        result.set_feat("host.is_container", bool(cont.get("is_container")), unit="",
                        note="容器内运行")
        if cont.get("kind"):
            result.set_feat("host.container_kind", str(cont["kind"]), unit="",
                            note="容器类型")

        dev = obj.get("device") if isinstance(obj.get("device"), dict) else {}
        for kind in ("npu", "gpu"):
            sub = dev.get(kind) if isinstance(dev.get(kind), dict) else {}
            if sub.get("available"):
                result.set_feat("device.%s_available" % kind, True, unit="",
                                note="capabilities 设备探测")
                cnt = common.inum(sub.get("count"))
                if cnt and cnt > 0:
                    result.set_feat("device.count_hint", cnt, unit="个",
                                    note="%s 设备数提示（%s）" % (kind.upper(), kind))

        tools = obj.get("tools") if isinstance(obj.get("tools"), dict) else {}
        have = sorted([k for k, v in tools.items() if v])
        if have:
            result.set_feat("host.tools_available", ",".join(have), unit="",
                            rel=rel, lines="1", snippet="tools=%s" % ",".join(have)[:300],
                            note="采集端可用工具")
        miss = sorted([k for k, v in tools.items() if not v])
        if miss:
            ctx.warn("capabilities: 缺失工具 %s" % ",".join(miss))

        perf_p = obj.get("perf_paranoid") if isinstance(obj.get("perf_paranoid"), dict) else {}
        if common.inum(perf_p.get("value")) is not None:
            result.set_feat("kernel.perf_paranoid", common.inum(perf_p.get("value")),
                            unit="", note="/proc/sys/kernel/perf_event_paranoid")

        result.record(rel, self.name, status="ok",
                      note="logical=%s numa=%s tools=%d" % (
                          n_logical, n_node, len(have)))


class ManifestParser(common.BaseParser):
    """manifest.json：案例元信息与数据质量等级（上报层职责，这里仅留特征）。"""

    name = "manifest"

    def parse(self, path, rel, ctx, result):
        obj = _load_json(path)
        if not isinstance(obj, dict) or not obj:
            result.skip(rel, "无法读取/JSON 无效")
            return
        if obj.get("case_id"):
            result.set_feat("host.case_id", str(obj["case_id"]), unit="", rel=rel,
                            lines="1", snippet=str(obj["case_id"])[:200],
                            note="manifest.case_id")
        ver = obj.get("version") if isinstance(obj.get("version"), dict) else {}
        for key in ("tool", "schema", "report", "rule"):
            if ver.get(key):
                result.set_feat("manifest.%s_version" % key, str(ver[key]), unit="",
                                note="版本四元组")
        dq = obj.get("data_quality") if isinstance(obj.get("data_quality"), dict) else {}
        if dq.get("grade"):
            result.set_feat("manifest.dq_grade", str(dq["grade"]), unit="",
                            note="采集侧数据质量初判")
        result.set_feat("manifest.sanitized", bool(obj.get("sanitized")), unit="",
                        note="是否已脱敏")
        result.record(rel, self.name, status="ok", note="case=%s" % obj.get("case_id"))
