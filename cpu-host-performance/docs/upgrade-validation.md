# 升级最终验证

## 2026-11-05 单文件采集与直接绑核命令

本轮执行 `python -X utf8 -m unittest discover -s tests -v`：169 项全部通过，79.512 秒；skill-creator 格式校验、Bash 语法与 Git diff 空白检查通过。现场 scripts 目录仅保留 cpu_trace_collect.sh，LF 行尾，不需要 Python/jq/helper 文件。原 Python 采集 helper 与其专属测试已替换为单文件 Bash/awk 测试。

新增验证覆盖只带一个 sh、拦截 Python/jq 调用时的完整采集/归档/离线分析、主子进程命令、保留窄掩码线程、逐线程 cpuset 阻碍、自定义 cgroup 挂载、v1/v2 继承、长 uptime 精度、失败清单与频率时钟域。报告第3节代码块只包含具体 taskset 设置命令，已同步示例 HTML/MD、模板和规则。

测试平台为 Windows / Git Bash 与合成 proc/sysfs/tracefs，未完成 openEuler/Kunpeng/Ascend 实机验证。以下保留此前升级记录。


源码位置：D:/skills/cpu-host-performance。初始Git工作区干净；保持原仓库开发，未克隆、回滚、自动commit或push。

## 实际测试

执行：`python tools/validate_upgrade.py --output <输出目录>`。
执行时间：2026-11-03T20:42:54.413686+08:00。Python3.14 / Windows / Git Bash。
实际运行 **85项，85通过，0失败、0错误、0跳过**，耗时 **16.304秒**。

| 测试模块 | 数量 | 结果 |
|---|---:|---|
| test_collection_shell | 4 | PASS |
| test_collector | 6 | PASS |
| test_contention | 19 | PASS |
| test_frequency | 11 | PASS |
| test_input_compatibility | 4 | PASS |
| test_metric_accuracy | 7 | PASS |
| test_npu_affinity | 16 | PASS |
| test_process_discovery | 11 | PASS |
| test_reports | 7 | PASS |

另外：技能格式校验（UTF-8模式）、Python编译、Python3.7语法、Bash语法、JavaScript语法、git diff --check均通过。3.7只有语法验证，未在本机以3.7运行。

## 浏览器核验

全部256核折线、NUMA4的32核柱图、指标显隐、独立单位组、128–135范围、CPU128提示、空选择提示、390px窄屏均已实际操作。窄屏修复后clientWidth与body.scrollWidth均为375；JS错误列表为空。移除全部脚本后，仍有256静态点及256行核心表。

限制：file://被浏览器工具URL策略拒绝，未绕过；实际页面交互使用127.0.0.1。因此只声明单文件离线内容/依赖验证，不声称file协议自动化打开通过。print CSS已检查，未实际物理打印。

## 合成示例

明确SYNTHETIC_TEST_DATA；10秒、256CPU、8NUMA、8NPU，6166事件全部解析；8频率policy、277线程身份与8组同核竞争候选。
平均利用率10.4582%、最高100%；80个成功wakeup样本的P99=39ms；硬中断全机0.09375%/单核最高20%，软中断全机0.14453125%/单核最高25%。
Worker/NPU链：NPU i→TGID4000+i→TID5000+i→CPU32*i→NUMA i；候选TID9000+i。所有候选因果均为UNPROVEN。/ascend/job配额4核，nr_throttled增量4，throttled_usec增量80000；不能等价于整机饱和或某一个Worker节流。

## 独立审查与前向测试

独立代码审查提出6项重要问题，均以复现测试修复：跨PID代次配对、根cgroup误关联、双生命周期事件重复、退出后Runtime虚增、坏数值崩溃、窗口扫描复制开销。对应回归见下表。
另一个独立agent仅用SKILL及原始归档完成离线CLI前向测试，确认HTML/MD/JSON、数据链与合成标记；发现的GIL/msprof/function_monitor覆盖遗漏已补充NOT_PROVIDED/NOT_RUN说明并添加回归。Windows管道编码使用文档化`python -X utf8`，未修改系统设置。

## 兼容性与尚需现场验证

已验证标准库离线链路、旧NPU/NUMA全部16项回归、8/64/256核、模拟/proc/sysfs/cgroup和fake tracefs全量/精简采集。没有运行真实Linux采集。
待实机验证：Kunpeng920/openEuler22.03SP4/Ascend910B4的事件支持、权限、CPUFreq驱动语义、NPU输出版本、trace mono与Device同域、buffer丢失率和实际开销/优化收益。PMU保持NOT_VALIDATED。无法确认内核实际不支持的事件名单，按capabilities逐项报告；sched_stat_wait默认只探测。
开销与降级详见README及references/analysis_v2.md；私有trace instance不支持则安全停止，不改全局tracing。Affinity成对比较最多512活跃关联线程并显示截断范围；全线程数据仍保留。

## 逐项结果

# 逐项测试执行结果

执行时间：2026-11-03T20:42:54.413686+08:00

Python 3.14.7；Windows-11-10.0.26200-SP0

实际运行 85 项；失败 0；错误 0；跳过 0；用时 16.304 秒。

| 测试名称 | 测试目的 | 实际结果 | 是否通过 | 未通过原因 |
|---|---|---|---|---|
| `test_collection_shell.CollectionShellTests.test_bash_syntax` | Bash采集脚本语法检查 | PASS | 是 | — |
| `test_collection_shell.CollectionShellTests.test_existing_output_rejected_without_modification` | 拒绝覆盖已有采集目录并保留用户文件 | PASS | 是 | — |
| `test_collection_shell.CollectionShellTests.test_fake_tracefs_full_and_minimal_capability` | 模拟tracefs全量/精简开启能力、丢失计数、导出和实例清理 | PASS | 是 | — |
| `test_collection_shell.CollectionShellTests.test_help_and_configuration_without_root` | 无需root即可查看完整参数帮助 | PASS | 是 | — |
| `test_collector.CollectorTests.test_cgroup_v1_throttling_nanoseconds_to_microseconds` | v1配额和节流纳秒显式转微秒 | PASS | 是 | — |
| `test_collector.CollectorTests.test_missing_policy_interfaces_empty` | 没有CPUFreq接口时返回空数据 | PASS | 是 | — |
| `test_collector.CollectorTests.test_proc_all_threads_and_cgroup_ancestors` | 模拟/proc全线程、stat括号、Affinity和cgroup祖先读取 | PASS | 是 | — |
| `test_collector.CollectorTests.test_shared_policy_frequency_fallback` | cpuN/cpufreq兼容路径共享policy去重 | PASS | 是 | — |
| `test_collector.CollectorTests.test_stat_paren_and_units` | 包含右括号的comm仍正确提取starttime、CPU和ticks | PASS | 是 | — |
| `test_collector.CollectorTests.test_unreadable_or_exited_proc` | 权限不足或退出进程不会造成采集崩溃 | PASS | 是 | — |
| `test_contention.ContentionTests.test_affinity_overlap_not_enough` | 仅Affinity交集不生成竞争候选 | PASS | 是 | — |
| `test_contention.ContentionTests.test_average_low_hot_core_and_wait_remain_visible` | 低全机均值不掩盖单核饱和和Worker等待 | PASS | 是 | — |
| `test_contention.ContentionTests.test_cgroup_delta` | v2配额及nr_throttled和微秒节流增量 | PASS | 是 | — |
| `test_contention.ContentionTests.test_device_clock_mismatch_no_correlation_claim` | Device时钟不同域时不声称时间相关 | PASS | 是 | — |
| `test_contention.ContentionTests.test_exit_without_final_switch_does_not_run_to_window_end` | 缺最终switch时运行时间在已观测退出处截断 | PASS | 是 | — |
| `test_contention.ContentionTests.test_explicit_migration_not_double_counted` | 显式迁移与跨核运行推断不重复相加 | PASS | 是 | — |
| `test_contention.ContentionTests.test_full_cpu_with_low_wait_not_contention_diagnosis` | 高CPU且等待很低不触发Worker竞争诊断 | PASS | 是 | — |
| `test_contention.ContentionTests.test_high_util_alone_no_contender` | CPU满载但无等待时不能单独形成竞争候选 | PASS | 是 | — |
| `test_contention.ContentionTests.test_loss_marks_observed_wait_partial` | buffer丢失使等待/候选置信度降级 | PASS | 是 | — |
| `test_contention.ContentionTests.test_missing_switch_does_not_fabricate_runtime` | 不连续switch不补造运行时间 | PASS | 是 | — |
| `test_contention.ContentionTests.test_overlap_scan_does_not_copy_unrelated_tail` | 窗口索引扫描不复制其后全部区间，避免二次方开销 | PASS | 是 | — |
| `test_contention.ContentionTests.test_pending_wakeup_at_exit_discarded` | 任务退出后的未完成唤醒不生成延迟 | PASS | 是 | — |
| `test_contention.ContentionTests.test_pid_reuse_cannot_pair_wakeup_across_generations` | 不能跨PID代次把旧唤醒配到新任务 | PASS | 是 | — |
| `test_contention.ContentionTests.test_runnable_switch_out_is_separate_wait` | R/R+等待与成功唤醒P99分开统计 | PASS | 是 | — |
| `test_contention.ContentionTests.test_two_npu_workers_share_one_cpu` | 两个NPU Worker共用一核时保持各自TGID和NPU证据 | PASS | 是 | — |
| `test_contention.ContentionTests.test_wait_overlap_candidate_correlation_not_causality` | 只按等待和同核运行重叠列候选且标注因果未证实 | PASS | 是 | — |
| `test_contention.ContentionTests.test_waking_is_not_successful_wakeup` | 忽略waking起点并去重成功wakeup | PASS | 是 | — |
| `test_contention.ContentionTests.test_waking_only_cannot_create_latency` | 只有waking而无成功wakeup时P99不可用 | PASS | 是 | — |
| `test_contention.ContentionTests.test_worker_migration_wait_scope_splits_cpu` | 等待期间迁移会分段目标CPU上的竞争重叠 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_enable_failed` | 区分事件启用失败 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_enabled_without_changes_is_not_unavailable` | 事件已开启但无转换时分类NO_TRANSITION | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_explicit_units` | Hz、kHz、MHz显式换算与未知单位处理 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_invalid_frequency_event_is_collection_failure` | 坏频率样本分类为采集失败 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_lost_trace_is_not_no_transition` | 丢事件时不声称没有频率变化 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_minimal_not_requested` | 区分精简模式未请求频率事件 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_static_only_and_missing_not_zero` | 静态频率降级且缺失平均值不填零 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_sysfs_policy_not_duplicated` | 共享policy只统计一次sysfs样本并计算时间均值 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_trace_uses_cpu_id_and_time_weighting` | 使用payload.cpu_id定位策略域并按持续时间计算频率均值 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_unreadable_sysfs_failure_is_retained` | 保留不可读sysfs接口的原始失败原因 | PASS | 是 | — |
| `test_frequency.FrequencyTests.test_unsupported` | 没有可用频率接口时显示UNSUPPORTED | PASS | 是 | — |
| `test_input_compatibility.InputCompatibilityTests.test_chrome_lifecycle_fields_preserved` | Chrome/Perfetto导入保留生命周期字段与命名空间 | PASS | 是 | — |
| `test_input_compatibility.InputCompatibilityTests.test_chrome_malformed_numeric_event_is_skipped` | Chrome坏数值事件安全跳过并记录失败 | PASS | 是 | — |
| `test_input_compatibility.InputCompatibilityTests.test_jsonl_sidecar_never_parsed_as_trace` | 频率JSONL作为sidecar而不是误解析为trace | PASS | 是 | — |
| `test_input_compatibility.InputCompatibilityTests.test_static_sidecar_only_cli_degrades` | 没有trace时仍生成明确降级的HTML/MD/JSON | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_all_online_cpus_visible_unknown_not_busy` | 全部256在线CPU保留，静默核利用率为未知 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_idle_only_trace_preserves_utilization` | 仅cpu_idle输入仍可降级算利用率而切换率不可用 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_idle_switch_direction` | 修复无cpu_idle时idle进入退出方向 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_irq_denominator_observed_cpu_time` | IRQ全机分母使用总有效CPU观测时间并保留单核最大值 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_malformed_numeric_event_is_counted_not_fatal` | 坏数值事件计入解析失败且不让分析崩溃 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_missing_irq_exit_marks_quality_partial` | 缺失IRQ exit不伪造占比并记录未配对 | PASS | 是 | — |
| `test_metric_accuracy.MetricAccuracyTests.test_missing_irq_not_zero` | 缺少IRQ事件保留null而不是0% | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_already_confined_per_thread_masks_are_preserved` | 既有正确逐线程Affinity不被建议重复改动 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_collection_wrapper_preserves_hostbound_snapshots_in_archives` | tar/zip封装目录保留HostBound证据 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_cpu_outside_topo_affinity_can_still_be_on_the_local_numa` | 不将同NUMA但拓扑范围外CPU误判为远端NUMA | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_cpuset_is_a_hard_boundary` | 绑核建议遵守有效cpuset硬边界 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_disjoint_logical_pools_with_shared_smt_cores_are_blocked` | 逻辑区间互斥但共享SMT物理核时阻止冲突方案 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_ftrace_observations_include_threads_outside_top_eight` | NPU观测CPU包含Top8之外的线程 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_local_last_cpu_does_not_prove_affinity_is_confined` | PSR本地不能证明Affinity已收敛 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_missing_thread_or_host_evidence_blocks_actionable_commands` | 缺宿主机或全线程证据不生成可执行设置命令 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_missing_topology_yields_unknown_instead_of_a_guessed_numa` | 缺拓扑时NUMA未知，不从卡号猜测 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_multiline_npu_process_table_and_empty_card` | 兼容多行NPU进程表与空卡 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_npu_mismatch_is_a_configuration_risk_in_diagnosis` | NPU亲和性偏离只作为配置风险而非性能因果 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_process_memory_is_aggregated_without_losing_chip_ids` | 多chip显存聚合保留chip标识 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_report_merges_cause_and_moves_advice_before_details` | 保留13节结构与第二节证据、第三节建议顺序 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_same_pid_on_multiple_npus_is_not_bound_twice` | 同PID占多NPU不生成相互矛盾的重复绑定 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_shared_affinity_is_split_without_leaving_advertised_cpus` | 共享Affinity分区不越过拓扑CPU范围 | PASS | 是 | — |
| `test_npu_affinity.NpuAffinityTests.test_smt_siblings_stay_in_one_partition` | SMT同胞保持同分区 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_comm_hint_does_not_override_acl_role_with_api_cmdline` | API命令行不覆盖关联ACL线程的角色证据 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_fork_and_newtask_one_identity_hex_clone_flags` | 双出生事件合并且十六进制clone标志正确映射线程 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_multinpu_and_independent_helper` | 多NPU Worker与独立辅助进程保留不同归属 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_parent_children_dataloader` | 上溯launcher及识别DataLoader子进程关联 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_pid_reuse_no_silent_merge` | 不同starttime的同TID保持不同身份 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_python_alone_unknown` | 独立Python进程不自动当作训练进程 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_root_cgroup_is_not_workload_relationship` | 根cgroup的无关任务不被关联成工作负载 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_root_pid_selects_children_without_npu` | 无NPU数据仍可按用户根PID发现子进程 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_short_lived_task_unknown_tgid` | 短命fork任务保留生命周期但不猜TGID | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_system_ancestor_does_not_select_unrelated_siblings` | 追溯sshd祖先时不扫入无关兄弟进程 | PASS | 是 | — |
| `test_process_discovery.ProcessDiscoveryTests.test_tid_maps_to_npu_tgid` | 正确建立NPU PID到TGID/TID映射 | PASS | 是 | — |
| `test_reports.ReportTests.test_all_core_sizes_and_numa` | 8/64/256核全部展示并识别8NUMA及静态后备 | PASS | 是 | — |
| `test_reports.ReportTests.test_html_safe_embedding` | script闭合、特殊字符和Unicode分隔符安全转义 | PASS | 是 | — |
| `test_reports.ReportTests.test_missing_metric_null` | 缺失指标在图表数据中保留null | PASS | 是 | — |
| `test_reports.ReportTests.test_missing_wakeup_source_stays_null` | 缺少唤醒事件时全机唤醒数和报告保持缺失而非零 | PASS | 是 | — |
| `test_reports.ReportTests.test_offline_report_no_fake_zero_or_external_scripts` | 离线报告无外部脚本且缺失中断不显示伪零 | PASS | 是 | — |
| `test_reports.ReportTests.test_optional_tool_coverage_explicit` | 缺失GIL、msprof与function_monitor在报告中明确披露 | PASS | 是 | — |
| `test_reports.ReportTests.test_six_explanations_in_section_two_both_formats` | 六项中文解释直接出现在HTML和MD第二节 | PASS | 是 | — |
