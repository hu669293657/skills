# -*- coding: utf-8 -*-
"""collector：自适应采集器（Linux 零依赖、三级采集、manifest/tar）。

公开入口：
  run_collect(params) → 采集结果字典（status/outdir/tar/case_id/dq_grade/manifest）
  CollectParams       → 采集参数（duration/interval/pid/auto_detect/level/output/
                        sanitize/no_tar）
  CollectorOrchestrator → 编排器实例（可注入 logger）

设计要点（对齐 DESIGN 5.1）：
  - 零第三方依赖：只用标准库；目标机无 Python 时依赖独立打包脚本（见交付层）
  - L1 默认：/proc 快照（system+threads）主线程循环
  - L2 工具：vmstat/mpstat/iostat/pidstat/numastat + perf stat + 设备循环
  - L3 授权：perf record -F 99 -g + report + script（60s/256MB 上限）
  - 结束产物：outdir 目录树 + manifest.json + case_id.tar.gz（可选）
"""

from host_bound.collector.main import CollectParams, CollectorOrchestrator

__all__ = ["CollectParams", "CollectorOrchestrator", "run_collect"]


def run_collect(params, logger=None):
    """便捷入口：构造编排器并执行完整采集流程。

    返回 dict，字段见 CollectorOrchestrator.run()。
    """
    return CollectorOrchestrator(params, logger=logger).run()
