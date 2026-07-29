​**作者**​：昇腾实战派
​**知识地图**​：[https://blog.csdn.net/Lumos\_Lovegood/article/details/161601003](https://blog.csdn.net/Lumos_Lovegood/article/details/161601003)

## 背景概述

本文档将介绍基于vLLM-Ascend的Qwen3.5-27B模型在Atlas 800I A3上的单机混部部署实践，包括支持的特性、特性配置、环境信息以及性能测试典型case。

## 背景概述

| 软件版本 | 设备信息 | 组网形态 | 总卡数 | 数据格式 |
| --- | --- | --- | --- | --- |
| 0.18.0 | NPU: Atlas 800I A3-560T, HBM 128G  <br>CPU: Kunpeng 920 (80核-2900MHz)  <br>内存: 32根*64G* 5200MHz  <br>OS: OpenEuler 22.03 LTS-SP4 | Atlas 800I A3单机 | 1 | W8A8C16 |

## 服务化配置

### 低时延

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1
export PYTORCH_NPU_ALLOC_CONF="expandable_segments:True"
export HCCL_IF_IP="xxx"
export HCCL_OP_EXPANSION_MODE="AIV"
export HCCL_BUFFSIZE=1024
export OMP_NUM_THREADS=1
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export TASK_QUEUE_ENABLE=1

vllm serve /mnt/share/weights/Qwen3.5-27B-w8a8-org/ \
    --served-model-name "qwen3.5" \
    --host 0.0.0.0 \
    --port 8000 \
    --data-parallel-size 1 \
    --tensor-parallel-size 2 \
    --max-model-len 18432 \
    --max-num-batched-tokens 16384  \
    --max-num-seqs 128 \
    --gpu-memory-utilization 0.9 \
    --compilation-config '{"cudagraph_capture_sizes":[1,6,12,18,24,30,36,42,48,54,72,78,84,90,96,102,108,144,192], "cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --speculative_config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 5, "enforce_eager": true}' \
    --trust-remote-code \
    --async-scheduling \
    --allowed-local-media-path / \
    --quantization ascend \
    --mm-processor-cache-gb 0 \
    --additional-config '{"enable_cpu_binding":true}'
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2048 | 2048 | MLA：DP1+TP2 | 5000 | 0 | 56 | 14 | 0 | 
| 3500 | 1500 | MLA：DP1+TP2 | 8000 | 0 | 40 | 10 | 0 |
| 16384 | 1024 | MLA：DP1+TP2 | 18432 | 0 | 16 | 4 | 0 |
| 32768 | 512 | MLA：DP1+TP2 | 34304 | 0 | 8 | 2 | 0 |
| 65536 | 1024 | MLA：DP1+TP2 | 67584 | 0 | 8 | 2 | 0 |
| 131072 | 1024 | MLA：DP1+TP2 | 133120 | 0 | 4 | 1 | 0 |

### 高吞吐

```bash
export ASCEND_RT_VISIBLE_DEVICES=0,1
export PYTORCH_NPU_ALLOC_CONF="expandable_segments:True"
export HCCL_IF_IP="xxx"
export HCCL_OP_EXPANSION_MODE="AIV"
export HCCL_BUFFSIZE=1024
export OMP_NUM_THREADS=1
export LD_PRELOAD=/usr/lib/aarch64-linux-gnu/libjemalloc.so.2:$LD_PRELOAD
export TASK_QUEUE_ENABLE=1

vllm serve /mnt/share/weights/Qwen3.5-27B-w8a8-org/ \
    --served-model-name "qwen3.5" \
    --host 0.0.0.0 \
    --port 8000 \
    --data-parallel-size 1 \
    --tensor-parallel-size 2 \
    --max-model-len 18432 \
    --max-num-batched-tokens 16384  \
    --max-num-seqs 128 \
    --gpu-memory-utilization 0.9 \
    --compilation-config '{"cudagraph_capture_sizes":[1,4,8,12,16,24,32,48,56,64,72,84,96,108,112,128,160,172,196,200,212,232,256,272,288,312,328,344,360,384,400,416,432,448,480,512], "cudagraph_mode":"FULL_DECODE_ONLY"}' \
    --speculative_config '{"method": "qwen3_5_mtp", "num_speculative_tokens": 3, "enforce_eager": true}' \
    --trust-remote-code \
    --async-scheduling \
    --allowed-local-media-path / \
    --quantization ascend \
    --mm-processor-cache-gb 0 \
    --additional-config '{"enable_cpu_binding":true}'
```

#### 典型测试用例

| 平均输入 | 平均输出 | 并行策略 | 上下文长度 | Prefix Cache命中率 | 总请求数 | 最大并发数 | 请求频率(req/s) |
| --- | --- | --- | --- | --- | --- | --- | --- |
| 2048 | 2048 | MLA：DP1+TP2 | 5000 | 0 | 180 | 45 | 0 | 
| 3500 | 1500 | MLA：DP1+TP2 | 8000 | 0 | 140 | 35 | 0 |
| 16384 | 1024 | MLA：DP1+TP2 | 18432 | 0 | 56 | 14 | 0 |
| 32768 | 512 | MLA：DP1+TP2 | 34304 | 0 | 24 | 6 | 0 |
| 65536 | 1024 | MLA：DP1+TP2 | 67584 | 0 | 16 | 4 | 0 |
| 131072 | 1024 | MLA：DP1+TP2 | 133120 | 0 | 8 | 2 | 0 |

### 测试命令

参考aisbench官方测试指南。

[aisbench测试命令](https://gitee.com/aisbench/benchmark)

[vllm-ascend社区官网](https://docs.vllm.ai/projects/ascend/en/latest/)

## 特别声明

1. 以上配置均未开启Prefix Cache，若实际生产环境需要使用该特性，参考vLLM-Ascend社区参数指南开启--enable-prefix-caching

