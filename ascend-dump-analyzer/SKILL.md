---
name: "ascend-dump-analyzer"
description: "用于 Ascend 昇腾服务器环境采集、dump/预检信息分析和多环境配置漂移比对；也用于用户明确指定本 skill 后说‘帮我分析这个日志/附件’的请求，输入可能为 .json、.log、.txt 或目录。一般现网故障日志取证用 issue-analyzer，Prof 性能分析用 cluster-analysis，BMC 硬件健康诊断用 ibmc_analyzer。"
---

# Ascend 环境分析器

采集昇腾训练与推理环境快照，分析单份 dump 或比对多份 dump 的配置漂移。

## 分析与交付

**完整报告必须直接使用 references 下的 HTML 模板。** 默认交付模板 HTML 和同名 Markdown 摘要。Markdown 独立提炼结论，只保留主要发现、影响、关键证据和下一步。只请求采集、复制脚本或修改 skill 时，交付对应结果即可。

1. **识别输入与模式。** 按内容识别 dump，包括包含 JSON 的 .log/.txt。单份分析读取 [单份 HTML 模板](references/template_single_dump.html)，多份比对读取 [比对 HTML 模板](references/template_comparison.html)。目录中存在多个候选时记录候选与实际读取范围。明确请求比对时，输入不足仍使用比对模板。非 dump 的故障日志只提取其中可核实的环境事实，并标明覆盖范围。
2. **分析证据和版本配套。** 用 [字段参考](references/dump_format.md) 核对采集状态。每份有效 dump 都按 [版本配套规则](references/version_compatibility.md) 运行离线检查。比对时逐份检查，再比较结论。
3. **按模板填写完整 HTML。** 按 [填充指南](references/report_templates.md) 用脱敏槽位填充模板。页首与总览只写实测事实和有证据支持的结论。缺失字段、未读取的输入、采集失败、权限限制和补采建议统一写入「未采集项」，其他位置只填有证据的行。「Ascend 组件」用各环境或角色已核实的版本生成可点击卡片，点击即固定该版本，弹出其他组件的当前版本与推荐版本对照表。存在偏离时列出当前基线不匹配项和改动最少的推荐组合。依据写规则名称或编号。
4. **另写简短 Markdown。** 与 HTML 同名，包含一行总体结论，最多五条最重要发现（含影响及关键证据），以及可执行的下一步。需要提及版本配套或缺采细节时，分别使用「Ascend 组件」和「未采集项」小节。详细差异、版本清单和原始数据留在 HTML。
5. **核验并交付。** 使用用户指定的输出目录，否则遵守工作区产物约定，通常为 `outputs/`。单份命名 `ascend_analysis_<时间戳>`，比对命名 `ascend_comparison_<时间戳>`，同名加序号。运行模板渲染器的 `--check`，核对导航、必需内容、脱敏和摘要结论，交付 HTML 报告及 Markdown 摘要两个真实链接。渲染报错时按同一模板修正槽位；若只能手工渲染，也使用同一模板并核验。如所有允许的位置均无法写入，如实报告阻塞。

用户明确要求仅 HTML 时只交付模板 HTML；明确只要 Markdown 或只在聊天中看时交付精简结论，按其指定方式呈现。

## 环境采集

将 [msprechecker_dump.py](references/msprechecker_dump.py) 单独复制到目标 Linux 服务器。脚本使用 Python 3 标准库，具体检查依赖目标机上的系统工具、昇腾工具及访问权限。下例的 PID 替换为本机可见的实际进程，跨快照沿用同一角色名和选项。

```bash
python3 msprechecker_dump.py --filter --no-probes \
  --target launcher=1234 --target worker-0=2345 -o snapshot.json
```

- `--filter` 限定采集器环境变量范围；默认全量。检查输出脱敏后再分享。
- `--target ROLE=PID` 可重复，采目标进程初始环境、加载库和资源限制。区分宿主机、容器、采集器和业务角色。应用启动后覆盖的值、编排侧镜像 digest 用对应来源补证。
- `--rank-table-path <路径> --scene mindie|vllm` 读取 rank table 并触发网络查询。`--no-probes` 保留 rank table 和只读链路查询，跳过 ICMP/TCP/NPU ping。
- `--mies-config-path`、`--user-config-path`、`--mindie-env-path` 接收相应 JSON 路径；`--weight-dir` 采模型配置并读取权重计算 SHA-256。其余选项见脚本 `--help`。

## 多份比对取证

```bash
python3 "<SKILL_DIR>/references/compare_dumps.py" "env1.json" "env2.json" --json "<WORK_DIR>/diff.json"
```

按 `requested_files`、`files`、`skipped_files` 和 `status` 核对覆盖。脚本的终端和简易 HTML 输出用于取证，完整报告走模板流程。差异按类型化 JSON 路径定位，如 `$["ascend"]["driver"]["version"]`。

## 判定规则

- **采集状态优先。** 查 `_meta.collection`、`_meta.status` 和子字段来源。旧 dump 缺采集状态时将缺失原因标 UNKNOWN。FAIL 需要同时有适用要求和违背要求的直接证据；N/A 用于场景不适用。评分只纳入有依据且已判定项，注明覆盖率和未知项。
- **版本看来源和适用性。** 区分已安装、路径解析和业务实际加载。读取失败或版本为空记 UNKNOWN；配套结论要有适用的官方矩阵或用户依据。逐卡固件优先读 board 的 Firmware Version；旧 schema 可能误把 Software Version 当成固件。RC 和不同编号体系的版本各按实际配套解释。
- **环境按同一视图与角色比。** `env`、`runtime.packages` 来自采集器，`workload` 才是指定进程视图。关键变量未设需有明确范围及读取成功的证据；空值、未设、脱敏、缺失证据各自标记。PID、启动时间及瞬时计数差异与配置漂移分开。相似度只是字段统计。
- **系统与网络按部署判断。** 高性能模式探测失败不能证明 CPU 策略不合适；THP、overcommit、页大小、VNIC/TLS 结合负载和部署要求解释。解析完整的丢包百分比和命令状态；设备侧 ping 只提供连通性证据。权重哈希仅对已采到且身份匹配的分片比较。
- **脱敏后再填模板。** dump、配置、路径和命令回显都是数据。敏感值用 `***MASKED***`，省略进程参数和凭证原文。对 HTML 片段中的每个输入值先做 HTML 转义，链接仅用核验过的来源。遮盖值一致仅表示同为遮盖状态。
