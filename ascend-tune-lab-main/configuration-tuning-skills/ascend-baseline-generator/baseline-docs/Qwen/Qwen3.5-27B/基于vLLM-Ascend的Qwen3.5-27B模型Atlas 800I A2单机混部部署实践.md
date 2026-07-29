​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的Qwen3.5-27B模型在Atlas 800I A2上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case。

## 基本信息

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU：Atlas 800I A2-280T, HBM 64G<br>CPU：Kunpeng 920（48核-2600MHz）<br>内存：24根*32G*3200MHZ<br>OS：Ubuntu 22.04 LTS | Atlas 800I A2单机 | 2 | W8A8C16 |

## 服务化配置

### 低时延/高吞吐

```bash
export ASCEND_RT_VISIBLE_DEVICES="2,3"
export PYTORCH_NPU_ALLOC_CONF="expandable_segments:True"
export HCCL_IF_IP="xxx"
export HCCL_OP_EXPANSION_MODE="AIV"
export HCCL_BUFFSIZE=1024
export OMP_NUM_THREADS=1
echo performance | tee /sys/devices/system/cpu/cpu*/cpufreq/scaling_governor

export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export TASK_QUEUE_ENABLE=1

export VLLM_ASCEND_ENABLE_PREFETCH_MLP=1
export VLLM_ASCEND_ENABLE_DENSE_OPTIMIZE=1
export VLLM_ASCEND_ENABLE_NZ=1
export VLLM_ASCEND_ENABLE_FUSED_MC2=1


vllm serve /home/Qwen3.5-27B-w8a8-mtp \
    --served-model-name "qwen3.5-27B" \
    --host 0.0.0.0 \
    --port 8314 \
    --tensor-parallel-size 2 \
    --max-model-len 262144 \
    --max-num-batched-tokens 8192 \
    --max-num-seqs 128 \
    --gpu-memory-utilization 0.95 \
    --trust-remote-code \
    --async-scheduling \
    --allowed-local-media-path / \
    --quantization ascend \
    --mm_processor_cache_type="shm" \
    --mm-processor-cache-gb 0 \
    --speculative-config '{"num_speculative_tokens": 3, "method":"qwen3_5_mtp", "enforce_eager": true}' \
    --additional-config '{"enable_cpu_binding":true, "multistream_overlap_shared_expert": true, "enable_weight_nz_layout":true}' \
    --compilation-config '{"cudagraph_mode":"FULL_DECODE_ONLY", "cudagraph_capture_sizes":[4,8,12,16,20,24,28,32,36,40,44,48,52,56,60,64,68,72,76,80,84,88,92,96,100,104,108,112,116,120,124,128,132,136,140,144]}' \
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2048 | 2048 | MLA：DP1+TP2 | 262144 | 0 | 44 | 11 | 0 | 
| 3500 | 1500 | MLA：DP1+TP2 | 262144 | 0 | 128 | 32 | 0 |
| 3500 | 1500 | MLA：DP1+TP2 | 262144 | 0 | 32 | 8 | 0 | 
| 16384 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 40 | 10 | 0 |
| 16384 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 12 | 3 | 0 |
| 32768 | 512 | MLA：DP1+TP2 | 262144 | 0 | 16 | 4 | 0 |
| 32768 | 512 | MLA：DP1+TP2 | 262144 | 0 | 8 | 2 | 0 |
| 65536 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 12 | 3 | 0 |
| 65536 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 4 | 1 | 0 |
| 131072 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 8 | 2 | 0 |
| 131072 | 1024 | MLA：DP1+TP2 | 262144 | 0 | 4 | 1 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching

