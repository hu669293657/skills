# host-bound-analyzer

深度学习训练/推理场景 **CPU/Host Bound 性能瓶颈离线诊断工具**：在 Linux 训练机上只读采集宿侧数据，回传后离线分析，自动生成固定 18 章节的零 JS 离线 HTML 报告。

核心特性：

- **零依赖**：仅 Python ≥3.6 标准库，分析端任意 OS，无需 pip / root / 网络。
- **两条路径**：Skill 现场自采（`run-collect`），或客户自采回传 tar.gz（`run`）。
- **三级自适应采集**：L1 只读 /proc、/sys；L2 追加系统工具与 perf stat；L3 perf record 需显式 `--level 3`。
- **规则引擎**：诊断规则全部外置在 YAML；依赖特征缺失时规则 SKIP，绝不虚构数据。
- **证据可回溯**：每条 Finding 附 metric/value/来源文件与行号。
- **固定章节报告**：18 章节英文标题、顺序永不变化；内嵌 SVG（折线/柱状/雷达/热力/时间线/根因树），双击即开。

## 1. 部署

- 依赖：仅 Python ≥3.6 标准库（tar.gz 解包在 Python <3.12 自动回退无 `filter` 参数的兼容路径）。
- 安装：无需安装，把项目目录放到任意位置，在该目录下执行 `python -m host_bound` 即可。
- 平台：分析端任意 OS；采集端仅 Linux。
- 单文件模式：客户机拿不到项目目录时，只传 `bin/host_bound_standalone.py` 一个文件
  （内嵌完整包与规则，运行时自解压到临时目录并自动清理，sha256 自校验，命令与
  `python -m host_bound` 完全一致），见下节。

## 2. 快速开始

路径 A（现场一键采集 + 诊断）：

```bash
python -m host_bound run-collect --auto-detect --duration 60 --interval 1 --level 1 --sanitize -o ./hb_out
```

路径 B（客户采集 → 回传 tar.gz → 离线诊断）：

```bash
# 客户机
python -m host_bound collect --auto-detect --duration 60 --level 1 --sanitize

# 回传 host_bound_collection_*.tar.gz 后，分析机
python -m host_bound run host_bound_collection_xxx.tar.gz -o report.html --sanitize
```

打开 `report.html` 即可阅读诊断结论；详细 Agent 工作流见 `SKILL.md`，总体设计见 `DESIGN.md`。

路径 C（单文件脚本：客户机拿不到项目目录/无网/无 pip 时）：

```bash
# ①（开发者，一次性）从整包生成单文件脚本
python bin/build_standalone.py --selftest

# ② 把唯一的产物 bin/host_bound_standalone.py 拷到客户 Linux 训练机，自检
python3 host_bound_standalone.py version
# 期望输出：host-bound 0.2.0 (schema 1.0 / report 1.0 / rules 1.0)

# ③a 客户只采集（结果保存到执行目录，回传 tar.gz 后按路径 B 分析）
python3 host_bound_standalone.py collect --auto-detect --duration 60 --level 1 --sanitize

# ③b 或客户机一键采集 + 本机直接出报告
python3 host_bound_standalone.py run-collect --auto-detect --duration 60 --sanitize -o ./hb_out
```

说明：

- 单文件脚本运行时把内嵌包解到系统临时目录并注册 `atexit` 自动清理，不留痕；
  首次运行自带 sha256 完整性校验，脚本被截断/篡改会拒绝启动。
- `python3 host_bound_standalone.py --unpack ./hb_pkg` 可把内嵌包解到指定目录，
  供现场微调 `rules/*.yaml` 后以 `PYTHONPATH=./hb_pkg python -m host_bound ...` 运行。
- 修改了包源码或规则后必须重跑 `python bin/build_standalone.py` 重新生成单文件脚本。

## 3. CLI 参考

| 命令 | 用途 | 关键参数（默认值） |
|---|---|---|
| `collect` | 现场采集，输出目录 + tar.gz | `--duration`(60) `--interval`(1) `--pid` / `--auto-detect`（互斥） `--level`(1) `-o` `--sanitize` `--no-tar` |
| `analyze` | 采集包 → analysis.json | `<collection>` `-o`(analysis.json) `--sanitize` |
| `report` | analysis.json → report.html | `<analysis>` `-o`(report.html) |
| `run` | 采集包 → 一键分析 + 报告 | `<collection>` `-o`(report.html) `--sanitize` |
| `run-collect` | 现场采集 + 分析 + 报告（不打包） | 同 `collect`（无 `--no-tar`） |
| `validate` | 采集包完整度体检 | `<collection>` |
| `version` | 打印版本号 | — |

退出码：`0` 成功且数据完整；`2` 成功但数据不完整（dq 非 complete，报告仍有效，需人工复核）；`1` 失败（tar 损坏 / manifest 缺失 / 参数错误等）。

## 4. 采集分级

| 级别 | 内容 | 约束 |
|---|---|---|
| L1 | 只读 /proc、/sys 快照（cpuinfo、meminfo、stat、vmstat、loadavg 等） | 默认，零风险 |
| L2 | 追加 vmstat / mpstat / iostat / pidstat 与 perf stat | 工具存在才采，逐源降级 |
| L3 | perf record + report（热点函数、IPC、cache-miss 归因） | 必须显式 `--level 3` 授权 |

## 5. 采集包结构

```
host_bound_collection_<case_id>/
├── manifest.json        # 采集参数、时间、目标进程、数据清单、数据质量
├── capabilities.json    # 环境探测：OS/arch/容器/可用工具
├── system/              # /proc、/sys 快照（proc_stat、cpuinfo、meminfo、loadavg…）
├── environment/         # uname、CPU 频率、内核参数
├── process/             # cmdline/environ/fd/进程树/target 摘要、ps 与 top 快照
├── threads/             # threads.csv（线程列表、亲和、状态）
├── numa/                # 节点拓扑、distance、numastat
├── tools/               # L2：vmstat/mpstat/iostat/pidstat 输出
├── perf/                # L2 perf stat / L3 perf report 输出
├── device/              # npu-smi / nvidia-smi（存在才采）
└── framework/           # 训练框架探测（PyTorch/MindSpore/vLLM）+ DataLoader 线程
```

`--sanitize` 对 IP / 主机名 / 用户名 / 路径 / 令牌统一脱敏。

## 6. 诊断引擎

**规则**：9 个 YAML（`host_bound/rules/`：host_bound、cpu_saturation、thread、scheduler、numa、memory、io、dataloader、perf），由自研 mini-YAML 子集解析（约 80 行，规则限定语法）；解析失败整体 fail-safe 降级为内置默认规则。

**评分**：

- 单条贡献 = `weight × SEV_FACTOR × confidence`，SEV_FACTOR = CRITICAL 1.0 / HIGH 0.85 / MEDIUM 0.6 / LOW 0.35 / INFO 0.15。
- `host_bound_score = 100 × Σ(top5 贡献) / max(Σ top5 权重, 3.0)`。

**分档**：

| 分数 | 标签 |
|---|---|
| 0–20 | 基本不存在 |
| 20–40 | 轻微 |
| 40–60 | 疑似 |
| 60–80 | 较明显 |
| 80–100 | 强 Host Bound |

**置信度**：特征级默认 `0.55 + 0.08 × 证据源文件数`（单源 0.63、双源 0.71），规则可显式指定；诊断整体置信度由命中规则与数据质量综合。

**数据质量**：按源完整度分级（complete / partial / incomplete），任一 Parser/Analyzer 异常被捕获并记入 warnings，不中断全流程；不完整时报告中明示"部分规则因数据缺失未启用"。

## 7. HTML 报告

18 个固定章节（顺序永不变化）：Executive Summary / Overall Diagnosis / Host Bound Assessment / CPU Analysis / Thread Analysis / Scheduler Analysis / NUMA Analysis / Memory Analysis / I/O Analysis / Host-Device Timeline / Framework Analysis / Profiler Analysis / Root Cause / Evidence / Optimization Suggestions / Risk and Impact / Collection Information / Raw Data Appendix。

零 JS、零外部资源（无 CDN、无字体外链）、SVG 全部内嵌，单文件离线可开，可归档审计。

## 8. 目录结构

```
host-bound-analyzer/
├── SKILL.md             # Agent 工作流定义（触发条件/禁令/步骤）
├── README.md            # 本文档（部署、使用、扩展）
├── DESIGN.md            # 总体设计文档
├── bin/
│   ├── build_standalone.py        # 单文件构建器（内嵌整包，--selftest 自检）
│   └── host_bound_standalone.py   # 生成的单文件可执行脚本（拷给客户即可）
├── host_bound/          # 主包（python -m host_bound）
│   ├── cli.py           # 7 个子命令
│   ├── models/          # Event / MetricSeries / Evidence / Finding / Diagnosis
│   ├── collector/       # 自适应采集（environment/system/process/thread/numa/tools/perf/framework/npu/sanitize/packer）
│   ├── parser/          # registry + proc/ps/thread/numa/perf/io/device/generic 解析器
│   ├── analyzer/        # cpu/scheduler/numa/thread/memory/io/perf/host_bound
│   ├── diagnosis/       # miniyaml/rules/evidence/scoring/engine/recommendation
│   ├── report/          # charts(SVG) / template(18 章节) / generator
│   ├── util/            # fsio / stats
│   └── rules/           # 9 个诊断规则 YAML
└── tests/               # 单元 + 端到端黄金案例
```

## 9. 规则扩展

在 `host_bound/rules/*.yaml` 中按以下格式追加或新建规则（示例摘自 `io.yaml`）：

```yaml
- rule_id: HB_IO_IOWAIT
  name: iowait 占比过高
  group: io
  severity: HIGH              # CRITICAL/HIGH/MEDIUM/LOW/INFO
  weight: 1.5
  contribution: host_bound
  condition: cpu.iowait_pct >= 20   # 数值比较表达式，特征名来自 FeatureSet
  impact_hint: 65
  diagnosis:
    type: IO_BOUND
    statement: CPU 大量时间在等待 IO，数据供给链路存在瓶颈
  recommendation:
    level: P1                 # P0-P3
    certainty: 较高           # 明确建议 / 建议实验验证
    action: 定位慢盘与冷读路径；做数据本地化、预热与顺序读优化
    expected: cpu.iowait_pct 下降至 5 以下
    verify: 重采后对比 iowait 与 io.await_ms
    risk: ""
```

注意事项：

- `condition` 引用的特征必须在采集解析后真实存在，缺失时该规则 SKIP（不会报错、不会虚构）。
- 语法为规则限定子集（见 `host_bound/diagnosis/miniyaml.py`），不支持任意 YAML 特性；解析失败整体降级为默认规则。
- 新增/修改规则后请运行测试回归（见下节），并在 `RULE_VERSION` 中体现语义变更。

## 10. 版本语义

四元独立版本号（`python -m host_bound version` 输出）：

| 变量 | 当前值 | 含义 |
|---|---|---|
| VERSION | 0.2.0 | 工具版本 |
| SCHEMA_VERSION | 1.0 | analysis.json / manifest 数据结构 |
| REPORT_VERSION | 1.0 | HTML 报告结构与 18 章节集 |
| RULE_VERSION | 1.0 | 规则语义与权重 |

## 11. 测试

```bash
python -m unittest discover -s tests -v
```

当前基线：56 个测试全部通过（2 个按平台跳过），含 8 个黄金案例的端到端断言（触发规则、评分公式、分类、置信度、18 章节 HTML 结构、零外部资源）。

## 12. 已知边界

- Host-Device Timeline 章节在 V0.1 为占位（时间线解析规划于 V0.2）。
- 插件 Plugin API 规划于 V0.3（见 DESIGN.md）。
- 采集仅支持 Linux；Windows/macOS 仅可作分析端。
- L3 采样对运行中的训练任务有轻微开销，务必经用户确认后使用。
