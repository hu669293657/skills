# 现网全栈日志矩阵与安全取证（Ascend 优先）

> 适用：训练、推理、容器、集群、服务启动/中断。采集时只读、先保全故障现场，原则上不在现网执行重启、reset、清日志、设置长期 DEBUG 或修改网络/防火墙。**产品、CANN/驱动版本不同，命令及目录以部署手册为准**。以下命令是建议，不代表一定支持。

## 1. 信息优先级（不是所有日志都必须采）

| 优先级 | 故障层 | 必要信息及典型位置 | 能回答的问题 |
|---|---|---|---|
| P0 | 业务现场 | 故障时间/时区、故障范围、最近变更、启动命令脱敏版、容器 ID/作业 ID、各 rank 的 stdout/stderr/Traceback、退出状态 | 任务为什么没有走下去？哪个 rank 先退出？ |
| P0 | CANN 应用日志 | `$HOME/ascend/log/{debug,run,security}/plog/plog-*.log`；`{debug,run}/device-*/device-*.log`；先看 `ASCEND_PROCESS_LOG_PATH`、`ASCEND_WORK_PATH` | Host runtime/GE/ACL/TorchNPU/HCCL vs Device AI CPU/HCCP |
| P0 | OS/内核 | `journalctl -k`、`dmesg`、`/var/log/messages`、`/var/log/syslog`；进程退出码、OOM、core、systemd、reboot | 是否被 OS 杀死？PCIe/驱动/网卡/内存/磁盘异常？ |
| P0 | 调度/容器（若存在） | Pod describe/events、container `--previous` 日志、kubelet/containerd、`docker inspect`、cgroup v1/v2 限额 | CrashLoop、OOMKilled、Evicted、FailedScheduling、镜像/挂载问题 |
| P1 | Device 系统与硬件 | **Device slog/device-os、syslog、黑匣子/bbox**；在兼容裸机侧经 `msnpureport` 导出；`npu-smi info`、健康/RAS/故障事件（按型号查询） | 设备级复位、ECC/RAS/链路与系统服务是否先异常 |
| P1 | HCCL/网络 | HCCL run/debug plog、HCCP device、link-establish、心跳/ERROR CQE/STUCK/LOST、rank-table、IP/端口/TLS/路由、网卡计数器 | 超时根因在对端退出、网络、还是各 rank 行为不同 |
| P1 | 训练框架/数据 | torchrun/elastic、MindSpore/MindSpeed/Megatron/DeepSpeed；step/数据加载器/checkpoint/编译/精度；CPU 内存、文件系统 | 数据读取阻塞、Python 异常、存盘失败、主机 OOM |
| P1 | 推理服务 | vLLM/vllm-ascend/MindIE/Triton 等启动及 worker 日志、HTTP 访问/网关/健康探针、模型权重、请求 ID、KV Cache 指标 | 请求超时是服务未启动、worker 退出、资源耗尽还是上游依赖 |
| P2 | 深度取证 | core/stackcore、coredumpctl 信息、ftrace、Profiler/db/text、算子 dump | 仅第一轮日志无法解释时再按需取证 |

### 容易混淆的术语

- `plog` 是应用 **Host 侧**日志；`device-<id>/device-<pid>_*.log` 是应用 **Device 侧**日志；`slog` 常指设备**系统类**日志，不应认为“slog 就是 plog 的来源”。
- `$HOME/ascend/log` 是典型默认目录，不是唯一目录；容器中的 HOME 可能与裸机不同。用户也可能设置了 `ASCEND_PROCESS_LOG_PATH` 和 `ASCEND_WORK_PATH`。
- `msnpureport` 在部分形态的设备系统日志导出需要在**宿主机**执行；官方文档指出某些标准形态不支持容器内导出，必须先确认工具所在路径、权限、产品支持情况，不能简单假定每个容器内可运行。
- Device ID / Logic ID / Physical ID / Chip ID / rank ID / container NPU 可见索引不是同一字段！写明映射关系，避免错投故障卡。
- `dmesg -T` 按当前系统时间转换内核单调时钟，不保证跨重启/时钟调整时精确。多机时序应保留时区、NTP 偏差和原始时间字符串。

## 2. 只读取证命令（在用户具备授权的节点执行）

```bash
date -Ins; hostname -f; uptime; cat /etc/os-release
id; uname -a; env | grep -E '^(ASCEND|HCCL|TORCH|MASTER_|RANK|WORLD_SIZE|LOCAL_RANK|NPU_VISIBLE_DEVICES|LD_LIBRARY_PATH)='
npu-smi info
# 具体设备 ID/Chip ID 先用当前型号 npu-smi 帮助和 info 输出确认；按型号决定健康查询
# npu-smi info -t health -i <device-id> -c <chip-id>

journalctl -k --since '2026-10-17 10:00:00' --until '2026-10-17 11:00:00' --no-pager
journalctl --since '2026-10-17 10:00:00' --until '2026-10-17 11:00:00' --no-pager
dmesg -T | tail -n 400
df -hT; df -i; free -h; cat /proc/meminfo | head
ss -lntup; ip -br addr; ip route; cat /etc/resolv.conf
ls -ld "$HOME/ascend/log" "${ASCEND_PROCESS_LOG_PATH:-$HOME/ascend/log}" 2>/dev/null
```

```bash
# K8s: 命名空间和实例名请替换；注意 previous 是上一个容器实例，不保证保留所有历史日志
kubectl -n <namespace> get pod <pod> -o wide
kubectl -n <namespace> describe pod <pod>
kubectl -n <namespace> logs <pod> -c <container> --timestamps
kubectl -n <namespace> logs <pod> -c <container> --previous --timestamps
kubectl -n <namespace> get events --sort-by=.metadata.creationTimestamp
# Docker
docker inspect <container_id>
docker logs --timestamps <container_id>
```

`msnpureport`、`hccn_tool`、特定 `npu-smi -t ...` 选项的语法需按现场已安装版本的 `--help` 和对应产品手册核对，不能把命令失败判定为产品故障。采集前确认磁盘空间和操作授权。

## 3. 任务态专项补证

**拉起失败**：启动命令与配置（去密）、入口栈/进程退出码、镜像/挂载、ldd/包版本、模型路径/权限、rank table 与网络。不要直接跳到 HCCL。

**训练中断 / Hang**：所有 rank 最后一个正常 step、首次进度停止、数据加载器/checkpoint 阶段、torchrun agent 子进程退出原因、Host OOM、系统/设备告警、HCCL run/plog 心跳，不能只看超时者。

**推理异常**：API 网关→Frontend→Engine→Worker→Runtime→NPU 各环日志关联，记录请求 ID、HTTP 状态、排队/预填充/解码阶段、健康探针、worker 状态、进程/设备内存、启动权重与并行配置。

**多机通信**：保存 rank↔hostname↔IP↔NPU ID↔PID 映射；比较各 rank 相同 collective/tag 的事件顺序，校正时钟后再说“谁先发生”。`ERROR CQE` 指向网络相关线索，`STUCK`/ `LOST` 不直接等价于某个设备损坏。

**系统通用问题**：检查 OS/cgroup OOM、磁盘/inode 满、文件句柄、进程限制、DNS IPv6、端口冲突、磁盘/NFS 超时、调度回收、容器 cgroup 与 PIDs；这些问题会使健康 NPU 上的任务失败。

## 4. 安全性和保真性

- 原始日志只读，不上传含客户令牌、模型权重、IP、密钥和敏感路径的信息到公共仓库或公共分析服务。脱敏必须保留跨文件可连接的稳定替代 ID；保留离线原件、SHA256 和脱敏映射（映射不要公开）。
- 禁止自动执行 `npu-smi reset`、`reboot`、`kill`、`systemctl restart`、改日志级别、`rm`、网络切换、驱动/固件升级；如需建议，只给风险、回滚和许可前提。
- 对需要 root 权限或生产环境可能增加负荷的命令明确风险。遇到崩溃证据，应先采集并保存，再讨论恢复操作。

## 官方参考（使用时匹配现场版本）

- [CANN 8.5 应用与系统日志](https://www.hiascend.com/document/detail/en/canncommercial/850/maintenref/logreference/logreference_0002.html)
- [MindCluster 2.6.1 故障诊断日志采集](https://www.hiascend.com/document/detail/en/mindcluster/2610/faultdiag/faultdiagug/docs/en/faultdiag/ascend-faultdiag/05_usage/02_log_collection.md)
- [CANN 9.0 RAS event id / bbox 定位](https://www.hiascend.com/document/detail/en/CANNCommunityEdition/900/maintenref/troubleshooting/troubleshooting_0103.html)
- [Kubernetes 调试运行中的 Pod](https://kubernetes.io/docs/tasks/debug/debug-application/debug-running-pod/)
