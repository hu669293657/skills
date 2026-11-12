#!/usr/bin/env python3
"""根据有来源的配套矩阵检查已采集的版本。"""
import argparse
import json
from pathlib import Path
import re
import sys


SCENES = {'torch': 'torch_npu', 'vllm': 'vllm_ascend', 'mindspeed': 'mindspeed_llm',
          'modelink': 'modellink', 'mindie': 'mindie'}
PACKAGES = set(SCENES.values()) | {'torch', 'vllm', 'mindspeed', 'megatron_core',
                                  'triton_ascend', 'fsdpturbo'}
VERSION = re.compile(r'v?(\d+(?:\.\d+)+(?:[a-z0-9._+-]*))', re.I)
PYTHON_RELEASE = re.compile(r'\d+\.\d+(?:\.\d+)?')


def version(value, component):
    if not isinstance(value, str):
        return None
    # Toolkit 采集器将 version_dir 附在主版本之后。
    value = value.strip().split(' (', 1)[0] if component == 'cann' else value.strip()
    match = VERSION.fullmatch(value)
    if not match:
        return None
    normalized = match[1].lower()
    if component == 'torch' and normalized.endswith('+cpu'):
        normalized = normalized[:-4]
    if component in ('torch', 'torch_npu', 'vllm', 'vllm_ascend', 'triton_ascend'):
        normalized = re.sub(r'(?<=\d)[._-]?rc[._-]?(?=\d)', 'rc', normalized)
    return normalized


def read_object(path):
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    if not isinstance(data, dict):
        raise ValueError(f'{path.name} 顶层须为 JSON 对象')
    return data


def observation(component, samples):
    evidence = [{'version': value, 'source': source} for value, source in samples
                if version(value, component)]
    distinct = {version(item['version'], component) for item in evidence}
    return {'version': next(iter(distinct)) if len(distinct) == 1 else None,
            'evidence': evidence, 'conflict': len(distinct) > 1}


def collect_versions(data, overrides):
    runtime = data.get('runtime') or {}
    ascend = data.get('ascend') or {}
    packages = runtime.get('packages') or {}
    samples = {component: [] for component in PACKAGES | {'python', 'cann', 'hdk'}}
    for field in ('watched', 'watched_locations'):
        for name, value in (packages.get(field) or {}).items():
            component = name.lower().replace('-', '_')
            if component not in PACKAGES:
                continue
            for record in value if isinstance(value, list) else [value]:
                raw = record.get('version') if isinstance(record, dict) else record
                samples[component].append((raw, f'runtime.packages.{field}.{name}'))
    for line in packages.get('pip_list_raw') or []:
        name, sep, raw = line.partition('==')
        component = name.lower().replace('-', '_')
        if sep and component in PACKAGES:
            samples[component].append((raw, f'runtime.packages.pip_list_raw[{name}]'))
    for component, key in (('cann', 'toolkit'), ('mindie', 'mindie'), ('hdk', 'hdk')):
        record = ascend.get(key) or {}
        if isinstance(record, dict) and record.get('collection_status', 'success') == 'success':
            samples[component].append((record.get('version'), f'ascend.{key}.version'))
            for item in record.get('installed_candidates') or []:
                samples[component].append((item.get('version'), f'ascend.{key}.installed_candidates'))
    py_version = runtime.get('python_version') or (data.get('_meta') or {}).get('python_version')
    py_source = 'runtime.python_version' if runtime.get('python_version') else '_meta.python_version'
    samples['python'].append((py_version, py_source))
    result = {key: observation(key, values) for key, values in sorted(samples.items())}
    for key, item in overrides.items():
        if (key not in result or not isinstance(item, dict) or not version(item.get('version'), key)
                or not isinstance(item.get('source'), str) or not item['source'].strip()):
            raise ValueError(f'补充证据 {key} 须包含有效版本和来源')
        result[key] = observation(key, [(item['version'], item['source'])])
    return result


def satisfies(actual, expected, component):
    if component == 'python' and '..' in expected:
        if not PYTHON_RELEASE.fullmatch(actual):
            return False
        lower, upper = expected.split('..')
        parts = [tuple(map(int, value.split('.'))) for value in (lower, actual, upper)]
        floor, found, ceiling = [value + (0,) * (3 - len(value)) for value in parts]
        # 仅采到 major.minor 时无法判断位于同一小版本的补丁下限。
        if len(parts[1]) == 2 and floor[:2] == found[:2] and floor[2]:
            return None
        return floor <= found < ceiling
    if expected.endswith('*'):
        if component == 'python' and expected.endswith('.*') and actual == expected[:-2]:
            return True
        return bool(re.fullmatch(re.escape(expected[:-1].lower()) + r'\d+', actual))
    return actual == version(expected, component)


def assess(actual, expected, component):
    outcomes = [satisfies(actual, value, component) for value in expected] if actual else []
    if any(outcomes):
        status = 'matched'
    elif not outcomes or None in outcomes:
        status = 'unknown'
    else:
        status = 'deviation'
    return {'status': status, 'observed': actual, 'expected': expected}


def best_plans(versions, matrix, scene, keep=None, required=()):
    """Rank complete rows within one scene, optionally keeping one observed version."""
    anchor = SCENES[scene]
    candidates = [
        (rule, {anchor: [rule['release']], **rule['checks'], **rule.get('host_baseline', {})})
        for rule in matrix['rules'][scene]
    ]
    scoped = set().union(*(requirements for _, requirements in candidates))
    required = set(required)
    ranked = []
    for rule, requirements in candidates:
        if not requirements.keys() >= required:
            continue
        checks = {key: assess(versions.get(key), expected, key)
                  for key, expected in requirements.items()}
        if keep and checks.get(keep, {}).get('status') != 'matched':
            continue
        changes = {key: check for key, check in checks.items() if check['status'] == 'deviation'}
        unknown = sum(check['status'] == 'unknown' for key, check in checks.items() if key in versions)
        uncovered = sorted((versions.keys() & scoped) - requirements.keys())
        rank = (len(changes), unknown + len(uncovered), anchor in changes)
        plan = {'rule': rule['id'], 'release': rule['release'], 'checks': checks,
                'changes': changes, 'optional': rule.get('optional', []), 'uncovered': uncovered}
        if 'product' in rule:
            plan['product'] = rule['product']
        if 'package_baseline' in rule:
            plan['package_baseline'] = rule['package_baseline']
        ranked.append((rank, plan))
    if not ranked:
        return []
    best_rank = min(rank for rank, _ in ranked)
    return [plan for rank, plan in ranked if rank == best_rank]


def remediation(versions, matrix, scene):
    """Return the current release's closest rows and actionable alternatives."""
    if not scene or SCENES[scene] not in versions:
        return [], []
    current = best_plans(versions, matrix, scene, keep=SCENES[scene])
    if not current or not current[0]['changes']:
        return current, []
    confirmed_deviations = set.intersection(*(set(plan['changes']) for plan in current))
    return current, best_plans(versions, matrix, scene, required=confirmed_deviations)


def check_software(observed, matrix, scene):
    if not scene:
        return {'status': 'unknown', 'reason': '检测到多个业务栈，请指定场景。', 'checks': {}}
    anchor = SCENES[scene]
    actual = observed[anchor]['version']
    if not actual:
        return {'status': 'unknown', 'reason': f'{anchor} 版本缺失或有冲突。', 'checks': {}}
    candidates = []
    for rule in matrix['rules'][scene]:
        if actual != version(rule['release'], anchor):
            continue
        checks = {key: assess(observed[key]['version'], expected, key)
                  for key, expected in rule['checks'].items()
                  if key not in rule.get('optional', ()) or observed[key]['evidence']}
        statuses = [entry['status'] for entry in checks.values()]
        candidates.append(((statuses.count('deviation'), statuses.count('unknown')), rule, checks))
    if not candidates:
        return {'status': 'unknown', 'reason': '矩阵未覆盖该发布版本。', 'checks': {}}
    ranking = min(candidate[0] for candidate in candidates)
    best = [(rule, checks) for rank, rule, checks in candidates if rank == ranking]
    if len(best) > 1 and ranking != (0, 0):
        return {'status': 'unknown', 'reason': '存在多个配套候选，请核对实际加载版本。',
                'checks': {}, 'candidates': [{'id': rule['id'], 'checks': checks,
                    'source': matrix['sources'][rule['source']]} for rule, checks in best]}
    rule, checks = best[0]
    status = 'deviation' if ranking[0] else 'unknown' if ranking[1] else 'matched'
    result = {'status': status, 'rule': rule['id'], 'source': matrix['sources'][rule['source']],
              'checks': checks}
    for key in ('product', 'host_baseline', 'package_baseline'):
        if key in rule:
            result[key] = rule[key]
    return result


def check_boards(data, source):
    result = []
    for index, device in enumerate((data.get('npu') or {}).get('devices') or []):
        query = (device.get('queries') or {}).get('board') or {}
        raw = device.get('compatibility')
        path = f'npu.devices[{index}].compatibility'
        if raw is None:
            board = (device.get('raw') or {}).get('board') or ''
            match = re.search(r'^\s*Compatibility\s*:\s*(\S+)\s*$', board, re.M | re.I)
            raw = match[1] if match else None
            path = f'npu.devices[{index}].raw.board'
        status = {'OK': 'matched', 'NOK': 'incompatible'}.get(str(raw).strip().upper(), 'unknown')
        if query.get('status') == 'failed' or query.get('returncode', 0) != 0:
            status = 'unknown'
            path = f'npu.devices[{index}].queries.board'
        result.append({'device_id': device.get('device_id'), 'model': device.get('name'),
                       'chip': device.get('chip_type'), 'status': status,
                       'observed': raw, 'evidence': path, 'source': source})
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description='离线核对昇腾环境版本配套。')
    parser.add_argument('dump', type=Path, help='采集快照 JSON')
    parser.add_argument('--scene', choices=('auto', *SCENES), default='auto', help='业务场景')
    parser.add_argument('--observed', type=Path, help='已核实的业务版本及证据 JSON')
    parser.add_argument('--output', type=Path, help='结果 JSON 路径，省略则输出到终端')
    args = parser.parse_args(argv)
    try:
        matrix = read_object(Path(__file__).with_name('compatibility_matrix.json'))
        data = read_object(args.dump)
        observed = collect_versions(data, read_object(args.observed) if args.observed else {})
        choices = [scene for scene, anchor in SCENES.items()
                   if scene != 'torch' and (observed[anchor]['evidence']
                       or (scene == 'vllm' and observed['vllm']['evidence']))]
        inferred = (choices or ['torch'])[0] if len(choices) <= 1 else None
        scene = inferred if args.scene == 'auto' else args.scene
        software = check_software(observed, matrix, scene)
        host = {key: dict(assess(observed[key]['version'], expected, key),
                          source=matrix['sources']['vllmHost'])
                for key, expected in software.get('host_baseline', {}).items()}
        host.setdefault('hdk', {'status': 'unknown', 'observed': observed['hdk']['version'],
                                'reason': '按硬件型号与 CANN 核对宿主机 HDK。',
                                'source': matrix['sources']['hdk']})
        system = data.get('system') or {}
        host['os_hardware'] = {
            'status': 'unknown',
            'observed': {
                'os': system.get('os_release'),
                'arch': system.get('machine'),
                'container': ((data.get('runtime') or {}).get('container') or {}).get('in_container'),
            },
            'reason': '按宿主机型号、系统和架构核对。',
            'source': matrix['sources']['hdk'],
        }
        component_support = {}
        if scene == 'mindspeed' and software.get('rule'):
            wheel = observed['torch_npu']['version']
            products = {rule['product'] for rule in matrix['rules']['torch']
                        if wheel and wheel == version(rule['release'], 'torch_npu')}
            product = next(iter(products)) if len(products) == 1 else None
            for key, actual in (('cann', observed['cann']['version']), ('torch_npu_product', product)):
                component_support[key] = dict(
                    assess(actual, matrix['mindspeed_component_support'][key], key),
                    source=matrix['sources']['mindspeed'])
        versions = {key: item['version'] for key, item in observed.items() if item['version']}
        _, recommendations = remediation(versions, matrix, scene)
        result = {'scene': scene, 'scene_source': '推断' if args.scene == 'auto' else '指定',
                  'observed': observed, 'software': software, 'host': host,
                  'component_support': component_support, 'recommendations': recommendations,
                  'boards': check_boards(data, matrix['sources']['board'])}
        output = json.dumps(result, ensure_ascii=False, indent=2) + '\n'
        if args.output:
            inputs = [args.dump, args.observed, Path(__file__).with_name('compatibility_matrix.json')]
            if args.output.resolve() in {path.resolve() for path in inputs if path}:
                raise ValueError('输出须使用独立文件')
            args.output.write_text(output, encoding='utf-8')
        else:
            sys.stdout.write(output)
    except (OSError, ValueError, TypeError, AttributeError, KeyError) as exc:
        print(f'配套检查失败：{exc}', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
