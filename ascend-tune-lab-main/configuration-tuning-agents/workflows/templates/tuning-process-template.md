# Phase 2 调优产物模板（唯一）

本文件是 Phase 2 **唯一模板**：同时承载

1. **中间落盘物** `tuning-process.md` / `tuning-process.json`（过程值，必填）
2. **汇总状态** `tuning-status.md`（摘要，必填）

上述文件均落在**工作目录**内的 `{case_dir}/tuning/`（见主流程「报告落盘约定」）。Agent / 编排脚本只 Read 本模板即可。

## 落盘路径

```text
{case_dir}/tuning/tuning-process.json   # 中间产物 · 机器可读（过程值）
{case_dir}/tuning/tuning-process.md     # 中间产物 · 可读报告（过程值）
{case_dir}/tuning/tuning-status.md      # 汇总状态（指向过程产物）
```

## 写者 / 读者

| 角色 | 关系 |
| --- | --- |
| **写者** | `orchestrate_parallel_tuning.py`（`write_tuning_process_report` + `write_tuning_status`） |
| **读者** | `serving-tuning-subagent` / primary / 用户核对过程 |

---

## 一、中间产物结构（`tuning-process.*`）

### Markdown（须含 A–D；其中 C 必须含算子核心路径与算子耗时）

```markdown
---
producer: serving-tuning-subagent
phase: 2
artifact: tuning-process
---

# Phase 2 中间过程报告（并行策略 / KV / SLO）

- case_dir: `{case_dir}`
- 机器可读副本: `tuning/tuning-process.json`

## A. 并行策略过程值
### A.1 公式
### A.2 输入（model / quant / num_npus / params_b / input_len / output_len …）
### A.3 计算结果（weight_gb / min_tp / world_size）
### A.4 合法组合过程表（label / DP / TP / EP / weight_per_npu / process）

## B. KV 内存并发上限过程值
### B.1 公式
### B.2 输入（kv_bytes_per_token / input+output / memory_defaults）
### B.3 各并行策略过程表（available_kv / per_request_kv / max_concurrency_memory / process）

## C. SLO 并发过程值
### C.1 公式
### C.2 输入 / 数据源（TTFT/TPOT、mtp_accept_rate、main_op_ratio、num_hidden_layers、perf_db_source、used_real_csv、perf_db_fallback_reasons）
- ITL 公式：`(Σ 主要算子 decode 耗时 × num_hidden_layers) / main_op_ratio`（`main_op_ratio` 默认参考 0.7）
- TPOT 公式：`ITL / (1 + mtp_accept_rate)`
### C.3 算子核心路径（vllm-ascend）
- family / use_mla / use_sparse / use_compress / selected_backend
- core_ops / tc_ops / moe_kernels / comm_kernels
- evidence_files / dispatch_hints / arch_sources / analysis_mode
-（可选）matched_model_files
### C.4 算子耗时数据（msmodeling @ 推荐组合 max_concurrency_slo）
- 查表上下文：label / DP/TP/EP / concurrency / perf_db_source / used_real_csv
- 过程表（必填列）：
  | op | phase | tokens | latency_us | scaled_latency_us | source | used_real_csv | kernel_types | fallback_reason |
- 各 kernel detail（可选展开）：kernel_type / latency_us / source / csv 路径
### C.5 各并行策略过程表（mem_cap / max_concurrency_slo / tpot|itl|ttft@max / process）

## D. 推荐（由上述过程得出）
```

### JSON 顶层键（须含）

```json
{
  "producer": "serving-tuning-subagent",
  "phase": 2,
  "artifact": "tuning-process",
  "case_dir": "...",
  "parallel_strategy_process": { "formula": {}, "inputs": {}, "computed": {}, "combinations": [] },
  "kv_memory_concurrency_process": { "formula": {}, "inputs": {}, "combinations": [] },
  "slo_concurrency_process": {
    "formula": {},
    "inputs": {},
    "operator_core_path": {},
    "operator_latency": {
      "context": {},
      "ops": []
    },
    "combinations": [],
    "recommended": {}
  }
}
```

要求：

- `operator_core_path`：来自 `code-path.json` / `slo-concurrency.code_path` 的核心调度路径字段（不可省略）。
- `operator_latency.ops[]`：推荐组合在 `max_concurrency_slo` 下的逐算子查表结果（含 `latency_us` / `scaled_latency_us` / `source` / `used_real_csv`）；若非真实 CSV，须带 `fallback_reason`。
- 每条 `combinations[]` 应带可读 `process` 字符串，便于核对逐步计算。

---

## 二、汇总状态结构（`tuning-status.md`）

摘要即可；**过程细节以第一节中间产物为准**（算子路径与耗时明细在 `tuning-process.md` §C.3/C.4）。

```markdown
---
producer: serving-tuning-subagent
phase: 2
status: completed
skill: serving-parallel-strategy-tuning
---

# 服务化调优状态（并行策略）

- case_dir: `{case_dir}`
- perf_db_source:
- used_real_csv:
- selected_backend:
- process_report: `tuning/tuning-process.md`

## 0. 算子耗时数据源（必须优先真实 CSV）
- required: msmodeling_csv
- actual / used_real_csv / fallback_reasons（非真实时必填）

## 1. SLO 约束
## 2. 源码仓库与代码路径（摘要：backend / core_ops / evidence_files）
## 3. 并行策略组合（摘要）
## 4. KV Cache 容量（摘要）
## 5. SLO 并发（摘要）
## 6. 推荐配置
## 7. 产物路径
- tuning/tuning-process.md
- tuning/tuning-process.json
- tuning/parallel-strategies.json
- tuning/kv-capacity.json
- tuning/slo-concurrency.json
- tuning/code-path.json
- …

> 离线估算；算子核心路径与耗时过程值见中间产物 `tuning-process.md` §C.3 / §C.4。
```
