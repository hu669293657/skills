# Trace 格式详细规范

本文件定义所有支持 trace 格式的结构、检测方法和解析规范。

## 格式检测决策树

```
读取文件前 1KB
  │
  ├── 扩展名 .trace / .perfetto-trace?
  │   └── 检查 magic byte 0x0a (protobuf field tag)
  │       └── YES → Perfetto 格式
  │
  ├── 扩展名 .perf / .data?
  │   └── 检查 magic "PERFILE2" (perf header)
  │       └── YES → perf data 格式
  │
  ├── 扩展名 .json?
  │   └── 解析 JSON，检查 traceEvents 字段
  │       └── 含 "traceEvents" → Chrome Trace Event 格式
  │       └── 含 "name":"acl" → msprof JSON 格式
  │
  ├── 扩展名 .csv?
  │   └── 检查表头，含 "Op Name" / "Duration" → msprof CSV
  │
  ├── 扩展名 .txt?
  │   └── 正则匹配行格式:
  │       ├── 匹配 `<task>-<pid> [<cpu>]` → ftrace 文本
  │       ├── 匹配 `trace-cmd version` → trace-cmd report
  │       ├── 匹配 `# tracer:` → ftrace raw
  │       └── 其他 → 尝试 JSON 行格式
  │
  └── 扩展名 .html?
      └── 检查含 "linux perf" → systrace (已弃用)
```

## 1. Perfetto Trace

### 格式特征

- **编码**：protobuf 二进制
- **工具**：Perfetto UI (ui.perfetto.dev) 或 traceconv
- **来源**：`perfetto` 命令行工具、Android systrace、Linux ftrace 配合 trace processor

### 检测方法

```python
def is_perfetto(file_path):
    with open(file_path, 'rb') as f:
        header = f.read(16)
    # Perfetto trace 文件以 Trace protobuf 的第一个字段开头
    # field 1 (trace_packets), wire type 2 (length-delimited)
    # 即 0x0a 后跟变长长度
    return header[0] == 0x0a
```

### 转换命令

```bash
# 转换为 JSON
traceconv json <input.trace> <output.json>

# 转换为文本
traceconv text <input.trace> <output.txt>

# 如果 traceconv 不可用，使用 Python API
pip install perfetto
python -c "
from perfetto.trace_processor import TraceProcessor
tp = TraceProcessor(trace='input.trace')
for row in tp.query('SELECT * FROM slice LIMIT 10'):
    print(row)
"
```

### 事件结构（转换后 JSON）

Perfetto trace 转换为 JSON 后，格式为 Chrome Trace Event：

```json
{
  "traceEvents": [
    {
      "name": "sched_switch",
      "cat": "sched",
      "ph": "X",  // X=complete event, B/E=begin/end, i=instant
      "ts": 1234567890,  // 微秒
      "dur": 0,  // 微秒
      "pid": 0,
      "tid": 0,
      "args": {
        "prev_pid": 1234,
        "prev_comm": "python",
        "next_pid": 5678,
        "next_comm": "python",
        "prev_state": "R",
        "cpu": 8
      }
    }
  ]
}
```

### 关键事件类型

| 事件名 | 类别 | 含义 | 关键参数 |
|--------|------|------|----------|
| sched_switch | sched | CPU 上下文切换 | prev_pid, next_pid, cpu |
| sched_wakeup | sched | 线程唤醒 | pid, target_cpu, success |
| sched_wakeup_new | sched | 新线程唤醒 | pid, target_cpu |
| sched_process_exit | sched | 进程退出 | pid, comm |
| cpu_frequency | power | CPU 频率变化 | cpu, state |
| cpu_idle | power | CPU 空闲状态 | cpu, state |
| irq_handler_entry | irq | 中断处理开始 | irq, cpu |
| irq_handler_exit | irq | 中断处理结束 | irq, cpu |
| softirq_entry | irq | 软中断开始 | vec, cpu |
| block_rq_issue | block | 块IO请求发出 | dev, sector, rwbs |
| block_rq_complete | block | 块IO完成 | dev, sector, rwbs |
| sys_enter | syscall | 系统调用进入 | id, args |
| sys_exit | syscall | 系统调用退出 | id, ret |

### NPU 相关事件（昇腾场景）

Perfetto trace 中可能包含昇腾 NPU 事件：

| 事件名 | 含义 | 关键参数 |
|--------|------|----------|
| acl:OpExecute | NPU 算子执行 | op_name, duration, device_id |
| acl:streamSync | 流同步 | stream_id, device_id |
| acl:memcpyH2D | Host 到 Device 拷贝 | size, duration |
| acl:memcpyD2H | Device 到 Host 拷贝 | size, duration |
| ge:LaunchOp | Graph Engine 算子下发 | op_name, graph_id |
| ge:CompileOp | Graph Engine 算子编译 | op_name, duration |

---

## 2. ftrace 文本格式

### 格式特征

- **编码**：纯文本，每行一个事件
- **来源**：`trace-cmd record`、`cat /sys/kernel/debug/tracing/trace`、Perfetto 文本导出

### 检测方法

```python
import re

def is_ftrace_text(first_lines):
    # 标准 ftrace 行格式
    pattern = r'^\s*(\S+)-(\d+)\s+\[(\d+)\]\s+([a-zA-Z.\s]*?)\s+(\d+\.\d+):\s+(\w+):'
    for line in first_lines:
        if re.match(pattern, line):
            return True
    return False
```

### 文件结构

```
# tracer: nop
#
# entries-in-buffer/entries-written: 500000/500000
#                              _-----=> irqs-off
#                             / _----=> need-resched
#                            | / _---=> hardirq/softirq
#                            || / _--=> preempt-depth
#                            ||| /     delay
#           TASK-PID   CPU#  ||||    TIMESTAMP  FUNCTION
#              | |       |   ||||       |         |
  python-1234  [008] d... 1234567.890123: sched_switch: prev_comm=python prev_pid=1234 prev_prio=120 prev_state=R+ next_comm=python next_pid=5678 next_prio=120
  python-1234  [008] d... 1234567.890456: sched_wakeup: comm=python pid=5678 prio=120 target_cpu=008
  python-5678  [008] d... 1234567.891234: block_rq_issue: 8,0 WS 0 () 128 + 8 [python]
```

### 字段解析

| 字段 | 位置 | 含义 | 示例 |
|------|------|------|------|
| task | 1 | 进程名 | python |
| pid | 2 | 进程ID | 1234 |
| cpu | 3 | CPU编号 | [008] |
| flags | 4 | 中断/抢占标志 | d... |
| timestamp | 5 | 时间戳(秒) | 1234567.890123 |
| event | 6 | 事件名 | sched_switch |
| data | 7+ | 事件数据 | prev_comm=... |

### flags 字段含义

```
 _-----=> irqs-off          d = 中断关闭
/ _----=> need-resched      N = 需要调度
| / _---=> hardirq/softirq  H = 硬中断, s = 软中断
|| / _--=> preempt-depth    数字 = 抢占深度
||| /     delay             . = 无延迟标记
```

### 关键事件解析

#### sched_switch

```
sched_switch: prev_comm=python prev_pid=1234 prev_prio=120 prev_state=R+ next_comm=python next_pid=5678 next_prio=120
```

解析：
- `prev_state=R+`: R=Running, +=preempted, S=Sleeping, D=Uninterruptible, T=Stopped

#### sched_wakeup

```
sched_wakeup: comm=python pid=5678 prio=120 target_cpu=008
```

#### sched_waking (与 sched_wakeup 区别)

```
sched_waking: comm=python pid=5678 prio=120 target_cpu=008
```

`sched_waking` 是 wakeup 发生时触发，`sched_wakeup` 是目标 CPU 接收到 wakeup 时触发。用两者的时间差计算调度延迟。

#### block_rq_issue / block_rq_complete

```
block_rq_issue: 8,0 WS 0 () 128 + 8 [python]
block_rq_complete: 8,0 WS () 128 + 8 [python]
```

- `8,0`: major,minor 设备号
- `WS`: 写操作
- `128 + 8`: 起始扇区 + 扇区数

#### numa_events

```
numa_hit: pid=1234 cpu=8 nid=0
numa_miss: pid=1234 cpu=8 nid=1 preferred_node=0
numa_local: pid=1234 cpu=8 nid=0
numa_remote: pid=1234 cpu=8 nid=1 preferred_node=0
```

#### irq/softirq

```
irq_handler_entry: irq=28 name=mlx5_comp@pci
irq_handler_exit: irq=28 ret=handled
softirq_entry: vec=1 [action=TIMER]
softirq_exit: vec=1 [action=TIMER]
```

---

## 3. msprof 格式（昇腾专用）

### 格式特征

- **编码**：JSON 或 CSV
- **来源**：昇腾 msprof 工具
- **用途**：主要采集 NPU 侧性能数据，但也包含 host runtime 调用

### 检测方法

```python
def is_msprof_json(file_path):
    with open(file_path, 'r') as f:
        content = f.read(4096)
    return '"name"' in content and ('"acl"' in content or '"Op"' in content or '"ge"' in content)

def is_msprof_csv(file_path):
    with open(file_path, 'r') as f:
        header = f.readline()
    return 'Op Name' in header or 'Duration' in header or 'ACLNN' in header
```

### JSON 格式

```json
[
  {
    "name": "MatMul",
    "cat": "Op",
    "ph": "X",
    "ts": 1234567890,
    "dur": 5000,
    "pid": 0,
    "tid": 12345,
    "args": {
      "device_id": 0,
      "stream_id": 0,
      "input_shapes": "[[2048,2048]]",
      "output_shapes": "[[2048,2048]]"
    }
  },
  {
    "name": "aclmdlExecute",
    "cat": "Runtime",
    "ph": "X",
    "ts": 1234567880,
    "dur": 5050,
    "pid": 0,
    "tid": 999,
    "args": {
      "host_thread": 12345,
      "device_id": 0
    }
  }
]
```

### CSV 格式

```csv
Op Name,Op Type,Task Type,Start Time,Duration,Device ID,Stream ID,Input Shapes,Output Shapes
MatMul,MatMul,AFrCtask,1234567890,5000,0,0,"[[2048,2048]]","[[2048,2048]]"
aclmdlExecute,Runtime,API,1234567880,5050,0,0,N/A,N/A
```

### 关键事件类别

| 类别 | cat 值 | 含义 | Host/NPU |
|------|--------|------|----------|
| 算子执行 | Op | NPU 算子执行 | NPU |
| Runtime 调用 | Runtime | ACL runtime API 调用 | Host→NPU |
| 内存拷贝 | Memcpy | H2D/D2H/DDR 拷贝 | Host↔NPU |
| 流同步 | Sync | Stream 同步等待 | Host↔NPU |
| Graph Engine | GE | 图引擎编译/下发 | Host→NPU |
| HCCL | HCCL | 多卡通信 | NPU↔NPU |

### Host runtime 事件

msprof 中的 host 侧事件（用于 Host-NPU 关联分析）：

| 事件名 | 含义 | 用途 |
|--------|------|------|
| aclmdlExecute | 模型执行 | Host 提交到 NPU 的时间点 |
| aclmdlExecuteAsync | 异步执行提交 | kernel launch 时间点 |
| aclrtSynchronizeStream | 流同步等待 | Host 等待 NPU 完成的阻塞时间 |
| aclrtMemcpy | 内存拷贝 | H2D/D2H 拷贝时间 |
| aclrtMemAlloc | 内存分配 | NPU 内存分配时间 |
| ge::CompileOp | 算子编译 | JIT 编译延迟 |

---

## 4. perf data 格式

### 格式特征

- **编码**：二进制
- **来源**：`perf record` 命令
- **检测**：文件头 magic `PERFILE2`

### 转换命令

```bash
# 转换为文本格式
perf script -i <input.perf> > <output.txt>

# 转换为 CSV（使用 perf script + awk）
perf script -i <input.perf> -F comm,pid,tid,cpu,time,event,ip,sym,dso | \
  awk -F' ' '{print}' > output.csv

# 聚合统计
perf report -i <input.perf> --stdio > report.txt

# 火焰图
perf script -i <input.perf> | stackcollapse-perf.pl | flamegraph.pl > flame.svg
```

### 转换后文本格式

```
python  1234/1234 [008] 1234567.890123:  sched:sched_switch: prev_comm=python prev_pid=1234
python  1234/1234 [008] 1234567.890456:  sched:sched_wakeup: comm=python pid=5678
python  1234/1234 [008] 1234567.891234:  cpu-clock:
        7f8b2c3d4e50 do_something+0x10 (/lib/libxxx.so)
        7f8b2c3d4e40 main+0x20 (/usr/bin/python)
```

### perf 事件类型

| 事件 | 含义 | Host/NPU |
|------|------|----------|
| sched:sched_switch | 调度切换 | Host |
| sched:sched_wakeup | 线程唤醒 | Host |
| sched:sched_process_exec | 进程执行 | Host |
| cpu-clock | CPU 时钟周期 | Host |
| task-clock | 任务时钟 | Host |
| page-faults | 页错误 | Host |
| context-switches | 上下文切换 | Host |
| migrations | CPU 间迁移 | Host |
| block:block_rq_issue | IO请求 | Host |
| block:block_rq_complete | IO完成 | Host |

---

## 5. trace-cmd 格式

### 格式特征

- **编码**：二进制（.dat）或文本（report 输出）
- **来源**：`trace-cmd record` 命令
- **检测**：二进制 magic `tracing_data`

### 转换命令

```bash
# .dat 文件转换为文本
trace-cmd report <input.dat> > <output.txt>

# 转换为 JSON（如果支持）
trace-cmd report -j <input.dat> > <output.json>
```

转换后格式与 ftrace 文本格式相同。

---

## 6. 自定义/未知格式处理

当格式无法识别时，Agent 应：

1. **采样分析**：读取文件前 100 行，尝试识别模式
2. **结构推断**：
   - 是否为 JSON 行格式（每行一个 JSON）
   - 是否为 CSV（有分隔符）
   - 是否为 key=value 格式
   - 是否有时间戳字段
3. **构建 ad-hoc parser**：
   - 提取时间戳字段（尝试常见字段名：ts, timestamp, time, t）
   - 提取事件类型字段（尝试常见字段名：name, event, type, op）
   - 提取 PID/TID/CPU 字段
4. **降级分析**：如果无法结构化解析，使用正则匹配关键模式

### 通用正则模式

```python
# 时间戳模式
TS_PATTERNS = [
    r'(\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}:\d{2}\.\d+)',  # ISO 格式
    r'(\d+\.\d+):',  # ftrace 格式
    r'"ts":\s*(\d+)',  # JSON
    r'\[(\d+\.\d+)\]',  # bracket 格式
]

# PID 模式
PID_PATTERNS = [
    r'pid[=:](\d+)',
    r'"pid":\s*(\d+)',
    r'-(\d+)\s+\[',
]

# CPU 模式
CPU_PATTERNS = [
    r'cpu[=:](\d+)',
    r'"cpu":\s*(\d+)',
    r'\[(\d+)\]',
]

# 事件类型模式
EVENT_PATTERNS = [
    r'^(\w+):',  # 行首事件名
    r'"name":\s*"(\w+)"',  # JSON
    r'(\w+):',  # event: data
]
```
