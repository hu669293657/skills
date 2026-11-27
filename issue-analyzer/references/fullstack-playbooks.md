# 全栈故障定位 Playbooks

每个 Playbook 必须执行：①反推出故障阶段 ②列出 2–4 个竞争假设 ③使用跨层证据证伪 ④输出低风险验证命令 ⑤保留未知项。**关键词仅供分诊，不等于根因。**

## A. 拉不起 / 初始化失败（谁在启动阶段真正先退出？）

| 故障证据 | 首要核验 | 下一层排查 |
|---|---|---|
| `ImportError`、`cannot open shared object`、`undefined symbol` | Python/torch_npu/CANN/驱动/固件版本矩阵、`ldd`/动态库顺序、容器挂载 | 变更前后版本、ABI、环境变量 |
| `No such file`、`Permission denied` | 路径、属主、ACL、模型缓存/权重挂载 | NFS/对象存储、checkpoint 完整性 |
| `address already in use`、`gai error -2`、rendezvous | DNS/IPv4/IPv6 地址解析、MASTER_ADDR、端口监听、hostname 与 hosts | Gloo/c10d 控制面，不能判成 HCCL 故障 |
| `HCCL init failed`、`wait socket establish timeout` | 各端 src/dst rank 的其它异常，rank-table、collective/tag、一致性 | TLS/端口/网络、对端是否实际进入同一算子 |
| `OOM`/内存分配失败 | 分清 Host RSS/cgroup、NPU HBM、swap、文件缓存 | worker 数、TP/PP、max_model_len、KV cache |
| `op not supported`、编译失败 | 失败算子、shape/dtype/SoC、CANN 和框架版本 | 代码路径或适配能力 |

## B. 训练中断 / Hang

1. 建立 `rank | host | pid | device | 最后成功 step | 最后有进度时间 | 退出原因` 表；无各 rank stdout 时不能确定首停者。
2. 如果全体出现 HCCL 超时，先在 run/debug plog 中区分 `ERROR CQE`、`STUCK`、`LOST` 和行为不一致，回溯报告异常 rank、src/dst、tag、collective。
3. 如果某 rank 有 Python Traceback、Host OOM killer、SIGSEGV/SIGABRT、容器 OOMKilled、进程被调度回收，优先按此排查；其他 rank 的超时多是传播结果。
4. 如果某 rank 无错误但 step 停止：看 DataLoader、NFS、CPU starvation、deadlock、checkpoint 保存、算子未返回；`watchdog` 不直接证明通信链路损坏。
5. 无异常通信心跳但 collective/tag 不一致：对比各 rank 数据/分支、版本、梯度同步逻辑、AllToAllV 的各端计数/shape；必要时在已批准复现场景短时开启官方支持的 `HCCL_ENTRY_LOG_ENABLE`。
6. 如果伴随 device RAS、reset、PCIe AER，再查看 host kernel/device system slog/bbox，与时间及硬件 ID 交叉确认。

## C. 推理启动失败 / 请求超时

分层：`Ingress/LB → API Frontend → Engine/Scheduler → Model Worker → torch_npu/CANN → NPU`。

- **启动即报错**：权重、配置、tokenizer、依赖包/so、TP/PP/rank mapping、shape、内存预算。
- **运行一段时间后崩溃**：查看 worker 退出码、Host OOM/cgroup OOM、HBM、动态 KV cache、请求量、上下游探针。
- **请求 5xx/504**：看网关超时开始时刻与 worker 真实处理日志，核查队列、并发、请求超时与上游重试；`504` 不能等于 NPU 报错。
- **首请求 hang**：编译/图捕获/预热/模型加载阶段，区分冷启动耗时与真故障。
- **分布式推理异常**：重点比对各 worker 的启动/初始化顺序、rank 分配、控制面 Gloo/DNS/端口、数据面 HCCL 与通信标识。
- vLLM 通用诊断文档里的 CUDA/NCCL 开关**不默认适用于昇腾**，必须核对 vllm-ascend、torch_npu 和 HCCL 对应版本再建议。

## D. Host/容器/调度常见反例（不是昇腾特有）

| 现象 | 需要拿到的决定性证据 | 反证路径 |
|---|---|---|
| `exit 137` / Killed | container lastState.reason、`memory.events`、OOM-killer kernel log | 137 也可能是外部 SIGKILL，不见 OOM 证据不可定为 OOM |
| `exit 143` / SIGTERM | Pod event、job lifecycle、自动伸缩/抢占/人工操作记录 | 不等于程序自身崩溃 |
| `No space left on device` | df -h、df -i、checkpoint/log 文件路径 | 检查 inode，不能仅查看磁盘容量 |
| `Too many open files` | `/proc/<pid>/limits`、FD 使用及 ulimit | 检查是否 FD 泄漏 |
| `Temporary failure in name resolution` | `getent hosts`、`/etc/resolv.conf`、DNS 服务日志 | IPv4 通不代表 IPv6 名称解析正常 |
| `connection refused` | 目标端监听、Service/endpoints、路由、容器网络 | 端口不通与 HCCL 数据面异常要分离 |
| `ImagePullBackOff`、`FailedMount` | pod events + registry/CSI/kubelet | 与 NPU Runtime 尚未初始化不同 |
| 数据读取/写入卡住 | NFS 客户端错误、IO 等待、挂载状态、checkpoint 耗时 | 不要把随后的 HCCL timeout 当成根因 |
| CPU 利用率低但进程 hang | 进程状态 D、线程栈、互斥锁/IO、scheduler trace | 不能只靠 CPU 低就判定网络故障 |
| `CUDA`/`NCCL` 字样 | 先确定任务是否为混部 GPU，日志是否属于目标 PID | 不直接套用昇腾 HCCL 处置 |

## E. HCCL 的三类诊断分叉（必须执行）

1. **建链阶段**：搜索 `wait socket establish timeout` / `[InitChannelStage][Timeout]`，查源端和目标端；目标端是否报了更早错误或压根没调用对应通信？双向超时再看网络/TLS，不能只看本端。
2. **任务执行阶段**：查 `Cluster Exception`、`ERROR CQE`、`STUCK`、`LOST`、`Notify`；优先以事件源头和 rank 依赖关系判断，不按哪个日志 ERROR 最早出现机械排序。
3. **通信行为分叉**：同一 communicator 中比较 collective/tag、输入输出大小、迭代、参与 rank；无明确网络/退出证据时，检查代码条件分支、数据 shape、版本和数据集。

参阅：
- [HCCL 建链阶段定位](https://www.hiascend.com/document/detail/en/CANNCommunityEdition/910/commlib/hcclug/docs/en/user_guide/fault_diagnosis/param_link_stage.md)
- [HCCL 任务调度/执行阶段](https://www.hiascend.com/document/detail/en/CANNCommunityEdition/910/commlib/hcclug/docs/en/user_guide/fault_diagnosis/task_exec_stage.md)
- [PyTorch 分布式诊断](https://docs.pytorch.org/docs/stable/distributed.html)
- [Docker 容器资源限制](https://docs.docker.com/engine/containers/resource_constraints)
