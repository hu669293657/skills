# 示例报告

`sample_report.html` 是当前分析器端到端生成的 5 节离线报告；`sample_bind_cpu.sh` 是同份合成数据生成的完整绑核脚本。仅作阅读示例，不能对真实服务器执行这些合成PID。MD报告已取消。

演示构造 8 张 NPU、PID 4000–4007 和 8 组 CPU Affinity；NUMA、SMT、线程 allowed CPU、PSR、boot_id 与 trace 均为显式构造的演示数据，不代表客户当前运行状态，示例命令不能直接用于真实业务。

第二节展示 NUMA 亲和状态。第三节展示主进程的 taskset -apc 预览与执行入口，PID 4000 的现有线程掩码不同，因此标为待处理。第四节用一张图显示合成的内存、磁盘 I/O、挂载点及任务配额压力。第一节的「核心问题：调度延迟严重」按严重程度展示业务线程排队、CPU 配额、中断、低频和绑核线索。所有判断均由合成事件计算。报告骨架位于 `../templates/report_template.md`。

验证命令（Skill 根目录）：`python3 -m unittest discover -s tests -v`。
