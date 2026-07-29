# baseline-summary 模板

`baseline-summary.md` 是 **`serving-baseline-reproduce-subagent` 的结构化输出**，不是整条服务化调优流水线的终态报告。

下一环节 **`serving-tuning-subagent`（Phase 2 · 占位）** 应 **以本文件为输入** 读取上下文；primary 在 Phase 1 验收后派发 Phase 2 占位 subagent。Phase 2 当前不执行调优，仅写 `tuning-status.md`。

## 落盘布局

```text
{case_dir}/baseline/
├── baseline-launch.sh       # 可执行启动脚本（与 summary 配套）
├── baseline-summary.md      # 本模板 — Phase 1 结构化输出
└── config.used.md           # 匹配用 MD 配置副本
```

## 写者 / 读者

| 角色 | 关系 |
| --- | --- |
| **写者** | `serving-baseline-reproduce-subagent`（Phase 1） |
| **读者** | `serving-tuning-subagent`（Phase 2）；primary 做 Phase 1 验收 |
| **不读者** | 不应把本文件当作 Plan Dashboard、调优 round 记录或最终性能结论 |

**边界**：

- 常驻区 **§1–§5** 为下游必填输入；缺失则 Phase 2 subagent 应拒绝开工并向 primary 报错。
- **§6 附录** 仅为基线复现过程留痕（文档匹配、评分）；Phase 2 **可跳过不读**。

---

## 按下面结构落盘

复制以下骨架到 `{case_dir}/baseline/baseline-summary.md` 并填写。

```markdown
# Baseline Summary

> producer: serving-baseline-reproduce-subagent
> phase: 1

## 1. 场景标识

下游用本节锁定「为谁调优、在哪类硬件上跑」。

| 字段 | 值 |
| --- | --- |
| case_id | |
| model_name | |
| device_type | |
| quantization | |
| num_npus | |
| deploy_strategy | |
| input_seq_len | |
| output_seq_len | |

- matched_baseline_doc: （仓库相对路径）
- match_status: matched | unmatched

## 2. 已确认基线方案

- selected_profile: 低时延 | 高吞吐 | 低时延/高吞吐（合并节）
- profile_confirmed: yes | no
- selection_note: （用户确认或 primary 代确认的一句话；若 profile_confirmed=no，下游不得启动调优）

## 3. 负载与参考容量口径

来自 baseline 文档「典型测试用例」匹配行；**下一环节**据此设计压测/对比（非已测得的实测性能）。

| 字段 | 值 |
| --- | --- |
| context_len_max_model_len | |
| ref_avg_input | |
| ref_avg_output | |
| ref_parallelism | |
| ref_max_concurrency | |
| ref_request_rate | |
| ref_prefix_cache_hit_rate | （如有） |

## 4. 部署产物引用

不粘贴 `baseline-launch.sh` 全文，只给路径与关键字段摘要。

| 字段 | 值 |
| --- | --- |
| launch_script_path | baseline-launch.sh |
| config_used_path | config.used.md |
| model_weights_path | |
| served_model_name | （若 launch 脚本中可解析） |

### 4.1 关键 vLLM 参数摘要

| 参数 | 值 |
| --- | --- |
| max_model_len | |
| max_num_seqs | |
| max_num_batched_tokens | （如有） |
| tensor_parallel_size | |
| data_parallel_size | |
| quantization | |

### 4.2 用户配置文件覆盖（`## 服务化配置`，可选）

- serving_config_section_present: yes | no
- overrides_applied: yes | no
- notes: （无该节或未提供完整三项时：使用 baseline 文档默认值）

## 5. Phase 2 输入清单

Phase 2 subagent 进场先 Read 本节并核对。

- [ ] `match_status` = matched 且 `profile_confirmed` = yes
- [ ] `launch_script_path` 存在且与 §4 摘要一致
- [ ] §3 参考容量字段已填（尤其 `context_len_max_model_len`、`ref_max_concurrency`）
- [ ] 服务访问：`service_host`、`service_port` 已填（或明确标注「部署后回填」）

| 字段 | 值 |
| --- | --- |
| service_host | |
| service_port | |
| perf_goal_optional | （可选：吞吐/时延目标） |
| suggested_next_actions | proceed_to_phase2_parallel_tuning |

## 6. 附录：基线复现过程（可选）

仅供 Phase 1 subagent 留痕；**Phase 2 不必读取**。

### 6.1 五字段比对

| 字段 | 配置值 | 文档值 | 结果 |
| --- | --- | --- | --- |
| device_type | | | |
| model_name | | | |
| quantization | | | |
| num_npus | | | |
| deploy_strategy | | | |

### 6.2 双 Profile 对比（若曾存在双匹配）

| 维度 | 低时延 | 高吞吐 |
| --- | --- | --- |
| context_len | | |
| max_concurrency | | |
| match_score | | |

### 6.3 匹配扫描

- docs_scanned:
- docs_identity_matched:
- scoring_formula: (平均输入 - input)² + (平均输出 - output)² × 0.8
```
