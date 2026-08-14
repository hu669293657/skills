#!/usr/bin/env python3
"""
Host Trace 规则引擎
=====================
功能：加载 YAML 规则 → 匹配指标 → 输出诊断结果
输入：host_metrics.json（由 feature_extractor.py 生成）
输出：matched_rules.json（匹配的规则列表）

使用方法：
    python rule_engine.py <metrics.json>
        [--rules-dir ./rules]
        [--output <output.json>]
        [--workload training]

注意：需要安装 PyYAML: pip install pyyaml
"""

import sys
import os
import json
import argparse
from typing import Dict, List, Any, Optional

try:
    import yaml
except ImportError:
    print("ERROR: PyYAML not installed. Run: pip install pyyaml", file=sys.stderr)
    sys.exit(1)


# ============================================================
# 条件评估器
# ============================================================

class ConditionEvaluator:
    """评估规则条件"""

    OPERATORS = {
        '>': lambda a, b: a > b,
        '>=': lambda a, b: a >= b,
        '<': lambda a, b: a < b,
        '<=': lambda a, b: a <= b,
        '==': lambda a, b: a == b,
        '!=': lambda a, b: a != b,
        'in': lambda a, b: a in b,
        'not_in': lambda a, b: a not in b,
    }

    @staticmethod
    def evaluate(condition: Dict, metrics: Dict, metadata: Dict) -> bool:
        """评估条件表达式"""
        if condition is None:
            return True

        if 'and' in condition:
            return all(
                ConditionEvaluator.evaluate(c, metrics, metadata)
                for c in condition['and']
            )

        if 'or' in condition:
            return any(
                ConditionEvaluator.evaluate(c, metrics, metadata)
                for c in condition['or']
            )

        if 'not' in condition:
            return not ConditionEvaluator.evaluate(
                condition['not'], metrics, metadata
            )

        # 简单条件: {metric: xxx, op: >, value: 90}
        metric_name = condition.get('metric')
        op = condition.get('op', '>')
        value = condition.get('value')
        ref = condition.get('ref')

        if metric_name is None:
            return False

        # 获取指标值
        metric_value = ConditionEvaluator._get_metric_value(
            metric_name, metrics, metadata
        )

        if metric_value is None:
            return False

        # 获取比较值
        if ref is not None:
            compare_value = ConditionEvaluator._get_metric_value(ref, metrics, metadata)
        else:
            compare_value = value

        if compare_value is None:
            return False

        # 执行比较
        op_func = ConditionEvaluator.OPERATORS.get(op)
        if op_func is None:
            return False

        try:
            return op_func(metric_value, compare_value)
        except TypeError:
            return False

    @staticmethod
    def _get_metric_value(name: str, metrics: Dict, metadata: Dict) -> Any:
        """从 metrics 中获取指标值，支持嵌套路径"""
        # 先在 metrics 顶层查找
        if name in metrics:
            return metrics[name]

        # 在 metadata 中查找
        if name in metadata:
            return metadata[name]

        # 在各分类中查找
        for category, cat_metrics in metrics.items():
            if isinstance(cat_metrics, dict) and name in cat_metrics:
                value = cat_metrics[name]
                if value is not None:
                    return value

        # 在 metadata 中递归查找
        if isinstance(metadata, dict):
            for key, val in metadata.items():
                if key == name:
                    return val
                if isinstance(val, dict) and name in val:
                    return val[name]

        return None


# ============================================================
# 规则引擎
# ============================================================

class RuleEngine:
    """规则匹配引擎"""

    SEVERITY_ORDER = {
        'CRITICAL': 0,
        'HIGH': 1,
        'MEDIUM': 2,
        'LOW': 3,
        'INFO': 4,
    }

    def __init__(self, rules_dir: str):
        self.rules_dir = rules_dir
        self.rules = []
        self.rule_version = 'unknown'

    def load_rules(self):
        """加载所有 YAML 规则文件"""
        if not os.path.isdir(self.rules_dir):
            print(f"WARNING: Rules directory not found: {self.rules_dir}", file=sys.stderr)
            return

        yaml_files = sorted([
            f for f in os.listdir(self.rules_dir)
            if f.endswith('.yaml') or f.endswith('.yml')
        ])

        for yaml_file in yaml_files:
            file_path = os.path.join(self.rules_dir, yaml_file)
            try:
                with open(file_path, 'r', encoding='utf-8') as f:
                    data = yaml.safe_load(f)

                if data is None:
                    continue

                # 检查文件版本
                if 'version' in data:
                    self.rule_version = data['version']

                # 加载规则
                rules = data.get('rules', [])
                if not rules and 'rule_id' in data:
                    # 单规则文件
                    rules = [data]

                for rule in rules:
                    if 'rule_id' in rule:
                        self.rules.append(rule)

                print(f"  Loaded {len(rules)} rules from {yaml_file}", file=sys.stderr)

            except Exception as e:
                print(f"  ERROR loading {yaml_file}: {e}", file=sys.stderr)

        print(f"Total rules loaded: {len(self.rules)}", file=sys.stderr)

    def evaluate(self, metrics: Dict, workload: str = 'unknown') -> Dict:
        """评估所有规则"""
        metadata = metrics.get('metadata', {})

        matched = []
        unmatched_close = []

        for rule in self.rules:
            # 检查 workload context
            if not self._check_workload_context(rule, workload):
                continue

            condition = rule.get('condition')
            if condition is None:
                continue

            is_matched = ConditionEvaluator.evaluate(condition, metrics, metadata)

            if is_matched:
                # 收集证据
                evidence = self._collect_evidence(rule, metrics, metadata)
                matched.append({
                    'rule_id': rule.get('rule_id'),
                    'category': rule.get('category'),
                    'severity': rule.get('severity', 'INFO'),
                    'diagnosis': rule.get('diagnosis', ''),
                    'evidence': evidence,
                    'suggestions': rule.get('suggestions', []),
                    'correlation_hint': rule.get('correlation_hint'),
                })
            else:
                # 检查是否接近阈值
                margin = self._calc_margin(rule, metrics, metadata)
                if margin is not None and margin < 0.1:  # 接近阈值 10% 以内
                    unmatched_close.append({
                        'rule_id': rule.get('rule_id'),
                        'severity': rule.get('severity', 'INFO'),
                        'diagnosis': rule.get('diagnosis', ''),
                        'margin': round(margin, 4),
                    })

        # 按严重程度排序
        matched.sort(
            key=lambda r: self.SEVERITY_ORDER.get(r['severity'], 99)
        )
        unmatched_close.sort(
            key=lambda r: r.get('margin', 1)
        )

        return {
            'matched_rules': matched,
            'unmatched_but_close': unmatched_close,
            'total_rules_evaluated': len(self.rules),
            'total_matched': len(matched),
            'rule_version': self.rule_version,
        }

    def _check_workload_context(self, rule: Dict, workload: str) -> bool:
        """检查规则是否适用于当前工作负载"""
        ctx = rule.get('workload_context')
        if ctx is None:
            return True

        match = ctx.get('match')
        exclude = ctx.get('exclude', [])

        if exclude and workload in exclude:
            return False

        if match and match != 'all' and workload != 'unknown' and workload != match:
            return False

        return True

    def _collect_evidence(self, rule: Dict, metrics: Dict, metadata: Dict) -> Dict:
        """收集规则匹配的证据"""
        evidence = {}
        required = rule.get('evidence_required', [])

        for metric_name in required:
            value = ConditionEvaluator._get_metric_value(
                metric_name, metrics, metadata
            )
            if value is not None:
                evidence[metric_name] = value

        # 添加相关元数据
        if 'cpu_count' in metadata:
            evidence['cpu_count'] = metadata['cpu_count']

        return evidence

    def _calc_margin(self, rule: Dict, metrics: Dict, metadata: Dict) -> Optional[float]:
        """计算规则条件的接近程度 (0=刚好触发, 1=远离阈值)"""
        condition = rule.get('condition')
        if condition is None:
            return None

        # 找到最接近阈值的条件
        return self._calc_margin_recursive(condition, metrics, metadata)

    def _calc_margin_recursive(self, condition: Dict, metrics: Dict, metadata: Dict) -> Optional[float]:
        if 'and' in condition:
            margins = [
                self._calc_margin_recursive(c, metrics, metadata)
                for c in condition['and']
            ]
            margins = [m for m in margins if m is not None]
            return min(margins) if margins else None

        if 'or' in condition:
            margins = [
                self._calc_margin_recursive(c, metrics, metadata)
                for c in condition['or']
            ]
            margins = [m for m in margins if m is not None]
            return min(margins) if margins else None

        # 简单条件
        metric_name = condition.get('metric')
        op = condition.get('op', '>')
        value = condition.get('value')
        ref = condition.get('ref')

        if metric_name is None:
            return None

        metric_value = ConditionEvaluator._get_metric_value(
            metric_name, metrics, metadata
        )
        if metric_value is None:
            return None

        if ref is not None:
            compare_value = ConditionEvaluator._get_metric_value(ref, metrics, metadata)
        else:
            compare_value = value

        if compare_value is None or compare_value == 0:
            return None

        try:
            # 计算相对距离
            if op in ('>', '>='):
                margin = (compare_value - metric_value) / compare_value
            elif op in ('<', '<='):
                margin = (metric_value - compare_value) / compare_value
            else:
                return None
            return max(margin, 0)
        except (TypeError, ZeroDivisionError):
            return None


# ============================================================
# Host-NPU 关联分析
# ============================================================

class HostNpuCorrelator:
    """Host-NPU 关联分析器"""

    CAUSAL_PATTERNS = {
        'CPU_SCHED_CONTENTION': {
            'confidence': 0.85,
            'conditions': ['runqueue_high', 'sched_delay'],
        },
        'IO_BLOCK': {
            'confidence': 0.78,
            'conditions': ['io_pending', 'thread_d_state'],
        },
        'MEMORY_PRESSURE': {
            'confidence': 0.72,
            'conditions': ['major_page_fault'],
        },
        'KERNEL_LAUNCH_GAP': {
            'confidence': 0.60,
            'conditions': ['no_other_anomaly'],
        },
    }

    @staticmethod
    def analyze(metrics: Dict, key_events: List[Dict] = None) -> Dict:
        """分析 Host-NPU 关联"""
        host_npu = metrics.get('host_npu', {})
        npu_idle_ratio = host_npu.get('npu_idle_ratio')
        npu_idle_max_gap = host_npu.get('npu_idle_max_gap_ms', 0)
        npu_idle_gap_count = host_npu.get('npu_idle_gap_count', 0)

        if npu_idle_ratio is None or npu_idle_ratio < 5:
            return {
                'correlation_found': False,
                'reason': 'no_significant_npu_idle',
                'npu_idle_ratio': npu_idle_ratio,
            }

        # 分析 NPU idle 期间的 host 事件
        cpu_metrics = metrics.get('cpu', {})
        sched_metrics = metrics.get('sched', {})
        io_metrics = metrics.get('io', {})
        memory_metrics = metrics.get('memory', {})
        metadata = metrics.get('metadata', {})

        # 检测因果模式
        patterns_found = []

        # 模式 A: CPU 调度竞争
        runqueue_pressure = sched_metrics.get('runqueue_pressure', 0)
        sched_latency = sched_metrics.get('sched_latency_p99_ms', 0)
        if runqueue_pressure > 1.0 and sched_latency and sched_latency > 5:
            patterns_found.append({
                'pattern': 'CPU_SCHED_CONTENTION',
                'confidence': HostNpuCorrelator.CAUSAL_PATTERNS['CPU_SCHED_CONTENTION']['confidence'],
                'evidence': {
                    'runqueue_pressure': runqueue_pressure,
                    'sched_latency_p99_ms': sched_latency,
                    'npu_idle_ratio': npu_idle_ratio,
                },
            })

        # 模式 B: IO 阻塞
        io_wait = io_metrics.get('io_wait_pct', 0)
        if io_wait and io_wait > 5:
            patterns_found.append({
                'pattern': 'IO_BLOCK',
                'confidence': HostNpuCorrelator.CAUSAL_PATTERNS['IO_BLOCK']['confidence'],
                'evidence': {
                    'io_wait_pct': io_wait,
                    'npu_idle_ratio': npu_idle_ratio,
                },
            })

        # 模式 C: 内存压力
        major_pf = memory_metrics.get('major_page_faults', 0)
        if major_pf and major_pf > 0:
            patterns_found.append({
                'pattern': 'MEMORY_PRESSURE',
                'confidence': HostNpuCorrelator.CAUSAL_PATTERNS['MEMORY_PRESSURE']['confidence'],
                'evidence': {
                    'major_page_faults': major_pf,
                    'npu_idle_ratio': npu_idle_ratio,
                },
            })

        # 模式 D: Kernel launch gap（无其他异常时）
        kernel_launch_gap = host_npu.get('kernel_launch_gap_avg_ms')
        if not patterns_found and kernel_launch_gap and kernel_launch_gap > 1:
            patterns_found.append({
                'pattern': 'KERNEL_LAUNCH_GAP',
                'confidence': HostNpuCorrelator.CAUSAL_PATTERNS['KERNEL_LAUNCH_GAP']['confidence'],
                'evidence': {
                    'kernel_launch_gap_avg_ms': kernel_launch_gap,
                    'npu_idle_ratio': npu_idle_ratio,
                },
            })

        # 确定主要瓶颈
        if patterns_found:
            primary = max(patterns_found, key=lambda p: p['confidence'])
            causal_chain = HostNpuCorrelator._build_causal_chain(
                primary['pattern'], metrics
            )
        else:
            primary = None
            causal_chain = None

        return {
            'correlation_found': len(patterns_found) > 0,
            'npu_idle_ratio': npu_idle_ratio,
            'npu_idle_max_gap_ms': npu_idle_max_gap,
            'npu_idle_gap_count': npu_idle_gap_count,
            'patterns_found': patterns_found,
            'primary_bottleneck': primary['pattern'] if primary else None,
            'causal_chain': causal_chain,
        }

    @staticmethod
    def _build_causal_chain(pattern: str, metrics: Dict) -> str:
        """构建因果链描述"""
        chains = {
            'CPU_SCHED_CONTENTION': (
                "CPU runqueue 压力高 "
                "→ runtime thread 调度延迟 "
                "→ kernel launch 延迟 "
                "→ NPU idle "
                "→ step time 增加"
            ),
            'IO_BLOCK': (
                "磁盘 IO 等待 "
                "→ DataLoader 线程进入 D 状态 "
                "→ 数据准备阻塞 "
                "→ NPU idle "
                "→ step time 增加"
            ),
            'MEMORY_PRESSURE': (
                "major page fault "
                "→ 内存不足导致磁盘换入 "
                "→ 数据准备阻塞 "
                "→ NPU idle "
                "→ step time 增加"
            ),
            'KERNEL_LAUNCH_GAP': (
                "runtime 调用阻塞 "
                "→ kernel 下发间隔大 "
                "→ NPU 计算完成后空闲等待 "
                "→ step time 增加"
            ),
        }
        return chains.get(pattern, '')


# ============================================================
# 主函数
# ============================================================

def main():
    parser = argparse.ArgumentParser(description='Host Trace 规则引擎')
    parser.add_argument('input', help='输入 metrics JSON 文件路径')
    parser.add_argument('--rules-dir', '-r', default=None, help='规则目录路径')
    parser.add_argument('--output', '-o', default=None, help='输出文件路径')
    parser.add_argument('--workload', '-w', default='unknown', help='工作负载类型')

    args = parser.parse_args()

    if not os.path.exists(args.input):
        print(f"ERROR: File not found: {args.input}", file=sys.stderr)
        sys.exit(1)

    # 设置规则目录
    if args.rules_dir is None:
        script_dir = os.path.dirname(os.path.abspath(__file__))
        args.rules_dir = os.path.join(os.path.dirname(script_dir), 'rules')

    if args.output is None:
        base = os.path.splitext(args.input)[0]
        args.output = f'{base}_diagnosis.json'

    # 加载 metrics
    print(f"[1/3] 加载指标: {args.input}", file=sys.stderr)
    with open(args.input, 'r', encoding='utf-8') as f:
        metrics = json.load(f)

    # 加载规则
    print(f"\n[2/3] 加载规则: {args.rules_dir}", file=sys.stderr)
    engine = RuleEngine(args.rules_dir)
    engine.load_rules()

    # 评估规则
    print(f"\n[3/3] 评估规则 (workload: {args.workload})", file=sys.stderr)
    diagnosis = engine.evaluate(metrics, workload=args.workload)

    # Host-NPU 关联分析
    correlator_result = HostNpuCorrelator.analyze(metrics)
    diagnosis['host_npu_correlation'] = correlator_result

    # 写入输出
    with open(args.output, 'w', encoding='utf-8') as f:
        json.dump(diagnosis, f, indent=2, ensure_ascii=False)

    # 打印摘要
    print(f"\n=== 诊断结果 ===", file=sys.stderr)
    print(f"匹配规则: {diagnosis['total_matched']}/{diagnosis['total_rules_evaluated']}", file=sys.stderr)

    for rule in diagnosis['matched_rules']:
        print(f"  [{rule['severity']}] {rule['rule_id']}: {rule['diagnosis']}", file=sys.stderr)

    if diagnosis['unmatched_but_close']:
        print(f"\n接近阈值:", file=sys.stderr)
        for rule in diagnosis['unmatched_but_close']:
            print(f"  [{rule['severity']}] {rule['rule_id']}: margin={rule['margin']}", file=sys.stderr)

    corr = diagnosis.get('host_npu_correlation', {})
    if corr.get('correlation_found'):
        print(f"\nHost-NPU 关联:", file=sys.stderr)
        print(f"  主要瓶颈: {corr.get('primary_bottleneck')}", file=sys.stderr)
        print(f"  因果链: {corr.get('causal_chain', '')}", file=sys.stderr)

    print(f"\n输出: {args.output}", file=sys.stderr)


if __name__ == '__main__':
    main()
