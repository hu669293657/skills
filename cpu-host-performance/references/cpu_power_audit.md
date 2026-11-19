# Linux 服务器：高性能策略是否启用，业务核是否达到最高频率？

这是两个独立问题。例如 OS 全部是 `performance`，业务核实测最高 2.4 GHz，策略上限 2.4 GHz、硬件上限 3.0 GHz：应答「已启用 OS 性能策略；采样中达到策略上限，未观察到硬件上限」，而不是「已满频」或「未开启」。再单列 BIOS/BMC 的结果；不同层不做多数表决。

## 1. 在目标宿主机检查策略（只读）

记录 CPU 型号、内核、业务核范围和采样时段。`lscpu`、`uname -r`、`cat /sys/devices/system/cpu/online` 可作为起点。虚机或容器可能只看到虚拟拓扑、不开放 CPUFreq 或看到宿主机共享策略，不能据此认定物理机的配置。没有宿主机或授权数据时写「未核实」。

在正常业务稳定期运行 `python3 scripts/cpu_power_audit.py --samples 10 --interval 1`（Python 3.7+，无第三方依赖、不写 sysfs、不启动压测）。脚本以 kHz 输出；除以 1000 得 MHz。无 Python 时用：

```bash
base=/sys/devices/system/cpu
for p in "$base"/cpufreq/policy[0-9]*; do
  [ -d "$p" ] || continue
  printf '\n%s\n' "$p"
  for key in affected_cpus scaling_driver scaling_governor energy_performance_preference scaling_min_freq scaling_max_freq cpuinfo_max_freq bios_limit; do
    printf '%s: ' "$key"; cat "$p/$key" 2>/dev/null || echo 'N/A'
  done
done
```

- `policy*` 是共享调频策略域，不等于一个物理核。老内核可查 `cpu[0-9]*/cpufreq/`，按 `related_cpus` 或真实路径去重；遍历无输出表示缺证据。装有工具时可用 `cpupower -c all frequency-info` 核对 driver、policy 限制与当前 governor；不要只看默认 CPU 或可选 governor 清单。
- `ALL_PERFORMANCE`：已覆盖的全部在线 CPU 均属 `performance`，只确认 OS 配置。`MIXED`：仅部分是；列出策略域。`NONE_PERFORMANCE`：全量可读且没有域使用这个 governor，不等于 CPU 一定低频。`UNKNOWN`：缺接口、权限或在线覆盖不足；列出已知域和缺项。
- 通用 governor 的 `performance` 请求策略允许的最高档；`schedutil`、`ondemand` 也可能随负载升到顶。Intel `intel_pstate` active 的 `powersave` 不等于锁最低；查看 `intel_pstate/status`、`no_turbo`、`max_perf_pct`（均在 `$base/intel_pstate/` 下）。AMD 看 `$base/amd_pstate/status`、每域 driver、EPP；主动模式下性能偏好是提示而非实测。Arm/鲲鹏按实际驱动（如 `cppc_cpufreq`）和固件能力核对，不要求有 x86 的计数器。各项仅在暴露时读取。

## 2. 在代表性负载下检查实际频率

记录运行中的目标 PID、其实际活跃 CPU/利用率（可用 `mpstat -P ALL 1 10` 辅助），区分空闲、单核忙和多核忙。不要为了“验满频”直接给生产机加压力。检查开始时记录的 `scaling_max_freq`（策略上限）和 `cpuinfo_max_freq`（该域硬件报告上限）；测试期间不要改变配置，热插拔或策略变化后重采。型号标称基频、单核最高睿频、全核可持续频率不是同一个目标。

| 数据 | 能说明什么 |
|---|---|
| `cpuinfo_cur_freq` | 驱动若支持，从硬件获取该域的当前频率；不是该域每个核的独立实测。脚本按域保留最小/峰值、有效样本数、分别达到两个上限的样本数。 |
| `scaling_cur_freq`、`power:cpu_frequency` | 常是驱动报告值或请求档位。脚本保留前者作旁证，但不拿它代替硬件样本。`lshw -c cpu` 中 `size=capacity`、`/proc/cpuinfo` 中一次 MHz 读数同样不能证明持续满频或 governor 配置。 |
| 受支持的 Intel/AMD x86 | 可另行运行 `turbostat --quiet --interval 1 --num_iterations 10 --show CPU,Busy%,Bzy_MHz`（可能需要管理员读取计数器）。用目标 CPU 行的 `Busy%` 与 `Bzy_MHz` 判断忙时平均频率；不能用包含空闲的 `Avg_MHz` 代替。工具/计数器不支持时不估算。 |

脚本对两个上限分别严格比较（无隐含容差）：有效样本 ≥ 上限记一次命中；`REACHED_IN_SAMPLES` 是「采样中达到或超过」，`NOT_SEEN_IN_SAMPLES` 是「有效采样中未见」，`UNKNOWN` 是「缺上限或实测」。例如 1/10 命中只能说曾达到，10/10 也只能说每个有效采样点都达到。多个域分别回答，不拿全机最高值代替业务核。没有 busy 证据就不声称是满载测试；若频率四舍五入而略低，报告数值差，不直接判故障。

未达标先查业务核是否真的忙、策略上限是否低于硬件上限、Turbo/Boost 是否允许，再核对 BIOS 功耗档、BMC 功率限制和温度/节流记录。Boost 可用不等于保证所有核同时跑到单核峰值。除非有同窗口证据，不归因为热降频。

## 3. 华为 A+K：固件和带内策略分别核验

按现场提供的 A+K 示例，在对应节点的 BMC 导出包读取 `dump_info/AppDump/bios/1/currentvalue.json` 的 `CustomPowerPolicy`（大小写不敏感），记录机型、BIOS 版本、导出时间和原值。`Performance` 表示此示例的固件性能档；`Efficiency` 表示效率/可调频档，此时 Linux 仍可能设置 `performance`。缺文件、缺字段、未知枚举或旧日志均不能代表当前配置。其他厂商请查该机型的 **当前生效** BIOS profile，不套用此键，也不把待重启设置当生效值。

**仅 A+K 的可选变更说明，不属于检查流程：** 若另获授权，记录原配置、维护窗口和回退方案后，可在支持的 BIOS 中按 `Advanced → Performance Config → Power Policy` 选择 `Performance`，按该机型手册核对 DEMT（现场示例为 Disable）；走正常维护重启，不强制重启。或保持 `Efficiency`，确认各域支持该 governor 后在带内执行 `sudo cpupower -c all frequency-set -g performance`。OS 设置未必跨重启保留；两条路径都应重新读取生效配置，并在同负载下复测实际频率。

**现场答复格式：** `OS：…（覆盖哪些在线 CPU/策略域）；固件：…（来源/时间）；业务核：…，负载与时段：…；实测来源/峰值：…，策略上限/命中：…，硬件上限/命中：…；缺证/下一步：…`。

参考：[Linux CPUFreq](https://docs.kernel.org/admin-guide/pm/cpufreq.html)、[intel_pstate](https://docs.kernel.org/admin-guide/pm/intel_pstate.html)、[amd-pstate](https://docs.kernel.org/admin-guide/pm/amd-pstate.html)、[turbostat 手册](https://github.com/torvalds/linux/blob/master/tools/power/x86/turbostat/turbostat.8)、[用户提供的鲲鹏 BIOS 参数参考](https://support.huawei.com/enterprise/zh/doc/EDOC1100220443/24c62a5)。
