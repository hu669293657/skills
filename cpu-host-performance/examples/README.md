# 示例报告

`sample_report.html` 是当前分析器端到端生成的13节离线报告；`sample_bind_cpu.sh` 是同份合成数据生成的完整绑核脚本。仅作阅读示例，不能对真实服务器执行这些合成PID。MD报告已取消。

演示沿用用户提供的 8 张 NPU、PID 4115–4122 及四组 CPU Affinity；NUMA、SMT、线程 allowed CPU、PSR、boot_id 与 trace 均为显式构造的演示数据，不代表客户当前运行状态，示例命令不能直接用于真实业务。

第二节可查看按 NUMA 分组的双卡任务卡、宿主机 PID 和亲和状态；第三节可查看分区后的完整宿主机预检/备份/taskset/核验/回滚方案。报告骨架位于 `../templates/report_template.md`。

验证命令（Skill 根目录）：`python3 -m unittest discover -s tests -v`。
