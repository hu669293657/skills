# vLLM-Ascend 服务化性能优化 · 工作流

本工作流由 `serving-perf-optimization` primary agent 强制 Read 并严格推进。

**当前版本范围**：Phase 0 配置门禁 → Phase 1 基线配置生成 → Phase 2 **离线并行策略调优**（入口 skill `serving-parallel-strategy-tuning` + 三子 skill）。

> 用户输入：先定 `workdir`（未指定则 `./workspace`），再在其中放 MD 配置（默认 `deploy-config.md`）。`## 基本参数` 必填，`## 服务化配置` / `## SLO约束` 可选。格式见 [`references/user-config-format.md`](references/user-config-format.md)。

## 工作目录确定（Phase 0 最先执行）

| 优先级 | 来源 | 路径 |
| --- | --- | --- |
| 1 | 用户在消息中指定 `workdir` / 工作目录 | 用户给出的路径 |
| 2 | **默认** | 当前路径下的 **`workspace/`** |

若用户**未设置**工作目录：

1. 在当前路径创建 `workspace/`（已存在则复用）。
2. 将该目录记为 `workdir`，后续配置与全部报告均落于此目录下。
3. 向用户明确告知：`workdir = <cwd>/workspace`。

`case_dir` 默认也放在 `workdir` 内（如 `workspace/cases/<model>-<device>/`）。

## 报告落盘约定（硬要求）

**运行过程中生成的全部报告 / 中间产物 / 汇总状态，一律写在工作目录（`workdir`）下**（不得写到 `workdir` 之外）。

| 类别 | 路径（相对 `workdir`） |
| --- | --- |
| 用户配置 | `deploy-config.md`（或用户指定的 `config_md_path`，须在 `workdir` 内或复制归档到 `workdir`） |
| 模型 config | **`model_config.json`**（Phase 0 从 ModelScope 下载；失败则用户手供） |
| Case 根 | `{case_dir}/`（默认 `cases/<model>-<device>/`，须在 `workdir` 内） |
| Phase 1 | `{case_dir}/baseline/`（`baseline-launch.sh`、`baseline-summary.md` 等） |
| Phase 2 报告 | `{case_dir}/tuning/`（`tuning-process.md` / `.json`、`tuning-status.md`、各阶段 JSON） |
| 源码仓（独立） | **`repos/`**（`{workdir}/repos/vllm-ascend`、`{workdir}/repos/msmodeling`；跨 case 复用，不放在 `case_dir` 内） |
| 进度 | `{case_dir}/progress.md` |

Primary / Subagent 向用户回报时须给出 **`workdir` 内**的相对或绝对路径，便于直接打开核对。

## 流程总览

```text
+--------------------------------------------------------------+
| Phase 0 · 工作目录 + 配置文件 + 模型 config                     |
|    未指定 workdir → 创建并使用 ./workspace/                      |
|    默认 {workdir}/deploy-config.md；不存在则生成模板并停止     |
|    未填完 ## 基本参数 → 停止                                   |
|    ModelScope 下载 model_config.json；失败 → 警告并要求手供    |
+-----------------------------+--------------------------------+
                              |
                              v
+--------------------------------------------------------------+
| Phase 1 · 基线配置生成 【serving-baseline-reproduce-subagent】|
|    读取配置 → 匹配 baseline → baseline-launch.sh + baseline-summary.md   |
+-----------------------------+--------------------------------+
                              |
                              v
+--------------------------------------------------------------+
| Phase 2 · 并行策略调优 【serving-tuning-subagent】            |
|    SLO(读/问/默认) → clone vllm-ascend →                      |
|    find-possible-parallel-strategy → kv-capacity → slo-concurrency |
|    → tuning-process.* + tuning-status.md（均在 workdir {case_dir}/tuning/） |
+--------------------------------------------------------------+
```

## Subagent 映射

| Phase | Subagent | 状态 |
| --- | --- | --- |
| 1 | `serving-baseline-reproduce-subagent` | **已实现** |
| 2 | `serving-tuning-subagent` | **已实现（离线）** |

派发模板见 [`references/subagent-prompt-templates.md`](references/subagent-prompt-templates.md)。

## 执行规则

进入本工作流后建立 TaskList，**严格按 Phase 0 → 1 → 2 顺序**推进。

### 全局约束

- **workdir 默认**：未指定则创建并使用 `{cwd}/workspace`。
- **配置文件硬门禁**：`workdir` 无合法配置文件，或 `## 基本参数` 未填完 → **不得进入 Phase 1**。
- **模型 config 硬门禁**：Phase 0 必须拿到 `{workdir}/model_config.json`（优先 ModelScope 下载；失败则警告并要求用户手供）→ 缺失则 **不得进入 Phase 1**。
- **报告落盘硬门禁**：全部生成报告只写 `workdir` 内（见上文「报告落盘约定」）；禁止落到 `/tmp` 等目录作为交付物（测试临时文件除外且不得当作流水线产物交付）。
- **Phase 1 硬门禁**：无 `baseline-launch.sh` 与 `baseline-summary.md` 不得进入 Phase 2。
- **Phase 2 约束**：禁止部署/压测/改写 `baseline-launch.sh`；允许 clone 源码与离线估算脚本（clone 目标为 **`{workdir}/repos/`**）。
- **源码仓下载失败**：必须向用户给出警告（`clone-repos.json` 的 `warning` / `{workdir}/repos-clone.warning.md`），要求手动放置 `vllm-ascend` 与 `msmodeling` 后重试；不得静默跳过（测试 `--allow-without-repos` 除外）。
- **流水线终点**：Phase 2 产出 `{case_dir}/tuning/tuning-status.md` 与中间过程 `{case_dir}/tuning/tuning-process.md` 后结束。
- **进度文件**：`{case_dir}/progress.md` 记录各 Phase 状态。

### TaskList 骨架

```text
T0. 确定 workdir；定位/生成/校验 deploy-config.md（Phase 0）
T0b. 从 ModelScope 下载模型 config → {workdir}/model_config.json；失败则警告并停止，要求用户手供
T1. 确定 case_dir（默认在 workdir 下），可选复制 config 到 case 目录归档
T2. 派发 serving-baseline-reproduce-subagent（Phase 1）
T3. 用户确认低时延/高吞吐（若 Phase 1 回报双匹配）
T4. Primary 验收 baseline-summary.md
T5. 派发 serving-tuning-subagent（Phase 2）
T6. Primary 验收 tuning-process.md + tuning-status.md，流水线结束
```

## Phase 0 · 工作目录 + 配置文件

### 工作目录 `workdir`

| 优先级 | 来源 | 路径 |
| --- | --- | --- |
| 1 | 用户指定 | 消息中的 `workdir` / 工作目录路径 |
| 2 | 默认 | **`{当前路径}/workspace`**（不存在则 `mkdir -p` 创建） |

### 配置文件路径

| 优先级 | 来源 | 路径 |
| --- | --- | --- |
| 1 | 用户在消息中指定 | 用户给出的 `config_md_path` |
| 2 | 默认 | **`{workdir}/deploy-config.md`** |

### Primary 步骤

1. Read [`references/user-config-format.md`](references/user-config-format.md)。
2. **确定 `workdir`**：用户指定 → 否则创建并使用 `{cwd}/workspace`；告知用户实际 `workdir`。
3. 确定 `config_md_path`（见上表）。
4. **文件不存在**：在 `workdir` 生成模板并停止。
5. **文件存在但未填完**：列出缺失项并停止。
6. **校验通过后立刻下载模型 config（硬门禁）**：
   ```bash
   python configuration-tuning-skills/serving-parallel-strategy-tuning/scripts/download_modelscope_config.py \
     --workdir {workdir} \
     --config {config_md_path} \
     --json
   ```
   - 成功 → `{workdir}/model_config.json` + `{workdir}/model_config.fetch.json`，继续。
   - 已存在合法 `model_config.json` → 复用，不重复下载。
   - **失败** → 向用户给出脚本 `warning`（含尝试过的 ModelScope ID 与手供路径），**停止**，不得进入 Phase 1。
   - 用户手供后：将文件放到 `{workdir}/model_config.json`（可选在 `## 基本参数` 增加 `ModelScope模型ID: org/name`）再重新发起。
7. 记录 `workdir`、`config_md_path`、`model_config_path={workdir}/model_config.json`，进入 Phase 1。

### case_dir

- 由用户指定或 Primary 按配置内容派生；**默认** `{workdir}/cases/<model>-<device>-<quant>/`。
- 可选：复制配置文件到 `{case_dir}/config.md` 归档；可将 `model_config.json` 复制到 `{case_dir}/model_config.json`。

## Phase 1 · 基线配置生成

### Primary 派发

1. 传入已校验的 `config_md_path` 与 `case_dir`。
2. 按模板派发 `serving-baseline-reproduce-subagent`。
3. 产出须符合 [`templates/baseline-summary-template.md`](templates/baseline-summary-template.md)。

### Primary 验收

- Read `{case_dir}/baseline/baseline-summary.md`。
- 确认完整后进入 Phase 2。

## Phase 2 · 并行策略调优

1. 派发 `serving-tuning-subagent`（模板见 subagent-prompt-templates § Phase 2）。
2. Subagent 按入口 skill 执行：SLO → clone → 三子 skill。
3. Primary 验收 `{case_dir}/tuning/tuning-process.md`（中间过程）与 `{case_dir}/tuning/tuning-status.md`（`status=completed`）及推荐并行策略。
4. 流水线结束；交付物路径均在工作目录内：`{case_dir}/baseline/baseline-launch.sh` + `{case_dir}/tuning/` 下报告。
