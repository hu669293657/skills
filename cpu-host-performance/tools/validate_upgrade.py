#!/usr/bin/env python3
"""Run stdlib upgrade tests and save per-test outcomes without real collection."""
import argparse
import datetime
import io
import json
import platform
import sys
import time
import unittest
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))

PURPOSES = dict(line.split('|',1) for line in '''
test_trace_uses_cpu_id_and_time_weighting|使用payload.cpu_id定位策略域并按持续时间计算频率均值
test_enabled_without_changes_is_not_unavailable|事件已开启但无转换时分类NO_TRANSITION
test_unsupported|没有可用频率接口时显示UNSUPPORTED
test_minimal_not_requested|区分精简模式未请求频率事件
test_enable_failed|区分事件启用失败
test_sysfs_policy_not_duplicated|共享policy只统计一次sysfs样本并计算时间均值
test_static_only_and_missing_not_zero|静态频率降级且缺失平均值不填零
test_explicit_units|Hz、kHz、MHz显式换算与未知单位处理
test_lost_trace_is_not_no_transition|丢事件时不声称没有频率变化
test_invalid_frequency_event_is_collection_failure|坏频率样本分类为采集失败
test_unreadable_sysfs_failure_is_retained|保留不可读sysfs接口的原始失败原因
test_tid_maps_to_npu_tgid|正确建立NPU PID到TGID/TID映射
test_parent_children_dataloader|上溯launcher及识别DataLoader子进程关联
test_python_alone_unknown|独立Python进程不自动当作训练进程
test_multinpu_and_independent_helper|多NPU Worker与独立辅助进程保留不同归属
test_short_lived_task_unknown_tgid|短命fork任务保留生命周期但不猜TGID
test_pid_reuse_no_silent_merge|不同starttime的同TID保持不同身份
test_root_cgroup_is_not_workload_relationship|根cgroup的无关任务不被关联成工作负载
test_system_ancestor_does_not_select_unrelated_siblings|追溯sshd祖先时不扫入无关兄弟进程
test_fork_and_newtask_one_identity_hex_clone_flags|双出生事件合并且十六进制clone标志正确映射线程
test_root_pid_selects_children_without_npu|无NPU数据仍可按用户根PID发现子进程
test_comm_hint_does_not_override_acl_role_with_api_cmdline|API命令行不覆盖关联ACL线程的角色证据
test_high_util_alone_no_contender|CPU满载但无等待时不能单独形成竞争候选
test_waking_is_not_successful_wakeup|忽略waking起点并去重成功wakeup
test_wait_overlap_candidate_correlation_not_causality|只按等待和同核运行重叠列候选且标注因果未证实
test_runnable_switch_out_is_separate_wait|R/R+等待与成功唤醒P99分开统计
test_missing_switch_does_not_fabricate_runtime|不连续switch不补造运行时间
test_pending_wakeup_at_exit_discarded|任务退出后的未完成唤醒不生成延迟
test_explicit_migration_not_double_counted|显式迁移与跨核运行推断不重复相加
test_cgroup_delta|v2配额及nr_throttled和微秒节流增量
test_affinity_overlap_not_enough|仅Affinity交集不生成竞争候选
test_loss_marks_observed_wait_partial|buffer丢失使等待/候选置信度降级
test_pid_reuse_cannot_pair_wakeup_across_generations|不能跨PID代次把旧唤醒配到新任务
test_exit_without_final_switch_does_not_run_to_window_end|缺最终switch时运行时间在已观测退出处截断
test_overlap_scan_does_not_copy_unrelated_tail|窗口索引扫描不复制其后全部区间，避免二次方开销
test_worker_migration_wait_scope_splits_cpu|等待期间迁移会分段目标CPU上的竞争重叠
test_full_cpu_with_low_wait_not_contention_diagnosis|高CPU且等待很低不触发Worker竞争诊断
test_average_low_hot_core_and_wait_remain_visible|低全机均值不掩盖单核饱和和Worker等待
test_two_npu_workers_share_one_cpu|两个NPU Worker共用一核时保持各自TGID和NPU证据
test_waking_only_cannot_create_latency|只有waking而无成功wakeup时P99不可用
test_device_clock_mismatch_no_correlation_claim|Device时钟不同域时不声称时间相关
test_irq_denominator_observed_cpu_time|IRQ全机分母使用总有效CPU观测时间并保留单核最大值
test_missing_irq_not_zero|缺少IRQ事件保留null而不是0%
test_idle_switch_direction|修复无cpu_idle时idle进入退出方向
test_all_online_cpus_visible_unknown_not_busy|全部256在线CPU保留，静默核利用率为未知
test_malformed_numeric_event_is_counted_not_fatal|坏数值事件计入解析失败且不让分析崩溃
test_idle_only_trace_preserves_utilization|仅cpu_idle输入仍可降级算利用率而切换率不可用
test_missing_irq_exit_marks_quality_partial|缺失IRQ exit不伪造占比并记录未配对
test_missing_wakeup_source_stays_null|缺少唤醒事件时全机唤醒数和报告保持缺失而非零
test_optional_tool_coverage_explicit|缺失GIL、msprof与function_monitor在报告中明确披露
test_all_core_sizes_and_numa|8/64/256核全部展示并识别8NUMA及静态后备
test_missing_metric_null|缺失指标在图表数据中保留null
test_html_safe_embedding|script闭合、特殊字符和Unicode分隔符安全转义
test_six_explanations_in_section_two_both_formats|六项中文解释直接出现在HTML和MD第二节
test_offline_report_no_fake_zero_or_external_scripts|离线报告无外部脚本且缺失中断不显示伪零
test_shared_policy_frequency_fallback|cpuN/cpufreq兼容路径共享policy去重
test_proc_all_threads_and_cgroup_ancestors|模拟/proc全线程、stat括号、Affinity和cgroup祖先读取
test_unreadable_or_exited_proc|权限不足或退出进程不会造成采集崩溃
test_stat_paren_and_units|包含右括号的comm仍正确提取starttime、CPU和ticks
test_missing_policy_interfaces_empty|没有CPUFreq接口时返回空数据
test_cgroup_v1_throttling_nanoseconds_to_microseconds|v1配额和节流纳秒显式转微秒
test_bash_syntax|Bash采集脚本语法检查
test_help_and_configuration_without_root|无需root即可查看完整参数帮助
test_existing_output_rejected_without_modification|拒绝覆盖已有采集目录并保留用户文件
test_fake_tracefs_full_and_minimal_capability|模拟tracefs全量/精简开启能力、丢失计数、导出和实例清理
test_chrome_lifecycle_fields_preserved|Chrome/Perfetto导入保留生命周期字段与命名空间
test_jsonl_sidecar_never_parsed_as_trace|频率JSONL作为sidecar而不是误解析为trace
test_static_sidecar_only_cli_degrades|没有trace时仍生成明确降级的HTML/MD/JSON
test_chrome_malformed_numeric_event_is_skipped|Chrome坏数值事件安全跳过并记录失败
test_already_confined_per_thread_masks_are_preserved|既有正确逐线程Affinity不被建议重复改动
test_collection_wrapper_preserves_hostbound_snapshots_in_archives|tar/zip封装目录保留HostBound证据
test_cpu_outside_topo_affinity_can_still_be_on_the_local_numa|不将同NUMA但拓扑范围外CPU误判为远端NUMA
test_cpuset_is_a_hard_boundary|绑核建议遵守有效cpuset硬边界
test_disjoint_logical_pools_with_shared_smt_cores_are_blocked|逻辑区间互斥但共享SMT物理核时阻止冲突方案
test_ftrace_observations_include_threads_outside_top_eight|NPU观测CPU包含Top8之外的线程
test_local_last_cpu_does_not_prove_affinity_is_confined|PSR本地不能证明Affinity已收敛
test_missing_thread_or_host_evidence_blocks_actionable_commands|缺宿主机或全线程证据不生成可执行设置命令
test_missing_topology_yields_unknown_instead_of_a_guessed_numa|缺拓扑时NUMA未知，不从卡号猜测
test_multiline_npu_process_table_and_empty_card|兼容多行NPU进程表与空卡
test_npu_mismatch_is_a_configuration_risk_in_diagnosis|NPU亲和性偏离只作为配置风险而非性能因果
test_process_memory_is_aggregated_without_losing_chip_ids|多chip显存聚合保留chip标识
test_report_merges_cause_and_moves_advice_before_details|保留13节结构与第二节证据、第三节建议顺序
test_same_pid_on_multiple_npus_is_not_bound_twice|同PID占多NPU不生成相互矛盾的重复绑定
test_shared_affinity_is_split_without_leaving_advertised_cpus|共享Affinity分区不越过拓扑CPU范围
test_smt_siblings_stay_in_one_partition|SMT同胞保持同分区
'''.strip().splitlines())


class Result(unittest.TextTestResult):
    def startTest(self,test):
        self._begin=time.monotonic()
        super().startTest(test)
    def record(self,test,status,reason=''):
        name=test.id().rsplit('.',1)[-1]
        self.records.append({'name':test.id(),'purpose':PURPOSES.get(name,name),'result':status,'passed':status=='PASS','reason':reason,'seconds':round(time.monotonic()-self._begin,4)})
    def addSuccess(self,test):
        self.record(test,'PASS');super().addSuccess(test)
    def addFailure(self,test,err):
        self.record(test,'FAIL',self._exc_info_to_string(err,test));super().addFailure(test,err)
    def addError(self,test,err):
        self.record(test,'ERROR',self._exc_info_to_string(err,test));super().addError(test,err)
    def addSkip(self,test,reason):
        self.record(test,'SKIP',reason);super().addSkip(test,reason)


def main():
    ap=argparse.ArgumentParser(description=__doc__);ap.add_argument('--output',required=True)
    output=Path(ap.parse_args().output);output.mkdir(parents=True,exist_ok=True)
    stream=io.StringIO();Result.records=[];begin=time.monotonic()
    result=unittest.TextTestRunner(stream=stream,verbosity=2,resultclass=Result).run(unittest.defaultTestLoader.discover(str(ROOT/'tests')))
    elapsed=time.monotonic()-begin
    data={'executed_at':datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=8))).isoformat(),'python':sys.version,'platform':platform.platform(),'command':'python tools/validate_upgrade.py --output <output>','tests_run':result.testsRun,'failures':len(result.failures),'errors':len(result.errors),'skipped':len(result.skipped),'elapsed_seconds':round(elapsed,3),'tests':Result.records}
    (output/'unit_tests.log').write_text(stream.getvalue(),encoding='utf-8')
    (output/'test_results.json').write_text(json.dumps(data,ensure_ascii=False,indent=2),encoding='utf-8')
    lines=['# 逐项测试执行结果','', '执行时间：'+data['executed_at'],'', 'Python '+platform.python_version()+'；'+data['platform'],'', '实际运行 %d 项；失败 %d；错误 %d；跳过 %d；用时 %.3f 秒。'%(result.testsRun,len(result.failures),len(result.errors),len(result.skipped),elapsed),'', '| 测试名称 | 测试目的 | 实际结果 | 是否通过 | 未通过原因 |','|---|---|---|---|---|']
    for row in Result.records:
        lines.append('| `%s` | %s | %s | %s | %s |'%(row['name'],row['purpose'],row['result'],'是' if row['passed'] else '否',row['reason'].replace('|','\\|').replace('\n',' ') or '—'))
    (output/'test_results.md').write_text('\n'.join(lines)+'\n',encoding='utf-8')
    print('Tests: %d; failures: %d; errors: %d; skipped: %d; %.3fs'%(result.testsRun,len(result.failures),len(result.errors),len(result.skipped),elapsed))
    print(str(output/'test_results.md'))
    return 0 if result.wasSuccessful() else 1


if __name__=='__main__':sys.exit(main())
