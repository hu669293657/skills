# 版本清单与参考资料查询

有 dump 时走[版本配套检查](version_compatibility.md)。核对已安装或拟部署的组合时，把已确认的版本、产品系列、CANN 版别和部署范围写入清单。分别记录宿主机 HDK、每个容器的 Toolkit、实际 Python 环境中的包。用安装记录确认发行号，用包元数据确认安装包全版本，保留 RC、post、dev 和本地构建后缀。

```bash
python3 "<SKILL_DIR>/references/check_compatibility.py" --inventory "<WORK_DIR>/inventory.json"
python3 "<SKILL_DIR>/references/check_compatibility.py" --reference "关键词"
```

## 输入约定

将清单保存为 UTF-8 JSON 文件，或用 `--inventory -` 从标准输入读取。可用 `--output <结果.json>` 保存查询结果。版本值使用字符串，未知项可以省略或设为 `null`。

```json
{
  "hardware": "A2",
  "versions": {
    "cann": "9.2.1",
    "hdk": "26.2.RC1",
    "mindie": "3.2.0"
  },
  "checks": [["cann", "hdk", "mindie"]]
}
```

| 字段 | 用法 |
|---|---|
| `hardware` | 系列，例如 `310B`、`310P`、`910`、`A2`、`A3`、`950PR/DT`。只有型号时保留型号并按已确认映射填写系列。 |
| `hardware_variant` | 机型限定。例如 MindSpeed 平台规则中的 `训练`、`推理`，或特定 HDK 规则中的 `950超节点`、`850E超节点/650E服务器`、`350加速卡`。组合值沿用规则标签，需确认具体型号属于标签范围。 |
| `model` | 完整产品型号，用于旧版下载选项。 |
| `cann_edition` | `community` 或 `commercial`。 |
| `architecture`、`install_method` | 如 `AArch64` 与 `离线安装 (Runfile)`，按目标环境、安装方式填写。 |
| `versions` | 组件及版本。用 `torch` 表示上游 PyTorch，`torch_npu` 表示安装包，`torchnpu_release` 表示适配发行号。用 `vllm` 表示上游包，`vllm_ascend` 表示 Ascend 插件，`triton_ascend` 表示 Triton Ascend。 |
| `checks` | 每个数组是一组需要同时匹配的组件。省略时，默认对全部已列组件做一次组合核对。 |
| `workload` | 有已整理依赖配置的组件，例如 `mindspeed_llm`、`vllm_ascend`。填写该组件版本，脚本会按同一依赖配置报告缺失字段。可同时用 `checks` 补查平台、HDK 和单独的安装包关系。 |
| `mode` | 默认 `compatibility`。查询固件驱动下载选项时用 `download`，按条目补齐系列、型号、架构和安装方式。旧版页面的“全部”只对应通用型号列表。 |

整套环境按需分别核对工作负载依赖、适用机型与 HDK、安装包和系统条件。同一组合放入同一个 `checks` 项，独立关系分别列项。用 `--reference` 定位 OS、容器镜像、硬件和 CANN 子包的条目；结果包含匹配条数并返回前 40 条。按发布版本、设备、架构及宿主机或容器视图核对条目的适用条件。

## 结果

| 状态 | 含义 |
|---|---|
| `confirmed` | 请求组合命中了同一条正向规则。多个检查全部命中时，结论覆盖这些检查。下载模式下结论限于下载关系。 |
| `candidate` | 选择条件或通用发布参考提供了候选。 |
| `incompatible` | 命中适用的明确限制。 |
| `unknown` | 缺少版本或范围、规则冲突，或当前快照缺少适用证据。 |
| `not_applicable` | 相应官方发行配置明确将该项标为 N/A。 |

逐项给出实测或拟部署的版本、适用范围、`evidence` 中的规则 ID 和 `missing` 缺少的字段。只有所需维度均有证据时，才能描述为整套配套。候选关系限于同一查询行。分支匹配只覆盖规则允许的数值部分，保留 `RC`、`alpha`、`beta`、`post` 等边界。用结果中的 `snapshot` 与 `revision` 说明资料版本。
