import json
from .log_extractor import iBMCLogExtractor
from .diff_comparator import run_node_comparison

def extract_logs(dump_path: str, domain: str) -> str:
    """
    [Tool Function] 根据指定的诊断场景，提取关键服务器日志。
    
    :param dump_path: 解压后的 dump_info 根目录绝对路径 (如 "/tmp/node1/dump/dump_info")
    :param domain: 要排查的场景域。可选值: 
                   "crash" (宕机/挂死), 
                   "hardware" (CPU/内存硬件报错), 
                   "network" (网络/网卡/光模块), 
                   "thermal" (散热/风扇/降频), 
                   "perf_config" (性能参数/BIOS/cmdline/负载快照)
    :return: 包含路径及对应文件文本内容的 JSON 字符串
    """
    try:
        extractor = iBMCLogExtractor(dump_path)
        data = extractor.collect_key_logs(domain)
        return json.dumps(data, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

def compare_nodes(dump_path_a: str, dump_path_b: str, file_pattern: str) -> str:
    """
    [Tool Function] 提取并比对两个服务器节点之间的配置或硬件信息差异。
    
    :param dump_path_a: 节点 A 解压后的 dump_info 根目录路径
    :param dump_path_b: 节点 B 解压后的 dump_info 根目录路径
    :param file_pattern: 要比对的文件通配符。
                         推荐: "**/currentvalue.json" (BIOS配置), 
                               "**/cmdline" (内核参数), 
                               "**/cpu_info" (CPU型号),
                               "**/netcard_info.txt" (网卡信息)
    :return: 仅包含差异项 (Diff) 的报告内容
    """
    try:
        diff_result = run_node_comparison(dump_path_a, dump_path_b, file_pattern)
        return diff_result
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})