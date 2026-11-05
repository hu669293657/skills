# 默认完整采集与证据契约

此契约用于解释 Manifest、读取原始数据和判断是否复采。入口是 `sudo bash scripts/cpu_trace_collect.sh`，默认30秒全量事件、HostBound、NPU自动发现及全机进程/线程前后快照。安全快照仍受内核能力、工具和权限限制；完整采集指尝试并记录所有常规能力，不能承诺每台机器都有每项数据。

## 数据链

1. `cpu_trace_collect.sh` 探测环境，先保留只读快照，再创建私有 trace instance。
2. 同一个 sh 内的 Bash/awk 函数逐命令/接口记录状态，标准化 sysfs、proc、cgroup 和身份；现场无需 Python、jq 或其他脚本。
3. NPU 三个原始命令保存在 `hostbound/npu_smi_info{_start,}.txt`、`npu_smi_map.txt`、`npu_smi_topology.txt`，离线分析时由 `analyzer/npu_parser.py` 解析，采集端保留原文与返回码，不依赖 Python 生成 NPU 规范化副本。显式保留解析错误，不能把格式变化解释成没有任务。
4. `hostbound/numa_topology.json` 与 `hostbound/sysfs/node/nodeN/{cpulist,distance,meminfo}` 保存 NUMA 降级证据；lscpu、lscpu 扩展/可解析输出、numactl、numastat 保留原文。
5. `process_start/end.json` 保留所有可读进程与线程；所有进程保留 NUMA 页汇总，指定目标另保存 NUMA 原始文件与 numastat -p。`frequency_start/end.json`、`frequency_samples.jsonl` 和 `idle_start/end.json` 是各自的 sysfs 来源。
6. 根目录及 hostbound 的 Manifest 随整个 `cpu_trace/` 打包。`parser.py` 保留声明的原始文件和真实包内路径清单，不把 JSONL/vendor 原始输出误当 trace。
7. `collection_integrity.py` 对账声明、包内存在性及实际解析；`report_data.json` 保留原文、字段、来源与诊断；HTML 展示摘要和可展开完整数据。

Manifest schema_version=1，`entries` 每条包含：`category`、`requested`、`command`（argv数组或null）、`command_exists`、`returncode`、`raw_path`（相对 cpu_trace）、`raw_exists`、`empty`、`parse_success`、`parsed_count`、`started_at`、`ended_at`、`failure_reason`、`status`。允许附加 size_bytes、PID/starttime、item_errors 等。opaque 原文没有语义解析时 parse_success/count 为null；不能将非空文件本身视为已正确解析。

## 状态及行动

| 状态 | 含义 | 后续处理 |
|---|---|---|
| COLLECTED | 命令/接口取得有效原文；parse_success 单独说明语义验证 | 使用已有证据 |
| NO_ACTIVE_PROCESS | 有效空进程表/明确无活动任务 | 说明该窗口无任务，无需机械复采 |
| COMMAND_NOT_FOUND | 可选工具缺失 | 先核对工具/驱动，使用已有proc/sysfs降级 |
| PERMISSION_DENIED | 读取/执行权限不足 | 说明具体接口和权限/namespace条件 |
| UNSUPPORTED | 内核、驱动、硬件或选项不支持 | 使用其他可信来源，重复相同采集不能解决 |
| PARSE_FAILED | 原始数据存在但语义/格式无效 | 修复解析器后离线重跑 |
| NOT_REQUESTED | 本次明确关闭该类采集 | 必要时使用默认完整入口 |
| NOT_INCLUDED | Manifest 声明已生成的文件不在收到的包中 | 找回原始文件并修复打包 |
| EXECUTION_FAILED | 工具存在但执行失败/超时/身份变化 | 保留返回码/错误原文，解决具体失败原因 |
| UNKNOWN | 旧包没有请求/能力记录 | 不伪造成功或未请求，先审查原始目录 |

`metrics.collection_integrity.entries[].collector_status` 是原始声明，`status` 是收到证据后的对账结果。ftrace 单独列出事件存在性、解析统计与丢失状态；未知丢失不能当0。HostBound 单独列出各状态数量和缺失类别。`actions` 解释具体缺失、原因、可解决性及条件；只有明确未请求常规数据、实际事件丢失等可由复采补足的场景才生成复采命令。常规复采示例无需 `--hostbound`。

## 拓扑、身份与诊断约束

- CPU→NUMA 使用本次 sysfs/lscpu/numactl；节点可稀疏编号，不能用 CPU 数量或卡号补造。距离矩阵保留列对应节点；MemFree 和 MemAvailable 分开。
- NPU→CPU 需要明确 CPU Affinity 字段。只有 HCCS/PCIe 或 Logic/Physical ID 时映射未知；不输出猜测命令。
- 调度字段是 TID；TGID 来自 proc 或可证明的 clone 关系。使用 boot_id、starttime、进程/线程前后快照及生命周期隔离 PID 复用。未确认宿主机命名空间时不生成可执行方案。
- 当前 CPU、allowed CPUs、有效 cpuset、Mems_allowed 与实际 numa_maps 页分布有不同语义。各 TID 的 cgroup 可能不同，需要逐线程核对。
- 绑核计划只使用在线 CPU 与所有相关 TID 有效 cpuset 的交集，保留 SMT 完整组和 Worker 分区互斥。共享池没有完整运行/等待需求证据时不平均分配。任务已合规则保留掩码。报告给出直接 taskset 命令，执行时应确认身份、线程集合与 cpuset 仍与快照一致；直接命令本身不包含自动预检或回滚。
- CPU 绑定不迁移现有内存页。启动命令 `numactl --physcpubind=... --membind=... COMMAND` 仅在有独立内存策略证据时讨论，不作为运行 PID 的迁移命令。
- 允许跨节点、单次 PSR 或页分布偏离只是潜在风险。已分配页不等于远端访问流量；风险升级需要关联迁移、实际内存访问、等待及性能退化证据。建议不直接修改全局调度参数。
- AI HIGH 置信度来自稳定 NPU PID 归属；父子/cgroup/命令等关联分别标注 MEDIUM/LOW。高CPU或名称匹配不能单独认定 AI/竞争。竞争候选需要 AI 可运行等待与其他任务同 CPU 执行窗口交集，因果标记保持 UNPROVEN。
- Frequency 按 related_cpus/policy 去重，显式kHz→MHz，记录 trace/sysfs 来源和时钟对齐。Idle 的 C-state 编号仅表示状态；进入到退出的时长用于 Residency。`-1`、32/64位无符号最大值表示退出；未知区间为null/N/A。

## 验证边界

`python3 -m unittest discover -s tests -v` 包括假 proc/sysfs/tracefs、归档与报告端到端用例。`tests/offline_report_browser.js` 在已有 Playwright/浏览器环境验证离线交互；依赖不可用会明确跳过。Python 3.7 语法兼容检查不等于实际在该解释器/目标内核运行。发布报告时分别说明模拟验证、本机浏览器验证与 openEuler/Kunpeng/Ascend 现场验证。

单文件采集使用 `/proc/uptime` 的 boottime 时间戳；频率样本和进程快照声明 `clock=boottime`，优先选择 ftrace `boot` 时钟。内核不支持 boot 时钟时保留数据但不叠加不同域时间线。旧版 Python 采集包的 monotonic/mono 组合继续兼容。
