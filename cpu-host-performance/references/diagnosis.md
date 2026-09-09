# Diagnostic model

Prefer combinations of metrics over a single threshold. The analyzer treats trace loss, unsupported events, and snapshot-only data as limits on confidence.

| Finding | Evidence pattern | Default severity |
|---|---|---|
| CPU_SATURATION | average traced CPU busy time >= 90% and scheduler latency P99 >= 2 ms | CRITICAL / WARNING |
| CPU_IMBALANCE | busiest CPU busy >= 90%, at least half CPUs busy <= 30%, spread >= 50 pp | WARNING |
| SCHEDULER_CONTENTION | context switches >= 20k/s plus wakeups >= 10k/s | WARNING |
| SCHEDULER_LATENCY | wakeup-to-run P99 >= 5 ms (>= 20 matched samples) | CRITICAL / WARNING |
| IRQ_HOTSPOT / SOFTIRQ_HOTSPOT | a CPU owns >= 50% of respective observed handler duration and >= 10 ms | WARNING |
| CPU_FREQUENCY_LOW | observed median frequency is < 60% of observed max while CPU busy >= 70% | DEGRADED |
| CPU_IDLE_EXCESSIVE | CPU busy <= 20% with meaningful trace duration | DEGRADED; it is not proof of a CPU problem |
| TASK_MIGRATION_HIGH | migrations >= 1k/s | WARNING |
| HOST_NOT_BOTTLENECK | CPU busy <= 50%, P99 schedule delay < 2 ms, no hotspot | HEALTHY |

The threshold table is a triage policy, not a hardware specification. Escalate severity only when multiple independent signals agree. Frequency and NUMA conclusions normally remain MEDIUM or LOW confidence without time-correlated topology, temperature, and workload data.
