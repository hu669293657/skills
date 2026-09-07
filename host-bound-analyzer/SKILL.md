---
name: host-bound-analyzer
description: 深度学习训练/推理场景 CPU/Host Bound 性能瓶颈的离线诊断。当用户提供采集包（tar.gz 或 host_bound_collection 目录）要求分析，或要求在 Linux 训练机上现场采集并诊断宿侧瓶颈（线程过订阅、OMP 线程过多、NUMA 远端访问、iowait/磁盘饱和、内存回收、DataLoader 供数不足等）时使用。产出固定 18 章节零 JS 离线 HTML 报告。
version: 0.2.0
---

# host-bound-analyzer：CPU/Host Bound 性能诊断

对深度学习训练/推理进程做"宿侧（CPU/Host）是否拖慢加速器"的证据化诊断：
只读采集 → 规则引擎（YAML，缺数据即 SKIP，绝不虚构）→ 加权评分 → 根因树与 P0-P3 优化建议 → 固定 18 章节 HTML 报告（零 JS、零外部资源、内嵌 SVG）。

## 1. 触发条件

满足任一即启用本 Skill：

- 用户给出采集包：`*.tar.gz` 或含 `manifest.json` 的采集目录（`host_bound_collection_*`）。
- 用户要求现场采集：训练慢、怀疑 CPU/数据供给/宿侧瓶颈，希望采集并诊断。
- 用户给出原始文本（ps/top/iostat/vmstat/numastat/perf 输出、/proc 快照）要求诊断。
- 客户机拿不到项目目录/无法联网装依赖，希望只传一个脚本完成采集 → 走路径 C
  （交付 `bin/host_bound_standalone.py`）。

## 2. 环境要求

- 分析端：仅 Python ≥3.6 标准库，任意操作系统，离线可用。
- 采集端：Linux，只读 `/proc`、`/sys` 与无特权工具；无需 root，无需 pip。
- 运行方式：项目根目录下 `python -m host_bound <command>`。
- 单文件模式：客户机无法拿到整个项目目录时，直接传 `bin/host_bound_standalone.py`
  一个文件即可（内嵌完整包，运行时自解压到临时目录并自动清理，不留痕）；
  用法与 `python -m host_bound` 完全一致，见路径 C。

## 3. 禁令（必须遵守）

1. **不虚构数据**：规则依赖的任一特征缺失时该规则 SKIP（报告中体现为"部分规则因数据缺失未启用"），禁止凭经验补结论。
2. **L3 须显式授权**：`--level 3`（perf record 采样）必须在用户当前会话中明确同意后才可执行；默认只用 L1/L2。
3. **不修改采集原始数据**：分析只读；需要加工时写入新目录。
4. **默认脱敏**：对外回传/展示一律带 `--sanitize`（IP/主机名/用户名/路径/令牌）；未经用户确认不得外发未脱敏数据。
5. **不改动报告结构**：18 个固定章节的标题与顺序永不变化；不得向报告注入任何外部 JS/资源。
6. **不夸大结论**：退出码 2（数据不完整）时必须向用户说明局限；禁止在数据不完整时声称"已完全排除瓶颈"。
7. **结论必须挂证据**：所有诊断引用报告中的 Finding（含 metric/value/来源文件与行号，可回溯），不得脱离证据下诊断。

## 4. 工作流

### 步骤 0：自检

```bash
python -m host_bound version
# 期望输出：host-bound 0.2.0 (schema 1.0 / report 1.0 / rules 1.0)
```

### 路径 A：现场采集 + 诊断（用户要求直接在训练机上采）

1. 确认目标进程：向用户要 PID，或使用 `--auto-detect` 自动识别训练进程（PyTorch/MindSpore/vLLM 等）。
2. 一键采集+分析+出报告（默认 60 秒、1 秒间隔、L1、不打包）：

```bash
python -m host_bound run-collect --auto-detect --duration 60 --interval 1 --level 1 --sanitize -o ./hb_out
```

3. 从命令输出的 JSON 摘要取 `report.html` 路径与 `dq_grade`，用浏览器打开报告核对关键 Finding。
4. 若 `dq_grade` 非 complete：向用户列出缺失数据源，评估是否按 L2 重采（见 4.3）。

### 路径 B：用户提供采集包（离线回传，最常用）

1. 输入可为 tar.gz 或已解包目录（自动定位含 `manifest.json` 的根目录）。
2. 一键出报告：

```bash
python -m host_bound run <collection> -o report.html --sanitize
```

3. 若用户可能继续追问细节，改用分步以保留中间 JSON：

```bash
python -m host_bound analyze <collection> -o analysis.json --sanitize
python -m host_bound report analysis.json -o report.html
```

4. 需要先体检完整度时：

```bash
python -m host_bound validate <collection>
```

### 路径 C：单文件脚本交付客户自采（客户机拿不到项目目录时）

1. 交付物仅为一个文件 `bin/host_bound_standalone.py`（约 200 KB，内嵌完整包与规则，
   自校验 sha256，Python ≥3.6 标准库，无网/无 pip/无 root 均可运行）。
2. 让客户把脚本拷到训练机上执行（命令与 `python -m host_bound` 完全一致）：

```bash
python3 host_bound_standalone.py collect --auto-detect --duration 60 --level 1 --sanitize
```

3. 采集结果自动保存在客户执行目录：`host_bound_collection_<时间戳>/` 与同名
   `tar.gz`；客户将 tar.gz 回传后按路径 B 分析。
4. 若客户要求本机直接出结论，让客户执行（报告也在其本地生成）：

```bash
python3 host_bound_standalone.py run-collect --auto-detect --duration 60 --sanitize -o ./hb_out
```

5. 注意事项：
   - L3（`--level 3`）经单文件执行时**同样必须先获得用户显式授权**，禁令不变。
   - 首次运行前让客户先跑 `python3 host_bound_standalone.py version` 自检；
     输出四元版本号即脚本完整、Python 版本满足。
   - 需要现场微调规则时：`python3 host_bound_standalone.py --unpack ./hb_pkg`
     解出内嵌包后按 `PYTHONPATH=./hb_pkg python -m host_bound ...` 运行。
   - 修改了包源码或 `rules/*.yaml` 后，必须重跑 `python bin/build_standalone.py`
     重新生成单文件脚本，否则客户拿到的是旧逻辑。

### 采集分级决策

| 级别 | 内容 | 使用时机 |
|---|---|---|
| L1（默认） | 只读 /proc、/sys 快照 | 任何环境，零风险 |
| L2 | 追加 vmstat/mpstat/iostat/pidstat 与 perf stat（工具存在才用） | 需要更细粒度时序时 |
| L3 | perf record 采样（热点函数/IPC/cache-miss 归因） | **仅用户显式授权后**，且用户关心热点归因时提议 |

## 5. 退出码语义

| 退出码 | 含义 | Agent 动作 |
|---|---|---|
| 0 | 成功且数据完整（dq=complete） | 直接汇报结论 |
| 2 | 成功但数据不完整（dq 非 complete） | 汇报结论，同时明确列出缺失数据源与补采建议 |
| 1 | 失败 | 读取 stderr 定位（tar 损坏 / manifest 缺失 / 参数错误），修正后重试 |

## 6. 结果解读（向用户汇报的固定结构）

1. **总结论**：`classification`（如 Thread Oversubscription / Data Pipeline Bound / Unknown）+ `statement`。
2. **Host Bound Score 分档**：

| 分数 | 标签（score_label） | 含义 |
|---|---|---|
| 0–20 | 基本不存在 | 宿侧无明显拖慢 |
| 20–40 | 轻微 | 有轻度宿侧压力 |
| 40–60 | 疑似 | 建议按建议项优化并复测 |
| 60–80 | 较明显 | 宿侧瓶颈较明显 |
| 80–100 | 强 Host Bound | 宿侧严重拖慢加速器 |

3. **命中规则与证据**：逐条列出 rule_id、severity、confidence 与证据（metric/value/来源行号）。
4. **优先级建议**：P0–P3，区分"明确建议"与"建议实验验证"。
5. **数据质量**：grade、warnings、未启用规则清单。
6. **下一步**：优化后按同参数重采对比；或按需升级 L2/L3。

## 7. 输出物

- `report.html`：18 个固定章节（英文标题）、零 JS、内嵌 SVG、双击离线可开。
- `analysis.json`：带 `_versions` 的版本化中间结果，可重复渲染报告。
- 采集包：`manifest.json` + `capabilities.json` + system/ environment/ process/ threads/ numa/ tools/ perf/ device/ framework/ 各源目录。

## 8. 常见追问

- "某条规则为什么没触发？" → 用 `validate` 查缺失数据源；规则缺特征即 SKIP，不会半猜半算。
- "分数怎么算的？" → score = 100 × Σ(top5 贡献) / max(Σ top5 权重, 3.0)，贡献 = weight × SEV_FACTOR × confidence。
- "能看热点函数吗？" → 需要 L3 perf record（须用户授权后重采），当前包内无该数据时明确告知无法归因。
- "单文件脚本怎么来的/怎么更新？" → 由 `python bin/build_standalone.py` 生成
  （内嵌整包 + sha256 自校验）；改过任何源码或规则后必须重新构建。
- "客户机怎么确认脚本能跑？" → 先 `python3 host_bound_standalone.py version`，
  能打印四元版本号即环境与脚本均正常；然后按路径 C 采集。
