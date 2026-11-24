# 示例报告

`custom_binding_assessment.json` 是离线定制规划器的**合成格式示例**，不代表真实服务器。运行 `python3 scripts/plan_custom_binding.py --input examples/custom_binding_assessment.json --output /tmp/binding_proposal.json`（从Skill根目录）仅输出建议；现场使用前替换全部拓扑、身份、负载和来源字段，按 [规划契约](../references/custom_binding.md) 核验。

[示例报告](sample_report.html) 和 [示例配置](bind_design.json) 来自同一次合成采集。报告第 3 节展示固定 [客户模板](../templates/cpu_binder_simple.py) 与对应配置。示例中的数字 ID 仅供离线阅读，模板运行时会直接操作对应的现场 ID。

演示构造 8 张 NPU、PID 4000–4007 和 8 组 CPU Affinity。NUMA、SMT、线程 allowed CPU、PSR、boot_id 与 trace 均为演示数据。

第二节展示 NUMA 亲和状态。第三节展示本次配置、执行条件及受阻的 PID。第四节展示合成内存、磁盘 I/O、挂载点及任务配额压力。第一节按严重程度展示业务线程排队、CPU 配额、中断、低频和绑核线索。[报告提纲](../templates/report_template.md)

第六节使用合成的系统和 CANN Runtime 版本，并展示 PID 4000 的启动变量、共享库路径和资源限额。

验证命令（Skill 根目录）：`python3 -m unittest discover -s tests -v`。
