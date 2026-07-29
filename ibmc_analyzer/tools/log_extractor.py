import os
import glob
import tarfile
import gzip
import json

class iBMCLogExtractor:
    # 故障域与文件 Pattern 映射表
    DOMAINS = {
        "crash": [
            "**/OSDump/systemcom.tar*",
            "**/LogDump/fdm_output*",
            "**/sensor_alarm/current_event.txt",
            "**/sensor_alarm/sel.db*",
            "**/LogDump/dmesg_info*",
            "**/sysinfo/ps_info"
        ],
        "hardware": [
            "**/CpuMem/cpu_info",
            "**/CpuMem/mem_info",
            "**/CpuMem/npu_info",
            "**/CpuMem/npu_ecc_info.json",
            "**/LogDump/*dfx_reg_log*",
            "**/storage/SubhealthyStatus.db",
            "**/StorageMgnt/RAID_Controller_Info.txt",
            "**/LogDump/PD_SMART_INFO_C*"
        ],
        "network": [
            "**/NpuIO/optical_module_history_info_log.csv",
            "**/NpuIO/port_history_log.tar.gz",
            "**/networkinfo/ifconfig_info",
            "**/BMC/lldp_info.txt",
            "**/netcard/netcard_info.txt"
        ],
        "thermal": [
            "**/cooling_app/fan_info.txt",
            "**/pram/env_web_view.dat",
            "**/pram/cpu_utilise_webview.dat",
            "**/BMC/psu_info.txt"
        ],
        # 新增：性能与配置排查域，抓取软硬件底层运行状态
        "perf_config": [
            "**/BIOS/currentvalue.json",      # BIOS 配置 (如 NUMA, 虚拟化, 功耗模式)
            "**/sysinfo/cmdline",             # OS 内核启动参数 (绑核, 大页等)
            "**/sysinfo/top_info",            # CPU 负载快照
            "**/sysinfo/meminfo",             # 内存使用详情快照
            "**/sysinfo/zoneinfo",            # 内存 NUMA 节点分布
            "**/versioninfo/server_config.txt",# 服务器综合硬件配置
            "**/CpuMem/npu_info",             # 算力卡硬件参数
            "**/driver_info/lsmod_info",      # 内核驱动加载状态
            "**/networkinfo/netstat_info"     # 端口与网络队列状态
        ]
    }

    IGNORE_EXTENSIONS = ('.md5', '.sha256', '.bak')

    def __init__(self, base_dump_path: str):
        self.base_dump_path = base_dump_path

    def collect_key_logs(self, domain: str) -> dict:
        patterns = self.DOMAINS.get(domain.lower(), [])
        if not patterns:
            # 默认返回配置和告警基本面
            patterns = self.DOMAINS["perf_config"] + ["**/sensor_alarm/current_event.txt"]

        extracted_data = {}
        for pattern in patterns:
            full_pattern = os.path.join(self.base_dump_path, pattern)
            matched_files = glob.glob(full_pattern, recursive=True)

            for file_path in matched_files:
                if file_path.endswith(self.IGNORE_EXTENSIONS) or os.path.isdir(file_path):
                    continue
                
                rel_path = os.path.relpath(file_path, self.base_dump_path)
                extracted_data[rel_path] = self._read_file_content(file_path)

        return extracted_data

    def _read_file_content(self, file_path: str, max_bytes: int = 200000) -> str:
        """安全读取文本，穿透 tar/gz 压缩包，防爆存限流"""
        try:
            if file_path.endswith('.gz') and not file_path.endswith('.tar.gz'):
                with gzip.open(file_path, 'rt', encoding='utf-8', errors='ignore') as f:
                    return f.read(max_bytes)
            elif file_path.endswith('.tar.gz') or file_path.endswith('.tar'):
                with tarfile.open(file_path, 'r:*') as tar:
                    summary = []
                    # 仅提取前3个文本文件，避免打包了整个 OS
                    for member in tar.getmembers()[:3]:
                        if member.isfile():
                            f = tar.extractfile(member)
                            if f:
                                summary.append(f"--- Archive: {member.name} ---")
                                summary.append(f.read(max_bytes // 3).decode('utf-8', errors='ignore'))
                    return "\n".join(summary)
            else:
                with open(file_path, 'r', encoding='utf-8', errors='ignore') as f:
                    return f.read(max_bytes)
        except Exception as e:
            return f"[Error: {str(e)}]"