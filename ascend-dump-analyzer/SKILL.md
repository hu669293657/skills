---
name: "ascend-dump-analyzer"
description: "用于 Ascend 昇腾服务器环境采集、dump/预检信息分析和多环境配置漂移比对；也用于用户明确指定本 skill 后说‘帮我分析这个日志/附件’的请求，输入可能为 .json、.log、.txt 或目录。一般现网故障日志取证用 issue-analyzer，Prof 性能分析用 cluster-analysis，BMC 硬件健康诊断用 ibmc_analyzer。"
---

# Ascend 环境分析器

采集、分析和比对昇腾 NPU 环境 dump JSON 文件。本技能包含独立采集脚本（`msprechecker_dump.py`）、比对脚本（`compare_dumps.py`）、报告导出脚本（`render_report.py`）、分析指南和 HTML 报告模板。

## 分析报告交付契约（必须使用模板）

**用户请求分析、诊断、环境体检或比对时，默认在同一轮交付两个实际文件：`<报告名>.md` 和 `<报告名>.html`。聊天中的 Markdown、终端输出和生成命令均不能替代报告文件。** 用户不需要额外说“生成报告”“保存文件”或“可视化”；“使用这个 skill 帮我分析这个日志”直接进入以下流程，不询问是否需要报告，也不以“需要的话可以生成”结束。

1. **定位输入并识别内容。** 优先使用用户指定路径、附件及当前上下文，按实际内容识别 dump，不按扩展名拒绝 `.log` / `.txt` 中的 JSON。目录中有多个候选时列清单并说明实际覆盖范围，不静默漏掉输入。明确指定本 skill 但输入是非 dump 故障日志时，只分析可支持的环境事实，报告标注能力边界并建议适合的专项技能。
2. **确定输出位置。** 使用用户指定目录；否则遵守当前工作区的产物目录约定（如 `outputs/`），没有约定则使用当前项目的 `outputs/`。创建必要目录，不默认写入 skill 源码目录，不覆盖原始输入。文件名采用 `ascend_analysis_<时间戳>` 或 `ascend_comparison_<时间戳>`；同名已存在时加序号，保留既有报告。只需选定路径并执行，无需为默认格式、目录或文件名另行确认。
3. **必须读取并填写对应模板。** 单份分析用 [单份报告模板](references/template_single_dump.md)，多份比对用 [比对报告模板](references/template_comparison.md)。用模板的必需二级标题及顺序写入实际 UTF-8 Markdown，替换所有填写提示，保留证据路径和适用性。缺失、受阻、零差异、只要求一种输出格式或只在聊天展示时同样按模板组织内容。所有敏感值先脱敏，依据证据标注 UNKNOWN 或 N/A。
4. **从同一份 Markdown 导出 HTML。** 默认执行下列命令；`<SKILL_DIR>` 指当前读取的本技能目录，必须替换成实际路径，脚本路径和报告路径均加引号。HTML 默认与 MD 同目录、同名，只改变扩展名，内联 CSS、零外部字体/脚本依赖。导出器只负责排版和文件检查，不代替分析或脱敏。

   ```bash
   python3 "<SKILL_DIR>/references/render_report.py" "<OUTPUT_DIR>/<报告名>.md" --template single
   python3 "<SKILL_DIR>/references/render_report.py" "<OUTPUT_DIR>/<报告名>.md" --template single --check
   ```

   多份比对的两条命令都改用 `--template comparison`。Windows 可将 `python3` 换成 `python`。导出器校验必需章节与顺序。Markdown 使用标题、段落、表格、平铺列表、引用、围栏代码块、行内代码/强调及普通链接。
5. **核验后交付。** 使用默认导出器时，两次命令均须退出码为 0；采用下表的手工降级路径时，实际读回文件并核对内容。检查 MD 和 HTML 真实存在、非空、内容一致，未残留模板占位符（日志原文中的同形文本可作为证据保留），不含未脱敏凭证。修改 MD 后重新导出并核验。最终回复给出简短结论与两个可点击的真实文件链接，分别标注“Markdown 报告”和“HTML 报告”；不只回复路径、计划或命令。

**条件分支：**

| 已观察到的条件 | 执行动作 |
|---|---|
| 数据部分缺失、解析失败、没有发现异常、比对结果零差异 | 仍生成 MD + HTML；分别说明证据限制、解析错误或零差异，UNKNOWN 不等同于正常 |
| 未收到输入、附件无法读取、路径不存在 | 在可写输出目录生成“分析受阻/待补充输入”双报告，说明请求路径、读取结果、未完成的分析及所需材料，再提示用户提供真实输入；不伪造诊断 |
| 比对脚本失败或有效输入不足两个 | 仍交付双报告，标明请求清单、实际读取清单、跳过/失败项；不能把剩余一份输入包装成完整比对 |
| HTML 导出失败 | 读取错误并修复 Markdown 格式/路径等问题后重跑；仍失败则用安全转义的内联 HTML 呈现同一份内容并人工核验，明确生成方式，保留 MD |
| 指定输出目录不可写 | 尝试当前工作区允许的产物目录并在回复说明实际位置；所有可用位置均不可写时如实说明缺失文件和写入错误，禁止声称已生成 |
| 用户明确说“只要 MD”“只要 HTML”“不要文件/只在聊天里说” | 遵守明确格式要求并在交付中说明覆盖了默认双格式；“分析一下”“简单分析”不构成覆盖 |
| 用户只要求讲解/修改 skill、拷贝采集脚本或仅采集原始 dump | 执行所请求的工作；不自动探测本机、登录服务器或开展额外诊断。已提供/采集 dump 且要求分析时执行双报告流程 |

**完成门槛：对应模板已填完、MD 与 HTML 已写入、模板和文件核验通过，并交付两个链接。只要求单一格式时仍须核对对应模板。**

## 触发条件

- 用户需要在 Ascend NPU 服务器上采集环境信息（使用采集脚本）
- 用户提供 `.json` 文件并要求分析/诊断 Ascend 环境
- 用户要求比对两个或多个 dump JSON 文件
- 用户提到"dump 分析"、"环境对比"、"配置漂移"
- 用户问"两个环境之间有什么变化"
- 用户要求比对两个或多个预检信息文件
- 用户明确指定本 skill 后说“分析这个日志/附件”，即使没有提到报告或文件扩展名不是 `.json`

## 核心能力

### 0. 环境采集（Dump）

本技能包含一个独立 Python 脚本 `references/msprechecker_dump.py`，可部署到任意 Ascend NPU 服务器采集环境信息。

**核心特性：**
- 零依赖 — 仅需 Python 标准库（psutil 为可选依赖，缺失时自动跳过）
- 单文件 — 拷贝到任意 Linux 服务器即可用 `python3` 运行
- 保留旧版主要 JSON 区块，随附比对工具读取历史与新版 dump
- 结构化 + raw 双输出 — 每个 section 同时提供结构化字段（供判定）与原始命令输出（供留证）

**使用场景：**
- 用户要求"采集环境信息"或"创建 dump"
- 用户需要部署前的基线快照
- 用户在未安装 msprechecker 的机器上采集数据

**部署和使用：**

1. 拷贝脚本到目标服务器：
```bash
scp references/msprechecker_dump.py user@server:/tmp/
```

2. 在目标服务器上运行：
```bash
# 基础采集（系统 + Ascend + 环境变量）
python3 /tmp/msprechecker_dump.py

# 指定输出路径
python3 /tmp/msprechecker_dump.py -o /tmp/snapshot.json

# 固定环境快照。将 PID 替换为本机可见的真实业务进程
python3 /tmp/msprechecker_dump.py --filter --no-probes \
  --target launcher=1234 --target worker-0=2345 -o /tmp/snapshot.json

# 只采集器视图，按环境键范围筛选
python3 /tmp/msprechecker_dump.py --filter -o /tmp/snapshot.json

# 全量采集（含配置、网络、权重）
python3 /tmp/msprechecker_dump.py \
  --mies-config-path /usr/local/Ascend/mindie/latest/mindie-service/conf/config.json \
  --rank-table-path /path/to/rank_table.json --scene mindie \
  --weight-dir /path/to/weights \
  --filter -o /tmp/full_dump.json
```

**命令行参数：**

| 参数 | 必选 | 说明 |
|------|------|------|
| `-o, --output-path` | 否 | 输出 JSON 路径。默认：`./msprechecker_dumped.json` |
| `--filter` | 否 | 筛选采集器环境变量，覆盖昇腾、框架、分布式、路径和绑核变量 |
| `--target ROLE=PID` | 否 | 可重复。读取指定进程的初始环境、加载库及限制。跨环境沿用相同角色名 |
| `--no-probes` | 否 | 固定环境快照建议使用。跳过 ICMP/TCP/NPU ping，保留 rank table 与只读链路状态查询 |
| `--mies-config-path` | 否 | MindIE 服务 config.json 路径 |
| `--user-config-path` | 否 | user_config.json 路径 |
| `--mindie-env-path` | 否 | mindie_env.json 路径 |
| `--rank-table-path` | 否 | Rank table 文件路径（触发 ping/HCCL/link/vnic/tls 采集） |
| `--scene` | 否 | `mindie` 或 `vllm` — 决定 rank table 解析格式 |
| `--weight-dir` | 否 | 模型权重目录（触发 SHA256 哈希采集） |
| `--chunk-size` | 否 | 哈希计算块大小(MB)：32/64/128/256。默认：32 |

**采集内容：**

| Section | 始终采集 | 触发参数 | 内容 |
|---------|---------|---------|------|
| `system` | 是 | — | CPU 型号、高性能模式、内核、THP、内存、虚拟化检测 |
| `hardware`、`npu`、`network` | 是 | — | 硬件资源、NPU 运行态与拓扑查询、网卡/RDMA、路由与策略规则 |
| `ascend` | 是 | — | 驱动、Toolkit、算子、Runtime、Compiler、HCCL、AI CPU、ATB/SiP、MindIE 等已知版本文件路径和候选版本 |
| `env`、`runtime` | 是 | — | 采集器视图的环境变量、Python 包和路径、进程概况、容器信息 |
| `workload` | 是 | `--target` 填充 | 按角色采目标进程环境、共享库、身份、资源限制；未指定时记录跳过 |
| `rank_table` | 否 | `--rank-table-path` | 规范化 Host、设备与角色清单，保留解析覆盖 |
| `mies config` | 否 | `--mies-config-path` | MindIE 服务 config.json |
| `user config` | 否 | `--user-config-path` | user_config.json |
| `mindie env` | 否 | `--mindie-env-path` | mindie_env.json |
| `model config` | 否 | `--weight-dir` | 权重目录下的 config.json |
| `weight` | 否 | `--weight-dir` | .safetensors 文件的 SHA256 哈希 |
| `ping` | 否 | `--rank-table-path` 且未关闭探测 | rank table 中主机的 ping 结果 |
| `hccl` | 否 | `--rank-table-path` 且未关闭探测 | 设备 IP 连通性探测 |
| `link` | 否 | `--rank-table-path` | NPU 链路状态（hccn_tool） |
| `vnic` | 否 | `--rank-table-path` | VNIC 状态（A3 板卡） |
| `tls` | 否 | `--rank-table-path` | TLS 证书状态 |
| `_meta` | 是 | — | 工具名、版本、时间戳、主机名、Python 版本、耗时 |

**用户指导：**
- 固定使用同一组选项及角色名，在 Host 和业务容器分别采集并标记视图。分享前复核脱敏，`--filter` 只限定键范围。
- `env` 与 `runtime.packages` 属于采集器，`workload.targets.<角色>.environment` 属于目标进程的初始环境。应用启动后覆盖的值、编排侧镜像 digest 需用相应来源补证。仅比对同一视图与角色。
- 1.2 的逐卡固件从 board 的 Firmware Version 读取。早期 dump 的结构化 `firmware_version` 可能误取驱动，需回看 board 原文再比对。NPU 清单和拓扑查询保留原文，避免把设备节点编号直接当成物理 ID。
- 多机比对时需在**每台**机器上分别采集
- PD 分离 / K8s 场景使用 `--user-config-path` 和 `--mindie-env-path`
- root 和非 root 均可运行（TLS 检查需 root）

### 0.5. Dump 比对（compare_dumps.py）

本技能包含一个独立 Python 脚本 `references/compare_dumps.py`，用于比对 2 个或多个 dump JSON 文件。

**核心特性：**
- 零依赖 — 仅需 Python 标准库
- 支持 2 个或更多文件同时比对
- 鲁棒 JSON 解析 — 处理 markdown 转义字符（`\_` → `_`、`\:` → `:`）、多余空行等
- 递归扁平化 — 适用于任意 JSON 结构，不依赖固定 schema
- Section 感知分类 — 按 hardware / system / npu / ascend / runtime / env / config / network / weight / other 分组差异
- 智能截断 — 超长值（LS_COLORS、路径）在显示时截断，JSON 输出中保留完整值
- 多种输出格式：终端文本、HTML 报告、JSON 差异文件

**使用场景：**
- 用户要求比对两个或多个 dump 文件
- 用户需要识别环境间的配置漂移
- 用户需要验证集群一致性

**使用方法：**

```bash
# skill 的默认比对路径：先取差异证据（占位路径需替换，使用当前 skill 的实际位置）
python3 "<SKILL_DIR>/references/compare_dumps.py" "env1.json" "env2.json" --json "<WORK_DIR>/diff.json"

# 读取 diff.json，结合证据与分析指南写入 <OUTPUT_DIR>/ascend_comparison_<时间戳>.md
# 然后生成配对 HTML 并核验；不是把终端输出或 JSON 当作交付报告
python3 "<SKILL_DIR>/references/render_report.py" "<OUTPUT_DIR>/ascend_comparison_<时间戳>.md"
python3 "<SKILL_DIR>/references/render_report.py" "<OUTPUT_DIR>/ascend_comparison_<时间戳>.md" --check

# 脚本底层选项：可额外导出机器可读 JSON 或简易 HTML
python3 "<SKILL_DIR>/references/compare_dumps.py" "env1.json" "env2.json" "env3.json" --json "diff.json" --html "raw_diff.html"
```

**命令行参数：**

| 参数 | 必选 | 说明 |
|------|------|------|
| `files`（位置参数） | 是（2+） | dump JSON 文件路径 |
| `--html PATH` | 否 | 输出 HTML 报告 |
| `--json PATH` | 否 | 输出 JSON 差异文件 |
| `--text` | 否 | 终端文本输出（默认） |

**鲁棒性设计（处理多样化 JSON 结构）：**

1. **递归扁平化**：任意 dict/list/scalar 展平为 `path→value` 对。字典路径用 `["JSON 转义键"]`，数组用 `[index]`，根为 `$`；空 `{}` 和 `[]` 保留原类型。
2. **JSON 修复**：尝试直接解析 → 修复 markdown 转义 → 去空行重试。全部失败则跳过该文件并告警。
3. **Section 追踪**：记录每个顶层 section 存在于哪些文件中。报告"仅在文件 X 中存在"。
4. **类别分类**：顶层 key 映射到类别（system/ascend/env/config/network/weight/meta/other）。未知 section 归入"other"。
5. **差异类型**：每项差异分为"modified"（所有文件都有但值不同）或"only_in"（部分文件有、部分没有）。
6. **相似度评分**：`一致路径数 / 总路径数 * 100`，排除 `_meta`。

**用户指导：**
- `compare_dumps.py` 单独运行的默认行为仍为终端文本；它是取证工具，不是 skill 的交付终点。调用 skill 时必须继续写入 MD 并导出同源 HTML，`--html` / `--json` 均为可选补充，不替代双报告
- 比对不同采集方法产生的文件时（如 `msprechecker dump` vs `msprechecker_dump.py`），JSON 结构应兼容但 section 名可能略有差异
- 脚本能处理一侧有某 section 而另一侧没有的情况（如一侧有 `ping`/`hccl` 而另一侧没有）

### 1. 单机分析

给定一个 dump JSON 文件（也可能包装在 `.log` / `.txt` 中），写入 Markdown 分析报告并导出同源 HTML，包含：

1. **元数据** — 主机名、时间戳、采集耗时
2. **系统健康** — CPU 型号、高性能模式、内核版本、THP 状态、内存设置、虚拟化检测
3. **Ascend 组件** — driver/toolkit/atb/mindie/atb-models 版本，标记缺失组件，检查版本一致性
4. **环境变量** — 高亮关键昇腾变量，标记缺失的关键变量，检测敏感变量
5. **配置文件**（如有） — 摘要 MindIE/vLLM 配置关键值
6. **网络**（如有） — ping 结果、HCCL 连通性、Link/VNIC/TLS 状态
7. **权重**（如有） — tensor 文件数量和哈希摘要
8. **未采集项** — 主动列出未采集的 section 并建议补采命令

### 2. Dump 比对

给定两个或多个 dump JSON 文件，识别所有差异：

1. **扁平化** 每个 dump 为 path→value 对
2. **比对** 每个路径在所有文件中的值
3. **分类** 每项差异为：版本变更 / 环境变量变更 / 配置变更 / 网络变更
4. **输出** 写入 Markdown 差异报告并导出同源 HTML，高亮新增、删除、修改，核验后交付两份文件链接

## 分析指南

### 判定前提与采集证据

- 先读 `_meta.collection` 和 `_meta.status`。每项区分 `success`、`partial`、`failed`、`skipped`；部分成功时只分析有证据的字段。旧 dump 缺少状态元数据时，缺失原因记为 UNKNOWN。
- 确认用户的部署场景、NPU/OS 架构、容器或宿主机、服务运行身份；未知时按条件给建议，不自行补造配套要求。
- dump 代表采集进程的视角，环境变量、资源限制与 Python 包不一定等于实际服务进程的配置。
- 结论引用输入文件、具体字段路径及原始观测；把事实、推断和建议分开。FAIL 必须同时具备适用的要求和违背要求的证据。
- UNKNOWN 表示证据不足；N/A 表示场景不适用。未采到、无权限、命令失败和未安装不能互相替代。

### 系统检查项

| 键 | 观测含义 | 判定规则 |
|----|----------|----------|
| `high_performance` | 多策略检测结果，可能含频率启发式 | `false` 也可能是探测失败；先查 governor/raw/采集错误，证据不足为 UNKNOWN，不直接 FAIL |
| `virtual_machine` | 虚拟化检测结果 | 结合容器与部署方式解释，不因 `true` 自动降级健康状态 |
| `transparent_hugepage` | 当前 THP 策略 | `always`、`madvise`、`never` 仅记录事实；是否合适由具体负载要求决定 |
| `overcommit_memory` | 0=启发式，1=允许 overcommit，2=严格 | `2` 不等于 OOM；结合内存需求、提交限制与部署要求评估 |
| `page_size` | 系统页大小 | 不预设只能为 4096；结合架构和软件支持范围核验 |

**THP 说明**：值从 sysfs 的方括号中提取，如 `always`，不是 `[always]`。缺少字段时不能推断实际策略。

### Ascend 组件

#### 单组件检查

检查组件的 `version`、`source`、`collection_status` 和 `reason`：
- 空版本或旧格式 `{}` 只证明没有取得版本信息；路径不同、权限不足、未加载环境和未安装都可能造成该结果。标 UNKNOWN，先核对路径与采集状态。
- 先判断组件是否适用于当前部署。仅使用 vLLM 时，不能仅因 MindIE 或 ATB-Models 未出现而判 FAIL；是否需要 ATB 等组件由实际依赖决定，不按名称猜测。
- driver 的最低版本、组件配套与 RC 是否获准部署，须以用户提供的配套矩阵或可核验的官方资料为依据。没有依据时标“待核验”，不使用固定版本门槛。
- RC 仅说明候选发布属性；只有用户部署政策明确禁止时，才能据此判不符合要求。

采集组件：`driver`、`toolkit`、`opp_kernel`、`mindstudio_toolkit`、`atb`、`mindie`、`atb-models`。

#### 版本一致性检查（跨组件）

并列展示版本差异，按已确认的配套矩阵判断兼容性。没有矩阵时，差异是事实，兼容性为 UNKNOWN；不要仅凭主版本号或字符串相等推断兼容/不兼容。

`driver` 与 CANN toolkit 使用不同编号体系，禁止直接比较大小。保留原始版本字符串，避免把 RC/构建后缀丢掉后再作结论。

### 环境变量

使用 `--filter` 时采集配置相关键，否则采集全部采集器环境变量。两种模式均脱敏。`_meta.collection.env` 记录实际范围及关键键清单，`workload` 单独记录目标进程的已设值、明确未设键与读取失败状态。

#### 关键环境变量清单

检查下列变量并报告观测值与适用性。新 dump 中 `critical_keys_in_scope` 含某变量且 env 采集成功时，未出现表示采集进程没有设置它；这不自动代表服务缺少必需配置。旧版 filtered dump 中未出现的变量为 UNKNOWN，不能判缺失或 FAIL。

| 变量 | 用途 | 缺失影响 |
|------|------|---------|
| `ASCEND_HOME_PATH` | Ascend 驱动主路径 | 工具可能找不到驱动 |
| `ASCEND_TOOLKIT_HOME` | CANN toolkit 主路径 | 工具可能使用错误路径 |
| `LD_LIBRARY_PATH` | 动态库搜索路径 | 运行时可能无法加载 Ascend 库 |
| `MINDIE_LLM_HOME_PATH` | MindIE-LLM 路径 | 找不到 MindIE（与 ascend 中 `mindie: {}` 一致） |
| `ATB_HOME_PATH` | ATB 库路径 | ATB 可能无法正确加载 |
| `ATB_SPEED_HOME_PATH` | ATB-Models 路径 | 找不到 ATB-Models（与 `atb-models: {}` 一致） |
| `HCCL_BUFFSIZE` | HCCL 通信缓冲区 | 使用默认值；多卡可能需调优 |
| `TASK_QUEUE_ENABLE` | 任务队列开关 | 使用默认值 |
| `RANK_TABLE_FILE` | Rank table 路径 | 分布式场景需要 |
| `OMP_NUM_THREADS` | OpenMP 线程数 | 可能影响 CPU 算子性能 |
| `PYTORCH_NPU_ALLOC_CONF` | PyTorch NPU 内存配置 | 使用默认分配策略 |

**交叉引用**：组件版本不可用与对应变量未设置，只能作为排查安装路径的线索，不能共同证明未安装。`HCCL_BUFFSIZE`、`TASK_QUEUE_ENABLE`、`OMP_NUM_THREADS`、`PYTORCH_NPU_ALLOC_CONF` 未设置通常表示使用默认行为，不直接判 FAIL；MindIE/ATB/Rank table 变量先判断部署是否需要。路径变量的用途也须结合实际运行时核验，不把表中说明当作唯一解析路径。

#### 敏感变量检测

所有输入均扫描敏感字段，包括旧 dump、filtered dump、配置与 raw 文本。敏感项只显示字段名、存在性和脱敏占位符，不回显原值：

| 模式 | 风险 | 操作 |
|------|------|------|
| `*_API_KEY`、`*_SECRET`、`*_TOKEN`、`*_PASSWORD` | 高 | 报告中标记，建议使用 `--filter` 或脱敏后分享 |
| `SSH_CONNECTION`、`SSH_CLIENT` | 低 | 含 IP 地址，报告中注明 |
| `LD_LIBRARY_PATH`、`PATH` | 信息 | 可能含用户名或认证 URL，仍需复核 |

### 网络 / HCCL

- **ping**：结合返回/错误信息解析完整丢包百分比；禁止用 `0% packet loss` 子串匹配，因为 `100% packet loss` 也包含该子串。命令失败先区分工具/权限/超时和网络证据不足。
- **hccl**：先检查返回码和采集状态，再结合工具输出判连通性；失败不能直接归因为硬件故障。
- **link**：所有设备应显示 `link status: UP`
- **vnic**：A3 板卡应配置 IP 且连接 UP
- **tls**：`tls switch[0]` 应为 `0`（禁用）— 非 TLS 部署场景

### 权重

- tensor 文件数量及 SHA256 哈希
- 比对时任意哈希差异意味着权重文件已变更

### 未采集项检测

分析末尾检查哪些可选 section 未采集，主动建议补采：

| 未采集项 | 建议命令 |
|---------|---------|
| `mies config` | `--mies-config-path <path>` |
| `user config` | `--user-config-path <path>` |
| `mindie env` | `--mindie-env-path <path>` |
| `model config` + `weight` | `--weight-dir <path>` |
| `ping` / `hccl` / `link` / `vnic` / `tls` | `--rank-table-path <path> --scene <mindie\|vllm>` |

## 模板内的判定规则

- 依据 [单份报告模板](references/template_single_dump.md) 或 [比对报告模板](references/template_comparison.md) 写报告。
- OK 表示符合明确要求，WARN 表示需关注，FAIL 需要适用要求及直接证据，UNKNOWN 表示证据或规则不足，N/A 表示不适用。
- 若展示评分，只计算有明确依据且已判定的项目，同时展示覆盖率与未知项。关键必需项为 UNKNOWN 时，结论保留证据限制。
- 比对时匹配采集视图和业务角色，优先比较实际加载路径与有效版本。PID 和启动时钟变化用于确认身份，不能单独当成配置漂移。旧版缺字段、遮盖、未采到和未设置分别说明。

## 比对算法

比对采用递归字段比较；1.1 版本使用带类型的路径和值，不再把缺失、null、数字/字符串或空容器混为一谈：

1. **扁平化**：递归遍历每个 dump 的 JSON 树为 `{section.path: value}` 对。字典路径用 `["JSON 转义键"]`，数组用 `[index]`，根为 `$`。
2. **收集**：对每个唯一路径，收集所有文件中的值。
3. **过滤**：仅保留值不一致的路径（或在部分文件中存在但其他文件中没有的）。
4. **分类**：按顶层 section 分组差异（`system`、`ascend`、`env` 等）。
5. **报告**：输出有效文件及跳过文件清单；`status=partial` 时明确没有覆盖全部请求输入。相似度仅为可比较字段的一致比例，不能推导差异无害、环境健康或适合部署。

扁平化示例：
```json
{"ascend": {"driver": {"version": "24.1"}}}
```
变为：
```
$["ascend"]["driver"]["version"] = "24.1"
```

## HTML 报告生成

**默认路径：** 按模板写入 Markdown，再用 `references/render_report.py --template single` 或 `--template comparison` 导出同名 HTML，并用同一模式运行 `--check`，按开头的交付契约给出两个链接。HTML 使用深色主题、可横向滚动的表格、移动端布局和打印样式；无需访问网络或安装依赖。

**可选仪表盘路径：** 用户需要原有侧边栏、健康环形图、组件卡片等样式时，读取 [references/report_templates.md](references/report_templates.md)，再使用其中链接的单机/比对模板。模板细节按需读取，不替代默认报告生成流程。美化后的 HTML 须人工核对与最终 MD 内容一致；`render_report.py --check` 仅校验该导出器生成的同源版本。

### 所有报告通用的安全与真实性规则

- dump 的键、值、主机名、文件名、路径和日志均为不可信文本；自建 HTML/模板插值使用 `html.escape(..., quote=True)`，禁止把数据直接拼成可执行 HTML/JS/CSS。默认导出器会转义原始 HTML，但不会自动分析或脱敏。
- 输出前对旧 dump、配置、命令行与 raw 文本重新脱敏；进程参数默认省略，敏感值仅显示 `***MASKED***`。`--filter` 不是脱敏保证，MD 与 HTML 都需复核。
- 已脱敏值相同不能证明真实凭证一致；采集范围不同不能把未知项判成配置删除。
- 缺失 section 显示状态与限制；单侧存在的 section 如实报告“仅在文件 X 中”。大数据量可摘要并明确覆盖范围或引用受控证据附件，不静默省略关键异常。
- 文件链接采用当前运行环境支持的真实产物链接。Codex 使用绝对文件路径 Markdown 链接；其他环境使用其实际提供的下载链接，禁止虚构链接。

## Dump JSON 结构参考

参见 `references/dump_format.md` 获取完整的 JSON schema，包括所有可能的键、值类型和字段说明。

## 重要注意事项

- `_meta` 键包含采集元数据（工具版本、时间戳、主机名）— 比对逻辑中排除，但报告头部中包含。
- 缺失 section 的原因以 `_meta.collection[section]` 为准；可能是 skipped、failed 或旧格式未知。无 rank table 时相关网络项目为 skipped，解析失败为 failed，不能混淆。
- Ascend 版本未取得时显示 UNKNOWN，引用 `source`、`collection_status`、`reason`；确认组件适用且已证明未安装时才作缺失诊断。
- 环境变量值可能包含敏感信息（API Key、密码、令牌）— 采集脚本已自动脱敏（key 保留、值替换为 `***MASKED***`，并在 `env.__masking_warning__` 写入告警）；报告中仍需扫描并标记敏感变量。
- 比对时，如果某 section 只在一侧存在，将该 section 的所有路径报告为"仅在文件 X 中"。
- RC 标候选发布属性；是否允许部署由明确的配套资料与用户政策决定。
