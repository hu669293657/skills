# msprof-analyze 原生 CLI 与 fallback 能力对照

## 原生 CLI（安装了 msprof-analyze 时优先）

```bash
msprof-analyze advisor {all|computation|schedule} <prof_dir> [-o <output>]
msprof-analyze compare -d <base_dir> -c <compare_dir> [-o <output>]
msprof-analyze cluster analysis -d <prof_dir> [-o <output>] [--top_num N]
msprof-analyze cluster analysis -d <prof_dir> --bp <标杆数据>   # 标杆对比（参数拼写以 `msprof-analyze cluster analysis --help` 实际输出为准）
```

- `advisor all`：全场景诊断（对应 P1）。
- `cluster analysis`：生成/更新 `cluster_analysis_output`（对应 P2），追加 recipe 名称可单跑某项进阶分析（对应 P3）。
- `compare`：双卡/双环境对比（对应 P4）。

## fallback 能力对照（未安装时使用，能力等价）

| 能力 | 原生实现 | 本技能 fallback |
|------|----------|-----------------|
| 输入检测 | 文档约定 | `scripts/detect_prof.py` |
| advisor all | `msprof-analyze advisor all` | `scripts/advisor_fallback.py`（全卡聚合） |
| 集群输出件 | `msprof-analyze cluster analysis` | `scripts/cluster_fallback.py`（同 schema db） |
| 进阶分析 | `msprof-analyze cluster analysis <recipe>` | `scripts/recipe_fallback.py`（正式 23 + 扩展 1 全量） |
| 性能比对 | `msprof-analyze compare` | `scripts/compare_fallback.py`（xlsx 中间件 → vendor prof-compare） |
| 泳道统计 | trace_view.json 人工分析 | `scripts/swimlane_analyzer.py`（流式解析） |
| 最终报告 | 无 | `scripts/report_generator.py` |

## 探测与回退流程

1. `msprof-analyze advisor --help` 探测（与 SKILL.md 执行规则一致）；可用 → P1/P2/P3/P4 优先原生 CLI。
2. 原生命令失败（数据格式不支持、缺依赖）→ 立即回退对应 fallback 脚本，不要中断流程。
3. fallback 产出的 `cluster_analysis_output/cluster_analysis.db` schema 与原生一致，可被 vendor cluster-analysis 直接消费。
