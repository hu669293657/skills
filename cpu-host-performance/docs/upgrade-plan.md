# CPU Host 升级审查与执行计划（2026-11-03）

## 审查结论
- 仓库位于 D:/skills/cpu-host-performance，Git 根 D:/skills，初始 git status 干净。无 AGENTS.md；不复制仓库、不提交、不回滚。
- shell 默认全量（13 类事件），minimal 仅 sched_switch。cpu_frequency 已列入全量，但不存在/未启用/无转换/采集丢失混为一谈。
- parser 读取 trace 与 hostbound 快照；Event.pid 为事件上下文任务，sched_switch prev_pid/next_pid 为 TID。旧 task 汇总没有 TGID 生命周期隔离。
- metrics 将 waking 与 wakeup 混算，30 秒老化会丢弃真实长等待；迁移显式与推断重复计数；swapper idle 起点错误。
- IRQ/SoftIRQ sum(time)/window 被称为全机占比，分母缺 CPU 观测时间；缺失事件被填零。HTML core 图 [:64]，频率图 [:32]。
- HostBound 仅 NPU 直接 PID 加指定 PID 的采集后快照；无祖先/子进程、start/end 线程身份和 quota 增量。
- reports 13 节固定；第二节只含表，解释藏在第13节；NPU affinity 模块已有严格证据和安全建议，保持兼容。
- 基线 python -m unittest discover -s tests -v：16/16。

## 实现设计
使用 Python3.7+ 标准库；保留 shell 唯一入口，旁路 Python 采集 helper 可选，缺 Python 时明确降级。增加 JSON schema_version=2 输出。

## 顺序与文件
1. Frequency: scripts/host_snapshot.py（只读 freq policy/进程快照 helper），analyzer/frequency_analysis.py；脚本记录 capability 与轮询。tests/test_frequency.py。
2. Discovery: analyzer/process_analysis.py；/proc 全机摘要与相关进程全线程，root/NPU/祖先子树/cgroup 关系及 lifecycle 身份分段。tests/test_process_discovery.py。
3. Contention: analyzer/scheduling.py、contention_analysis.py；配对运行/等待、身份隔离、同核候选、quota 增量；修复 metrics 聚合。tests/test_contention.py、test_metric_accuracy.py。
4. Balance: analyzer/core_balance.py + assets/core_balance.js（生成时内嵌）；全部CPU/NUMA/范围/指标/图形切换，静态 SVG 与表格后备。tests/test_reports.py。
5. Explanation: analyzer/report_explanation.py、report_sections.py；六指标动态中文解释，频率/发现/竞争证据，HTML/MD 一致。report.py/report_html.py 接入。
6. Integration: parser.py 新 sidecar 加载和字段；diagnosis.py 新证据/限制/置信度；analyze_cpu_trace.py/report.py 增加 JSON。
7. Fixtures: examples/generate_synthetic.py 生成256核8NUMA明确合成的采集包及报告；测试8/64/256、缺失数据、注入安全和浏览器交互。
8. Docs: SKILL.md、README.md、references/analysis_v2.md、docs/upgrade-validation.md；全部测试、bash语法/fixture mock、CLI归档回归、浏览器核验、独立最终审查。

## 全局约束与审查重点
- null 缺失与 0 分开；事件支持≠事件产生；窗口无转换≠降频；频率标准接口单位 kHz，显式单位转换。
- TID 不冒充 TGID；fork 无 clone 证据不猜 TGID；PID复用/事件丢失/边界样本标记，无法配对不生成等待。
- CPU 利用率/IRQ 使用实际可观测区间；缺失或解析错误降置信度。不得从全机低利用率排除局部瓶颈。
- 候选须可运行等待+同CPU运行重叠，affinity 仅辅助；Device 时钟/时间线不匹配不推断 NPU 因果。
- 不修改 affinity/IRQ/governor；不运行 PMU/训练；采集独立 trace instance、清理恢复、同名输出拒绝覆盖。

## 进度
- [x] 阶段一审查、基线16测试
- [x] 阶段二计划（用户已授权持续执行）
- [x] 阶段三频率
- [x] 阶段四发现
- [x] 阶段五竞争
- [x] 阶段六图表
- [x] 阶段七解释
- [x] 阶段八全部测试
- [x] 阶段九示例与浏览器
- [x] 阶段十文档
- [x] 阶段十一审查
