# 昇腾 A2、A3、A5 软件版本文件

硬件型号决定要配套的算子包。定位版本文件还需要知道安装用户、安装方式和选中的 CANN 版本。华为的版本查询接口对 Atlas A2、Atlas A3 和 Ascend 950PR/950DT 均支持按软件包名称读取版本。[CANN 9.1 版本查询接口](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/API/runtimeapi/aclcppdevg_03_2096.html)

## 产品与算子包

| 产品 | Conda 的 ops 包名 |
| --- | --- |
| Atlas A2 训练 / 推理系列（下行产品单列） | `cann-910b-ops` |
| Atlas 200I/500 A2 推理产品 | `cann-310b-ops` |
| Atlas A3 系列 | `cann-a3-ops` |
| Ascend 950PR / 950DT，Ascend RecSDK 中标作 A5 | `cann-950-ops` |

表中采用 CANN 9.2.0-beta.2 的 Conda 包名。该版安装文档要求 Toolkit 和对应芯片的 ops 装在同一路径，多种芯片的 ops 可随各自的 Toolkit 分目录安装。[华为安装说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/920beta2/softwareinst/instg/instg_0093.html) · [Ascend RecSDK 芯片标识](https://github.com/Ascend/RecSDK/blob/develop/docker/OVERVIEW.zh.md#镜像-tag-中关键字段描述)

## 定位安装目录

| 安装方式 | 从哪里找 |
| --- | --- |
| 系统或个人 run 包 | root 默认前缀为 `/usr/local/Ascend`，普通用户为安装用户的 `$HOME/Ascend`。自定义安装时，CANN 的 `Install_Path` 见 `/etc/Ascend/ascend_cann_install.info` 或安装用户的 `$HOME/Ascend/ascend_cann_install.info`。[安装参数](https://www.hiascend.com/doc_center/source/en/canncommercial/800/softwareinst/instg/instg_0043.html) · [配置键与文件位置](https://www.hiascend.com/doc_center/source/en/canncommercial/850/softwareinst/instg/instg_0032.html) |
| 版本入口 | CANN 8.2 文档使用前缀下的 `ascend-toolkit/latest`。8.5 文档使用 `cann`，9.1 也提供 `cann-<version>` 的示例。若装了多个版本，应以所选环境或安装记录为依据。[CANN 8.2 安装指南 §10.2](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/82RC1alpha001/softwareinst/instg/CANN%E7%A4%BE%E5%8C%BA%E7%89%88%208.2.RC1.alpha001%20%E8%BD%AF%E4%BB%B6%E5%AE%89%E8%A3%85%E6%8C%87%E5%8D%97%2001.pdf) · [CANN 8.5 路径表](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/850/softwareinst/instg/instg_0044.html) · [CANN 9.1 版本查询](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/softwareinst/instg/instg_0064.html) |
| Conda 环境 | Conda 安装的 Toolkit 和 NNAL 可位于当前环境的 `Ascend/` 内。示例中的 Toolkit 入口为 `/home/miniconda3/Ascend/cann`。[Conda 安装说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/920beta2/softwareinst/instg/instg_0093.html) |
| pip 环境 | 华为云镜像把 CANN 8.5 安装在 `<环境>/lib/python3.9/site-packages/ascend/cann-8.5.0`，并用 `ASCEND_HOME_PATH` 和 `ASCEND_TOOLKIT_HOME` 指向该目录。实际路径取决于 Python 环境和所装版本。[华为云 ModelArts 镜像示例](https://support.huaweicloud.com/intl/en-us/docker-modelarts/modelarts_23_0220.html) |

对于旧版运行环境，华为还给出了 `<前缀>/nnrt/latest` 和 `<前缀>/nnae/latest` 两种独立入口。采集器只在这些入口下确实找到对应组件的版本文件时才记录该组件的版本。[华为运行环境说明](https://www.hiascend.com/doc_center/source/en/canncommercial/601/inferapplicationdev/aclcppdevg/aclcppdevg_01_0004.html)

## 每个文件能证明什么

| 对象 | 版本证据和适用范围 |
| --- | --- |
| Toolkit 组合包 | 在选中的 CANN 目录下，`<arch>-linux/ascend_toolkit_install.info` 记录组合包版本、主机 CPU 架构和安装位置。`<arch>` 常见为 `aarch64` 或 `x86_64`。与之相邻的安装参数文件 `install.conf` 用途不同。[8.5 安装记录](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/850/softwareinst/instg/instg_0044.html) |
| Runtime、HCCL 等组件 | CANN 定义的包名对应 `<CANN目录>/share/info/<包名>`，各包以自己的 `version.info` 为准。HCCL 支持独立升级。ASC DevKit 属于 Toolkit 的一个子包，其版本文件只能直接证明这个子包的版本。[CANN 版本接口](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/API/runtimeapi/aclcppdevg_03_2096.html) · [CANN 8.5 子包配套表](https://www.hiascend.com/doc_center/source/zh/canncommercial/850/releasenote/CANN%E5%95%86%E7%94%A8%E7%89%88%208.5.0%20%E7%89%88%E6%9C%AC%E8%AF%B4%E6%98%8E%E4%B9%A6%2001.pdf) |
| 驱动 | `/etc/ascend_install.info` 中的 `Driver_Install_Path_Param` 给出驱动安装前缀，默认版本文件为 `/usr/local/Ascend/driver/version.info`。容器挂载可改变文件可见性。华为还记录过自定义安装时默认位置残留同名空目录的情况。[驱动路径配置](https://www.hiascend.com/doc_center/source/en/canncommercial/850/maintenref/troubleshooting/troubleshooting_0221.html) · [昇腾现场案例](https://www.hiascend.com/document/caselibrary/detail/topic_0000002273193421) |
| ATB | NNAL 安装指南给出的 ATB 版本证据是 `<安装前缀>/nnal/atb/latest/version.info`。NNAL 为可选加速库。[CANN 9.1 版本查询](https://www.hiascend.com/doc_center/source/en/CANNCommunityEdition/910/softwareinst/instg/instg_0064.html) · [NNAL 安装说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/920beta2/softwareinst/instg/instg_0093.html) |
| MindIE | 昇腾官方镜像以 `<安装前缀>/mindie/latest/version.info` 展示套件及各组件版本，首行示例为 `Ascend-mindie : MindIE 1.0.0`。`mindie/latest/mindie-service/version.info` 记录 Service 组件的版本。[Ascend 官方镜像说明](https://gitee.com/ascend/ascend-docker-image/blob/dev/mindie/README.md) · [MindIE 2.0 安装指南，第 58 页](https://www.hiascend.com/doc_center/source/zh/mindie/20RC1/envdeployment/instg/MindIE%202.0.RC1%20%E5%AE%89%E8%A3%85%E6%8C%87%E5%8D%97%2001.pdf) |

### 现场如何核对一个新布局

先在运行用户的环境中确认当前 Toolkit 目录。`ASCEND_HOME_PATH` 和 `ASCEND_TOOLKIT_HOME` 都表示 Toolkit 安装后的目录，`ASCEND_OPP_PATH` 单独指向算子库根目录。这些变量属于当前进程环境，sudo 启动的采集进程可能拿到不同的值。[华为环境变量说明](https://www.hiascend.com/doc_center/source/zh/CANNCommunityEdition/910/maintenref/envvar/envref_07_0003.html)

以下 Bash 命令列出所选安装中实际存在的版本文件，包括旧组件目录和现代包索引。将 `cann_dir` 设为现场确认的绝对路径，同机有多套安装时逐套核对。

```bash
cann_dir=/usr/local/Ascend/cann
for file in "$cann_dir"/*/version.info "$cann_dir"/share/info/*/version.info \
    "$cann_dir/$(uname -m)-linux/ascend_toolkit_install.info"; do
  [[ -f "$file" ]] && printf '%s\n' "$file"
done
```

将实际路径、包名、文件中的版本字段和 Manifest 的 `source` 一起核对。为新布局补用例时，让 Toolkit、Runtime、HCCL 和 ops 的版本各不相同，能同时检出漏采与错取其他组件版本。ops 整包的安装记录是 `<arch>-linux/ascend_ops_install.info`，OPP 的记录应从算子库自身的 `version.info` 取得。[华为整包版本查询](https://www.hiascend.com/doc_center/source/zh/canncommercial/850/softwareinst/instg/instg_0064.html) · [OPP 版本文件](https://www.hiascend.com/document/detail/zh/mindsdk/501/index/indexug/mxindexfrug_0431.html)

## 此次漏采的现场样本

据采集方记录，这台机器的 `/usr/local/Ascend/cann` 链接到 `cann-9.0.0`，`ascend-toolkit/latest` 也指向该安装。实际存在 `cann/share/info/{runtime,hccl,asc-devkit}/version.info`，三份文件均写有 `Version=9.2.0`。另有 `cann/{compiler,opp}/version.info` 和 `driver/version.info`。这个样本说明版本目录名和组件版本值可不同。采集方未提供该机器的硬件型号，不能据此确认三代硬件的实机覆盖。
