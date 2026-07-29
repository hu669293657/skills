​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的MiniMax-M2.5模型在Atlas 800I A3上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case。

## 基本信息

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU：Atlas 800I A3-560T，HBM 128G<br>CPU：Kunpeng 920（80核-2900MHz）<br>内存：32根*64G* 5200MHZ<br>OS：OpenEuler 22.03 LTS-SP4 | Atlas 800I A3单机 | 8 | W8A8C16 |

## 服务化配置

### 低时延/高吞吐

```bash
nic_name="xxx" 
nic_name="xxx" 
local_ip=xxx 
export HCCL_OP_EXPANSION_MODE="AIV"
export HCCL_IF_IP=$local_ip
export GLOO_SOCKET_IFNAME=$nic_name
export TP_SOCKET_IFNAME=$nic_name
export HCCL_SOCKET_IFNAME=$nic_name
export HCCL_BUFFSIZE=512
export PYTORCH_NPU_ALLOC_CONF=expandable_segments:True
export VLLM_ASCEND_ENABLE_FUSED_MC2=1 
export OMP_NUM_THREADS=1
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor
sysctl -w vm.swappiness=0
sysctl -w kernel.numa_balancing=0
sysctl kernel.sched_migration_cost_ns=50000
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export TASK_QUEUE_ENABLE=1
export VLLM_ASCEND_ENABLE_FLASHCOMM1=1
export VLLM_ASCEND_BALANCE_SCHEDULING=1
export VLLM_ASCEND_ENABLE_NZ

vllm serve /mnt/share/weight/MiniMax-M2.5-w8a8-QuaRot \
    --served-model-name "minimax" \
    --host 0.0.0.0 \
    --port 8004 \
    --tensor-parallel-size 8 \
    --data-parallel-size 1 \
    --enable-expert-parallel \
    --no-enable-prefix-caching \
    --async-scheduling \
    --max-num-seqs 32 \
    --max-model-len 196608 \
    --max-num-batched-tokens 16384 \
    --gpu-memory-utilization 0.85 \
    --trust-remote-code \
    --quantization ascend \
    --no-enable-prefix-caching \
    --compilation-config '{"cudagraph_mode": "FULL_DECODE_ONLY"}' \
    --additional-config '{"enable_cpu_binding":true}' \
    --speculative_config '{"method": "eagle3", "model": "/mnt/share/weight/MiniMax-M2-Eagle3-1/", "num_speculative_tokens": 3}'
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- | 
| 2048 | 2048 | MLA：DP1+TP8 | 196608 | 0 | 512 | 128 | 0 | 
| 2048 | 2048 | MLA：DP1+TP8 | 196608 | 0 | 100 | 25 | 0 | 
| 3500 | 1500 | MLA：DP1+TP8 | 196608 | 0 | 512 | 128 | 0 | 
| 3500 | 1500 | MLA：DP1+TP8 | 196608 | 0 | 100 | 26 | 0 |
| 16384 | 1024 | MLA：DP1+TP8 | 196608 | 0 | 120 | 30 | 0 | 
| 16384 | 1024 | MLA：DP1+TP8 | 196608 | 0 | 16 | 4 | 0 | 
| 32768 | 512 | MLA：DP1+TP8 | 196608 | 0 | 36 | 9 | 0 | 
| 32768 | 512 | MLA：DP1+TP8 | 196608 | 0 | 4 | 1 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching
2. eagle 权重下载路径：https://www.modelscope.cn/models/Eco-Tech/MiniMax-M2.7-eagle-model-short

