# 用户配置文件格式

流水线 **必须** 以工作目录中的一份 MD 配置文件作为唯一场景输入（`config_md_path`）。Primary 在 Phase 0 定位/生成/校验该文件；**配置文件填写完成前不得进入 Phase 1**。

参考示例：[`configuration-tuning-skills/ascend-baseline-generator/config.example.md`](../../configuration-tuning-skills/ascend-baseline-generator/config.example.md)

## 默认路径

| 场景 | 路径 |
| --- | --- |
| 用户指定工作目录 | 用户给出的 `workdir` |
| 用户未指定工作目录 | **`{当前路径}/workspace`**（不存在则创建） |
| 用户未指定配置路径 | **`{workdir}/deploy-config.md`** |
| 用户指定配置路径 | 用户给出的相对或绝对路径 |
| 模型 config（Phase 0） | **`{workdir}/model_config.json`**（ModelScope 下载；失败则用户手供） |

**工作目录 `workdir`**：用户指定 → 否则当前路径下的 `workspace/`。  
**不是**「整个 git 仓库根」；仓库根仅用于读取 skills/workflows；运行产物一律进 `workdir`。

**落盘约定**：流水线运行过程中生成的配置副本、baseline 报告、调优过程报告与汇总状态，**全部写在 `workdir` 内**（详见 `workflows/serving-perf-optimization-workflow.md`「工作目录确定」与「报告落盘约定」）。

### 模型 config（Phase 0 硬门禁）

基本参数校验通过后，Primary **必须**从 ModelScope 下载对应模型的 `config.json` 到 `{workdir}/model_config.json`（脚本：`serving-parallel-strategy-tuning/scripts/download_modelscope_config.py`）。

| 情况 | 行为 |
| --- | --- |
| 下载成功 | 继续 Phase 1；后续并行/KV/SLO 优先读该文件 |
| 已存在合法 `model_config.json` | 复用，不重复下载 |
| 下载失败 | **警告用户**，要求手动提供该文件后重新发起；**不得进入 Phase 1** |

可选：在 `## 基本参数` 增加 `- ModelScope模型ID: org/model-name` 以提高命中率。

## 必填：`## 基本参数`

配置文件 **必须** 包含 `## 基本参数` 章节，且以下 7 项 **全部存在且有效**（键存在且冒号后有非空值）：

| MD 键名 | 对应字段 | 示例 |
| --- | --- | --- |
| 输入长度 | input_seq_len | 4096 |
| 输出长度 | output_seq_len | 1024 |
| 设备类型 | device_type | A3 |
| 模型名称 | model_name | Qwen3.5-27B |
| 量化格式 | quantization | w8a8 |
| NPU卡数 | num_npus | 1 | 物理卡数，与 baseline「总卡数」一致；A3 并行枚举时自动 ×2 die |
| 部署策略 | deploy_strategy | 单机混部 |

缺少章节、缺少任一项、或任一项值为空 → Primary **停止并提示补全**，**不得**进入 Phase 1，**不得**在对话中零散收集参数替代配置文件。

## 可选：`## 服务化配置`

`## 服务化配置` 章节 **可以没有**。若存在，应为 bash 代码块，用于覆盖匹配结果中的：

- 模型权重路径（`vllm serve` 第一个参数）
- `--host`
- `--port`

三项 **都** 提供时才覆盖；缺任一则 Phase 1 subagent 使用 baseline 文档默认值，并在 `baseline-summary.md` §4.2 标注 `overrides_applied: no`。

## 可选：`## SLO约束`

`## SLO约束` 章节 **可以没有**。用于 Phase 2 并行策略调优：

| MD 键名 | 字段 | 示例 | 缺省 |
| --- | --- | --- | --- |
| TTFT | ttft_ms | `<1s` | 不限制 |
| TPOT | tpot_ms | `<50ms` | **50ms** |
| 其他约束 | other | 自由文本 | 空 |

若节缺失或字段为空：Phase 2 **向用户询问**；用户仍不提供则应用上表缺省。

## 配置文件骨架

```markdown
# 部署配置

## 基本参数

- 输入长度: 4096
- 输出长度: 1024
- 设备类型: A3
- 模型名称: Qwen3.5-27B
- 量化格式: w8a8
- NPU卡数: 1
- 部署策略: 单机混部

## 服务化配置

```bash
vllm serve /path/to/model \
    --host 0.0.0.0 \
    --port 8000 \
    ...
```

## SLO约束

- TTFT: <1s
- TPOT: <50ms
- 其他约束:
```

（`## 服务化配置` / `## SLO约束` 整节可省略。）

## Phase 0 流程（Primary）

```text
1. 确定 workdir（用户指定 → 否则 mkdir -p ./workspace）
2. 确定 config_md_path（用户指定 → 否则 {workdir}/deploy-config.md）
3. 文件不存在？
   → Read workflows/templates/deploy-config.template.md
   → 写入 {config_md_path}
   → 告知用户填写后重新发起 → **停止**
4. 文件存在但未填完？
   → 列出缺失/空字段 → **停止**
5. 下载 ModelScope 模型 config → {workdir}/model_config.json
   → 失败：警告 + 要求手供 → **停止**
6. 校验通过且 model_config 就绪 → 进入 Phase 1
```

### Phase 0 校验清单

- [ ] 已确定 `workdir`（默认 `{cwd}/workspace`，已创建）
- [ ] 已确定 `config_md_path`（默认 `{workdir}/deploy-config.md` 或用户指定）
- [ ] 文件存在且可读（不存在则已生成模板并停止）
- [ ] 包含 `## 基本参数`
- [ ] 7 个基本参数字段均已解析且值非空
- [ ] `{workdir}/model_config.json` 已就绪（ModelScope 下载或用户手供）
- [ ] （可选）若存在 `## 服务化配置`，内容为 bash 代码块

校验通过后，将 `workdir`、`config_md_path`、`model_config_path` 与 `case_dir` 传入 Phase 1 subagent；可将配置文件复制到 `{case_dir}/config.md` 便于归档，**但以 `workdir` 中的原文件为匹配输入**。
