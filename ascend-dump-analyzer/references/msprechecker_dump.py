#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
msprechecker_dump.py — 独立环境信息采集脚本

从 msprechecker 项目中提取的 dump 功能，用于在昇腾 NPU 服务器上
快速采集系统、环境、Ascend 组件、配置文件、网络拓扑、权重哈希等信息，
并保存为 JSON 快照文件，便于后续比对分析。

本脚本仅依赖 Python 标准库，无需安装 msprechecker 或 msguard/psutil。

用法:
    python3 msprechecker_dump.py                                          # 采集默认信息
    python3 msprechecker_dump.py -o /tmp/snapshot.json                    # 指定输出路径
    python3 msprechecker_dump.py --filter                                 # 仅采集昇腾相关环境变量
    python3 msprechecker_dump.py --mies-config-path /path/to/config.json  # 额外采集 MindIE 配置
    python3 msprechecker_dump.py --rank-table-path /path/to/rank_table.json --scene mindie
    python3 msprechecker_dump.py --weight-dir /path/to/weights --chunk-size 64
"""

import os
import re
import sys
import json
import stat
import time
import shutil
import hashlib
import shlex
import platform
import subprocess
import ipaddress
import itertools
import argparse
from enum import Enum
from dataclasses import dataclass, field
from typing import Dict, List, Tuple, Optional, Union
from concurrent.futures import ThreadPoolExecutor, ProcessPoolExecutor, as_completed

__version__ = "1.0.0"

# ============================================================================
# 颜色输出
# ============================================================================

class Color:
    RED = "\033[91m"
    YELLOW = "\033[93m"
    CYAN = "\033[96m"
    GREEN = "\033[92m"
    RESET = "\033[0m"
    BOLD = "\033[1m"


def info(msg, *args):
    print(f"{Color.GREEN}[INFO]{Color.RESET} {msg.format(*args) if args else msg}")

def warn(msg, *args):
    print(f"{Color.YELLOW}[WARNING]{Color.RESET} {msg.format(*args) if args else msg}", file=sys.stderr)

def error(msg, *args):
    print(f"{Color.RED}[ERROR]{Color.RESET} {msg.format(*args) if args else msg}", file=sys.stderr)


# ============================================================================
# 简易版本比较（替代 packaging.version.Version）
# ============================================================================

class SimpleVersion:
    """简易版本号比较，支持 major.minor[.patch] 格式"""

    def __init__(self, version_str: str):
        self._raw = str(version_str).strip()
        parts = re.split(r'[.\-+]', self._raw)
        self._parts = []
        for p in parts:
            m = re.match(r'(\d+)', p)
            if m:
                self._parts.append(int(m.group(1)))
            else:
                break
        if not self._parts:
            self._parts = [0]

    def __ge__(self, other):
        if isinstance(other, str):
            other = SimpleVersion(other)
        return self._parts >= other._parts

    def __gt__(self, other):
        if isinstance(other, str):
            other = SimpleVersion(other)
        return self._parts > other._parts

    def __eq__(self, other):
        if isinstance(other, str):
            other = SimpleVersion(other)
        return self._parts == other._parts

    def __repr__(self):
        return f"Version('{self._raw}')"


# ============================================================================
# NPU 工具函数
# ============================================================================

HCCN_TOOL_CMD = "/usr/local/Ascend/driver/tools/hccn_tool"


def get_npu_count() -> int:
    """通过 /dev/davinciN 设备文件检测 NPU 数量"""
    for device_id in itertools.count(0):
        device_path = f"/dev/davinci{device_id}"
        try:
            f_mode = os.stat(device_path).st_mode
        except Exception:
            break
        if not stat.S_ISCHR(f_mode):
            break
    return device_id


def is_in_container() -> bool:
    """检测是否运行在容器中"""
    if os.path.exists('/.dockerenv'):
        return True
    try:
        with open('/proc/1/sched', 'r') as f:
            first_line = f.readline()
        if first_line and first_line.startswith('systemd'):
            return False
        return True
    except Exception:
        return True


def which(cmd: str) -> Optional[str]:
    """安全版 shutil.which"""
    return shutil.which(cmd)


# ============================================================================
# Rank Table 解析
# ============================================================================

class Framework(Enum):
    MINDIE = "mindie"
    VLLM = "vllm"


@dataclass
class DeviceInfo:
    device_ip: Union[ipaddress.IPv4Address, ipaddress.IPv6Address]
    device_id: int
    rank_id: int


@dataclass
class RankTable:
    host_to_devices: Dict
    server_count: int
    version: SimpleVersion


class RankTableParseError(ValueError):
    pass


_HOST_LIMIT = 1000
_DEVICE_LIMIT_PER_HOST = 32


def _load_json(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception as exc:
        raise RankTableParseError(f"无法加载 JSON 文件: {path!r}") from exc


def _parse_mindie_rank_table(path: str) -> RankTable:
    data = _load_json(path)

    if "server_list" not in data:
        raise RankTableParseError(f"rank table 中未找到 'server_list': {path!r}")
    if "server_count" not in data:
        raise RankTableParseError(f"rank table 中未找到 'server_count': {path!r}")

    host_to_devices = {}
    for host_num, server_info in enumerate(data["server_list"]):
        if host_num >= _HOST_LIMIT:
            raise RankTableParseError(f"主机数量超过限制 {_HOST_LIMIT}")

        host_ip_str = server_info.get("server_id", "")
        device_list = server_info.get("device", [])

        if not host_ip_str or not device_list:
            continue

        try:
            host_ip = ipaddress.ip_address(host_ip_str)
        except ValueError:
            warn("无效的 server_id: {!r}, 跳过", host_ip_str)
            continue

        if host_ip not in host_to_devices:
            host_to_devices[host_ip] = []

        for dev_num, dev_info in enumerate(device_list):
            if dev_num >= _DEVICE_LIMIT_PER_HOST:
                raise RankTableParseError(
                    f"主机 {host_ip_str!r} 的设备数量超过限制 {_DEVICE_LIMIT_PER_HOST}"
                )
            try:
                device_ip = ipaddress.ip_address(dev_info.get("device_ip", ""))
                device_id = int(dev_info.get("device_id", ""))
                rank_id = int(dev_info.get("rank_id", ""))
            except (ValueError, TypeError):
                continue
            host_to_devices[host_ip].append(
                DeviceInfo(device_ip=device_ip, device_id=device_id, rank_id=rank_id)
            )

    if not host_to_devices:
        raise RankTableParseError(f"rank table 中未解析出任何设备: {path!r}")

    server_count = data["server_count"]
    if isinstance(server_count, str):
        server_count = int(server_count) if server_count.isdigit() else 0

    return RankTable(
        host_to_devices=host_to_devices,
        server_count=server_count,
        version=SimpleVersion(data.get("version", "1.0")),
    )


def _parse_vllm_rank_table(path: str) -> RankTable:
    data = _load_json(path)

    if "prefill_device_list" not in data or "decode_device_list" not in data:
        raise RankTableParseError(
            f"vllm rank table 中需要 'prefill_device_list' 和 'decode_device_list': {path!r}"
        )

    host_to_devices = {}
    for device_list in [data["prefill_device_list"], data["decode_device_list"]]:
        if device_list is None:
            continue
        for dev in device_list:
            host_ip_str = dev.get("server_id", "")
            try:
                host_ip = ipaddress.ip_address(host_ip_str)
            except ValueError:
                continue

            if host_ip not in host_to_devices:
                if len(host_to_devices) >= _HOST_LIMIT:
                    raise RankTableParseError(f"主机数量超过限制 {_HOST_LIMIT}")
                host_to_devices[host_ip] = []

            if len(host_to_devices[host_ip]) >= _DEVICE_LIMIT_PER_HOST:
                raise RankTableParseError(
                    f"主机 {host_ip_str!r} 的设备数量超过限制 {_DEVICE_LIMIT_PER_HOST}"
                )

            try:
                device_ip = ipaddress.ip_address(dev.get("device_ip", ""))
                device_id = int(dev.get("device_id", ""))
                cluster_id = int(dev.get("cluster_id", "1"))
            except (ValueError, TypeError):
                continue

            host_to_devices[host_ip].append(
                DeviceInfo(device_ip=device_ip, device_id=device_id, rank_id=cluster_id - 1)
            )

    if not host_to_devices:
        raise RankTableParseError(f"rank table 中未解析出任何设备: {path!r}")

    server_count = data.get("server_count", len(host_to_devices))
    if isinstance(server_count, str):
        server_count = int(server_count) if server_count.isdigit() else 0

    return RankTable(
        host_to_devices=host_to_devices,
        server_count=server_count,
        version=SimpleVersion(data.get("version", "1.0")),
    )


def parse_rank_table(path: str, framework: Union[str, Framework]) -> RankTable:
    """解析 rank table 文件"""
    if isinstance(framework, str):
        framework = Framework(framework)

    if framework == Framework.MINDIE:
        return _parse_mindie_rank_table(path)
    elif framework == Framework.VLLM:
        return _parse_vllm_rank_table(path)
    else:
        raise ValueError(f"不支持的框架: {framework!r}")


# ============================================================================
# 采集器
# ============================================================================

class CollectorResult:
    """采集结果容器"""
    def __init__(self, data, collect_type: str, errors: list = None):
        self.data = data
        self.collect_type = collect_type
        self.errors = errors or []

    @property
    def has_errors(self) -> bool:
        return len(self.errors) > 0


class SysCollector:
    """系统信息采集器 — 采集 CPU、内核、内存、虚拟化等信息"""

    def __init__(self):
        self.collect_type = "system"

    def _collect_lscpu(self) -> dict:
        try:
            output = subprocess.check_output(
                ['/usr/bin/lscpu'], stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            return {}

        info = {}
        for line in output.splitlines():
            if ':' not in line:
                continue
            key, value = [x.strip() for x in line.split(':', 1)]
            if key in ("Model name", "型号名称"):
                info["model_name"] = value
            elif key == "BIOS Model name" and "model_name" not in info:
                info["model_name"] = value
        return info

    def _collect_virtual_machine(self) -> dict:
        keywords = ["hypervisor", "vmware", "virtualbox", "kvm", "xen"]
        try:
            with open("/proc/cpuinfo", 'r') as f:
                for line in f:
                    if any(kw in line.lower() for kw in keywords):
                        return {"virtual_machine": True}
        except Exception:
            pass
        return {"virtual_machine": False}

    def _collect_cpu_high_performance(self) -> dict:
        """检测 CPU 是否处于高性能模式（多策略降级）"""
        strategies = [
            self._check_scaling_governor,
            self._check_dmidecode,
            self._check_cpupower,
            self._check_psutil,
            self._check_lshw,
        ]
        for strategy in strategies:
            try:
                if strategy():
                    return {"high_performance": True}
            except Exception:
                continue
        return {"high_performance": False}

    def _check_scaling_governor(self) -> bool:
        cpu_count = os.cpu_count()
        if not cpu_count:
            return False
        for core_id in range(cpu_count):
            gov_path = f'/sys/devices/system/cpu/cpu{core_id}/cpufreq/scaling_governor'
            try:
                with open(gov_path, 'r') as f:
                    if f.read().strip() != "performance":
                        return False
            except Exception:
                return False
        return True

    def _check_dmidecode(self) -> bool:
        try:
            output = subprocess.check_output(
                shlex.split("dmidecode -t processor"),
                stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            return False
        max_speeds = []
        current_speeds = []
        for line in output.splitlines():
            m = re.search(r'Max Speed:\s*(.+)', line, re.IGNORECASE)
            if m:
                max_speeds.append(m.group(1).strip())
            m = re.search(r'Current Speed:\s*(.+)', line, re.IGNORECASE)
            if m:
                current_speeds.append(m.group(1).strip())
        return bool(max_speeds and current_speeds and max_speeds == current_speeds)

    def _check_cpupower(self) -> bool:
        try:
            output = subprocess.check_output(
                shlex.split("cpupower frequency-info"),
                stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            return False
        max_m = re.search(r'hardware limits:\s*[\d\.]+\s*[GMK]?Hz\s*-\s*([\d\.]+\s*[GMK]?Hz)', output, re.IGNORECASE)
        cur_m = re.search(r'current CPU frequency:\s*([\d\.]+\s*[GMK]?Hz)', output, re.IGNORECASE)
        if max_m and cur_m:
            return max_m.group(1).strip() == cur_m.group(1).strip()
        return False

    def _check_psutil(self) -> bool:
        try:
            import psutil
            cpu_freq = psutil.cpu_freq()
            if cpu_freq:
                return cpu_freq.current == cpu_freq.max
        except ImportError:
            pass
        return False

    def _check_lshw(self) -> bool:
        try:
            output = subprocess.check_output(
                shlex.split("lshw -c cpu"),
                stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            return False
        sizes = []
        capacities = []
        for line in output.splitlines():
            m = re.search(r'size:\s*(.+)', line, re.IGNORECASE)
            if m:
                sizes.append(m.group(1).strip())
            m = re.search(r'capacity:\s*(.+)', line, re.IGNORECASE)
            if m:
                capacities.append(m.group(1).strip())
        return bool(sizes and capacities and sizes == capacities)

    def _collect_kernel_info(self) -> dict:
        info = dict(platform.uname()._asdict())
        try:
            with open('/sys/kernel/mm/transparent_hugepage/enabled', 'r') as f:
                content = f.read()
            m = re.search(r'\[(\w+)\]', content)
            info['transparent_hugepage'] = m.group(1) if m else content.strip()
        except Exception:
            pass
        return info

    def _collect_memory_info(self) -> dict:
        mem_info = {}
        try:
            mem_info['page_size'] = os.sysconf("SC_PAGESIZE")
        except Exception:
            pass
        try:
            with open('/proc/sys/vm/overcommit_memory', 'r') as f:
                mem_info['overcommit_memory'] = f.read().strip()
        except Exception:
            pass
        return mem_info

    def collect(self) -> CollectorResult:
        sub_collectors = [
            ("lscpu", self._collect_lscpu),
            ("vm", self._collect_virtual_machine),
            ("cpu_hp", self._collect_cpu_high_performance),
            ("kernel", self._collect_kernel_info),
            ("memory", self._collect_memory_info),
        ]

        result = {}
        errors = []

        max_workers = min(len(sub_collectors), os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers) as executor:
            futures = {
                executor.submit(fn): name
                for name, fn in sub_collectors
            }
            for future in as_completed(futures):
                name = futures[future]
                try:
                    data = future.result()
                    result.update(data)
                except Exception as e:
                    errors.append(f"采集 {name} 失败: {e}")

        return CollectorResult(result, self.collect_type, errors)


class AscendCollector:
    """Ascend 组件版本采集器"""

    COMPONENTS = [
        ("driver", "", "", "/usr/local/Ascend/driver/version.info", ("version",), "", ""),
        ("toolkit", "ASCEND_TOOLKIT_HOME", "/usr/local/Ascend/ascend-toolkit/latest/",
         "toolkit/version.info", ("version", "version_dir"), "timestamp", ""),
        ("opp_kernel", "ASCEND_TOOLKIT_HOME", "/usr/local/Ascend/ascend-toolkit/latest/",
         "opp_kernel/version.info", ("version", "version_dir"), "timestamp", ""),
        ("mindstudio_toolkit", "ASCEND_TOOLKIT_HOME", "/usr/local/Ascend/ascend-toolkit/latest/",
         "mindstudio-toolkit/version.info", ("version",), "", ""),
        ("atb", "ATB_HOME_PATH",
         "/usr/local/Ascend/nnal/atb/latest/atb/cxx_abi_0",
         "../../version.info", ("ascend-cann-atb version",), "", "commit id"),
        ("mindie", "MINDIE_LLM_HOME_PATH",
         "/usr/local/Ascend/mindie/latest/mindie-llm",
         "../version.info", ("ascend-mindie",), "timestamp", ""),
        ("atb-models", "ATB_SPEED_HOME_PATH",
         "/usr/local/Ascend/atb-models",
         "version.info", ("atb-models version",), "time", "commit id"),
    ]

    def __init__(self):
        self.collect_type = "ascend"

    @staticmethod
    def _get_version_file(env_var, default_home, version_file):
        home = os.getenv(env_var) if env_var else ''
        base_path = home or default_home
        if version_file.startswith('/'):
            return os.path.normpath(version_file)
        return os.path.normpath(os.path.join(base_path, version_file))

    @staticmethod
    def _parse_version_file(file_path, ver_keys, ts_key, commit_key):
        result = {}
        if not os.path.isfile(file_path):
            return result
        try:
            with open(file_path, 'r') as f:
                for line in f:
                    line = line.strip()
                    if not line:
                        continue
                    parts = line.split('=', 1) if '=' in line else line.split(':', 1)
                    if len(parts) != 2:
                        continue
                    key, value = parts[0].strip().lower(), parts[1].strip()
                    for ver_key in ver_keys:
                        if ver_key == key:
                            if 'version' not in result:
                                result['version'] = value
                            else:
                                result['version'] += f' ({value})'
                    if ts_key and ts_key == key:
                        result['timestamp'] = value
                    if commit_key and commit_key == key:
                        result['commit'] = value
        except Exception:
            pass
        return result

    def collect(self) -> CollectorResult:
        results = {}
        for name, env_var, default_home, version_file, ver_keys, ts_key, commit_key in self.COMPONENTS:
            file_path = self._get_version_file(env_var, default_home, version_file)
            results[name] = self._parse_version_file(file_path, ver_keys, ts_key, commit_key)
        return CollectorResult(results, self.collect_type)


class EnvCollector:
    """环境变量采集器"""

    ENV_FILTERS = [
        "ASCEND", "MINDIE", "ATB_", "HCCL_", "MIES",
        "RANKTABLE", "GE_", "TORCH", "ACL_", "NPU_",
        "LCCL_", "LCAL_", "OPS", "INF_"
    ]

    def __init__(self, filter_env: bool = False):
        self.collect_type = "env"
        self.filter_env = filter_env

    def collect(self) -> CollectorResult:
        env_items = dict(os.environ)
        if self.filter_env:
            env_items = {
                k: v for k, v in env_items.items()
                if any(f in k for f in self.ENV_FILTERS)
            }
        return CollectorResult(env_items, self.collect_type)


class ConfigCollector:
    """JSON 配置文件采集器"""

    def __init__(self, config_path: str, collect_type: str = "config"):
        self.config_path = config_path
        self.collect_type = collect_type

    def collect(self) -> CollectorResult:
        errors = []
        try:
            with open(self.config_path, 'r', encoding='utf-8') as f:
                data = json.load(f)
            return CollectorResult(data, self.collect_type)
        except FileNotFoundError:
            errors.append(f"配置文件不存在: {self.config_path!r}")
        except json.JSONDecodeError as e:
            errors.append(f"配置文件 JSON 解析失败: {self.config_path!r}: {e}")
        except Exception as e:
            errors.append(f"读取配置文件失败: {self.config_path!r}: {e}")
        return CollectorResult({}, self.collect_type, errors)


class PingCollector:
    """网络连通性采集器 — ping rank table 中的所有主机"""

    def __init__(self, rank_table: RankTable):
        self.collect_type = "ping"
        self.rank_table = rank_table

    def collect(self) -> CollectorResult:
        result = {}
        errors = []

        if not which("/usr/bin/ping"):
            errors.append("当前环境没有 'ping' 命令")
            return CollectorResult(result, self.collect_type, errors)

        host_to_devices = self.rank_table.host_to_devices
        if not host_to_devices:
            errors.append("rank table 没有解析出任何主机信息")
            return CollectorResult(result, self.collect_type, errors)

        for host in host_to_devices:
            try:
                output = subprocess.check_output(
                    shlex.split(f"/usr/bin/ping -c 3 -q -W 2 {host}"),
                    stderr=subprocess.STDOUT, text=True, timeout=10
                )
            except Exception:
                output = "ping failed"
            result[str(host)] = output

        return CollectorResult(result, self.collect_type, errors)


class HCCNCollector:
    """hccn_tool 命令采集器基类 — 采集 Link/VNIC/TLS 状态"""

    def __init__(self, cmd_name: str, collect_type: str):
        self.cmd_name = cmd_name
        self.collect_type = collect_type
        self.npu_count = get_npu_count()

    def _run_cmd(self, device_id: int) -> str:
        cmd = f"{HCCN_TOOL_CMD} -i {device_id} -{self.cmd_name} -g"
        try:
            return subprocess.check_output(
                shlex.split(cmd), stderr=subprocess.DEVNULL, text=True
            )
        except Exception:
            return "command failed"

    def collect(self) -> CollectorResult:
        errors = []
        if not which(HCCN_TOOL_CMD):
            place = "容器" if is_in_container() else "宿主机"
            errors.append(f"{place}上没有找到 'hccn_tool' 命令")
            return CollectorResult([], self.collect_type, errors)

        max_workers = min(self.npu_count, os.cpu_count() or 1)
        with ThreadPoolExecutor(max_workers) as executor:
            futures = [executor.submit(self._run_cmd, i) for i in range(self.npu_count)]
            results = [f.result() for f in futures]

        return CollectorResult(results, self.collect_type)


class HCCLCollector:
    """HCCL 通信连通性采集器 — NPU 间 HCCS ping"""

    def __init__(self, rank_table: RankTable):
        self.collect_type = "hccl"
        self.rank_table = rank_table
        self.npu_count = get_npu_count()
        self.option = (
            "-hccs_ping" if rank_table.version >= SimpleVersion("1.2") else "-ping"
        )

    def _run_cmd(self, device_id, device_ip):
        if device_ip.version == 4:
            cmd = f"{HCCN_TOOL_CMD} -i {device_id} {self.option} -g address {device_ip}"
        elif device_ip.version == 6:
            cmd = f"{HCCN_TOOL_CMD} -i {device_id} {self.option} -inet6 -g ipv6_address {device_ip}"
        else:
            return None, None

        try:
            proc = subprocess.Popen(
                shlex.split(cmd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True
            )
            ret = proc.wait()
            output = proc.stdout.read()
        except Exception:
            ret, output = -1, "command failed"
        return cmd, (ret, output)

    def collect(self) -> CollectorResult:
        errors = []
        if not which(HCCN_TOOL_CMD):
            place = "容器" if is_in_container() else "宿主机"
            errors.append(f"{place}上没有找到 'hccn_tool' 命令")
            return CollectorResult({}, self.collect_type, errors)

        all_device_ips = [
            dev_info.device_ip
            for dev_list in self.rank_table.host_to_devices.values()
            for dev_info in dev_list
        ]

        max_workers = min(self.npu_count, os.cpu_count() or 1)
        results = {}

        with ThreadPoolExecutor(max_workers) as executor:
            futures = [
                executor.submit(self._run_per_device, device_id, all_device_ips)
                for device_id in range(self.npu_count)
            ]
            for future in as_completed(futures):
                results.update(future.result())

        return CollectorResult(results, self.collect_type)

    def _run_per_device(self, device_id, device_ips):
        result = {}
        for device_ip in device_ips:
            cmd, ret_output = self._run_cmd(device_id, device_ip)
            if cmd:
                result[cmd] = list(ret_output) if ret_output else [None, None]
        return result


class WeightCollector:
    """权重文件 SHA256 哈希采集器"""

    TENSOR_SUFFIX = '.safetensors'
    TENSOR_ID_PATTERN = re.compile(r'(\d{5})-of-\d{5}' + re.escape(TENSOR_SUFFIX))

    def __init__(self, weight_dir: str, chunk_size: int = 32 * 1024 * 1024):
        self.collect_type = "weight"
        self.weight_dir = weight_dir
        self.chunk_size = chunk_size

    @staticmethod
    def _calculate_sha256(filepath, chunk_size):
        sha256 = hashlib.sha256()
        with open(filepath, 'rb') as f:
            while True:
                data = f.read(chunk_size)
                if not data:
                    break
                sha256.update(data)
        return sha256.hexdigest()

    def _get_tensor_files(self):
        tensor_files = []
        for root, dirs, files in os.walk(self.weight_dir):
            for filename in files:
                filepath = os.path.join(root, filename)
                if os.path.isfile(filepath) and filename.endswith(self.TENSOR_SUFFIX):
                    if not os.path.islink(filepath):
                        tensor_files.append(filepath)
        return tensor_files

    def collect(self) -> CollectorResult:
        errors = []
        if not self.weight_dir or not os.path.isdir(self.weight_dir):
            errors.append(f"权重目录不存在: {self.weight_dir!r}")
            return CollectorResult({}, self.collect_type, errors)

        tensor_files = self._get_tensor_files()
        if not tensor_files:
            errors.append(f"权重目录下没有找到 {self.TENSOR_SUFFIX!r} 文件")
            return CollectorResult({}, self.collect_type, errors)

        max_workers = min(len(tensor_files), os.cpu_count() or 1)
        results = {}

        with ProcessPoolExecutor(max_workers=max_workers) as executor:
            futures = {
                executor.submit(self._calculate_sha256, tf, self.chunk_size): tf
                for tf in tensor_files
            }
            for future in as_completed(futures):
                tf = futures[future]
                basename = os.path.basename(tf)
                m = self.TENSOR_ID_PATTERN.search(basename)
                tensor_id = m.group(1) if m else basename
                try:
                    results[tensor_id] = future.result()
                except Exception as e:
                    results[tensor_id] = f"error: {e}"

        return CollectorResult(results, self.collect_type)


# ============================================================================
# 主采集逻辑
# ============================================================================

def collect_all(args) -> dict:
    """执行所有采集器，返回完整的 dump 字典"""
    dump_content = {}
    warnings = []

    def _run_collector(collector):
        """运行单个采集器并处理结果"""
        try:
            result = collector.collect()
        except Exception as e:
            warn("采集 {} 时发生异常: {}", collector.collect_type, e)
            return

        if result.has_errors:
            for err in result.errors:
                warn("[{}] {}", result.collect_type, err)
            if not result.data:
                return

        dump_content[result.collect_type] = result.data

    # --- 始终采集的核心信息 ---
    info("{Color.BOLD}=== 开始采集环境信息 ==={Color.RESET}".format(Color=Color))

    info("采集系统信息...")
    _run_collector(SysCollector())

    info("采集 Ascend 组件版本...")
    _run_collector(AscendCollector())

    info("采集环境变量...")
    _run_collector(EnvCollector(filter_env=args.filter))

    # --- 可选：配置文件 ---
    if args.mies_config_path:
        info("采集 MindIE 服务配置: {}", args.mies_config_path)
        _run_collector(ConfigCollector(args.mies_config_path, "mies config"))

    if args.user_config_path:
        info("采集 user_config: {}", args.user_config_path)
        _run_collector(ConfigCollector(args.user_config_path, "user config"))

    if args.mindie_env_path:
        info("采集 mindie_env: {}", args.mindie_env_path)
        _run_collector(ConfigCollector(args.mindie_env_path, "mindie env"))

    # --- 可选：权重目录 ---
    if args.weight_dir:
        info("采集模型权重配置和哈希...")
        model_config_path = os.path.join(args.weight_dir, "config.json")
        _run_collector(ConfigCollector(model_config_path, "model config"))

        chunk_size = args.chunk_size * 1024 * 1024
        _run_collector(WeightCollector(args.weight_dir, chunk_size))

    # --- 可选：网络与 HCCL（需要 rank table） ---
    if args.rank_table_path:
        framework = Framework.MINDIE
        if args.scene:
            if "vllm" in args.scene:
                framework = Framework.VLLM

        try:
            rank_table = parse_rank_table(args.rank_table_path, framework)
            info("rank table 解析成功: {} 台主机, {} 版本",
                 len(rank_table.host_to_devices), rank_table.version)
        except Exception as e:
            error("rank table 解析失败: {}", e)
            rank_table = None

        if rank_table:
            info("采集 Ping 连通性...")
            _run_collector(PingCollector(rank_table))

            info("采集 HCCL 通信状态...")
            _run_collector(HCCLCollector(rank_table))

            info("采集 Link 状态...")
            _run_collector(HCCNCollector("link", "link"))

            info("采集 VNIC 状态...")
            _run_collector(HCCNCollector("vnic", "vnic"))

            info("采集 TLS 状态...")
            _run_collector(HCCNCollector("tls", "tls"))

    return dump_content


# ============================================================================
# 命令行入口
# ============================================================================

def main():
    parser = argparse.ArgumentParser(
        description="独立环境信息采集脚本 — 从 msprechecker 提取的 dump 功能",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""\
示例:
  %(prog)s                                              # 采集默认信息
  %(prog)s -o /tmp/snapshot.json                        # 指定输出路径
  %(prog)s --filter                                     # 仅采集昇腾相关环境变量
  %(prog)s --mies-config-path /path/to/config.json      # 额外采集 MindIE 配置
  %(prog)s --rank-table-path rank_table.json --scene mindie  # 采集网络与 HCCL
  %(prog)s --weight-dir /path/to/weights --chunk-size 64     # 采集权重哈希
""",
    )

    parser.add_argument(
        "-o", "--output-path",
        default="./msprechecker_dumped.json",
        help="输出文件路径 (JSON 格式)。默认: ./msprechecker_dumped.json"
    )
    parser.add_argument(
        "--filter",
        action="store_true",
        help="仅采集昇腾相关的环境变量 (ASCEND/MINDIE/ATB_/HCCL_ 等)"
    )
    parser.add_argument("--mies-config-path", help="MindIE 服务 config.json 路径")
    parser.add_argument("--user-config-path", help="user_config.json 路径")
    parser.add_argument("--mindie-env-path", help="mindie_env.json 路径")
    parser.add_argument("--rank-table-path", help="rank table 文件路径")
    parser.add_argument(
        "--scene",
        help="部署场景，如 'mindie' 或 'vllm'。用于确定 rank table 的解析格式"
    )
    parser.add_argument("--weight-dir", help="模型权重目录路径")
    parser.add_argument(
        "--chunk-size",
        choices=[32, 64, 128, 256], type=int, default=32,
        help="计算权重 SHA256 时的块大小 (MB)。默认: 32"
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")

    args = parser.parse_args()

    start_time = time.time()

    dump_content = collect_all(args)

    # 添加元数据
    dump_content["_meta"] = {
        "tool": "msprechecker_dump.py",
        "version": __version__,
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime()),
        "hostname": platform.node(),
        "python_version": platform.python_version(),
        "collect_duration_seconds": round(time.time() - start_time, 2),
    }

    # 保存
    output_dir = os.path.dirname(os.path.abspath(args.output_path))
    if not os.path.isdir(output_dir):
        os.makedirs(output_dir, exist_ok=True)

    with open(args.output_path, 'w', encoding='utf-8') as f:
        json.dump(dump_content, f, indent=4, ensure_ascii=False)

    duration = round(time.time() - start_time, 2)
    info("{Color.BOLD}=== 采集完成 ==={Color.RESET}".format(Color=Color))
    info("采集项目: {}", len(dump_content) - 1)  # 减去 _meta
    info("耗时: {} 秒", duration)
    info("已保存至: {}", args.output_path)
    info("提示: 可使用 'msprechecker compare' 比较多个 dump 文件的差异")


if __name__ == "__main__":
    main()
