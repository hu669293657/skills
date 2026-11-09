"""Normalize environment sidecars independently of scheduling and binding evidence."""
import json
import re
from pathlib import PurePosixPath


ENV_CATEGORIES = ('environment_host', 'environment_ascend', 'environment_process')
TARGET_FIELDS = ('exe', 'environment', 'limits', 'libraries')
PREFIX = 'hostbound/environment/'
PID_PATH = re.compile(r'hostbound/environment/pid_([1-9][0-9]*)\.json')
VERSION_KEY = re.compile(r'version(?:_dir)?|ascend-cann-atb version|ascend-mindie(?: version)?', re.I)
LIMIT = re.compile(r'(Max .+?)\s{2,}(\S+)\s+(\S+)(?:\s+(\S+))?\s*$')


def _assignments(raw):
    return dict(line.split('=', 1) for line in raw.splitlines() if '=' in line)


def _target_field(field, raw):
    if field == 'environment':
        return _assignments(raw)
    if field == 'libraries':
        return sorted(set(raw.splitlines()))
    if field == 'limits':
        values = {}
        for line in raw.splitlines():
            match = LIMIT.fullmatch(line)
            if match:
                name, soft, hard, unit = match.groups()
                values[name] = {'soft': soft, 'hard': hard, 'unit': unit}
        if not values:
            raise ValueError('未解析到进程资源限额。')
        return values
    return raw.strip()


def parse_environment_value(category, path, raw):
    """Parse only supported sidecar shapes. Unrecognized versions remain unknown."""
    if category == 'environment_process':
        match = PID_PATH.fullmatch(path)
        data = json.loads(raw)
        if (not match or not isinstance(data, dict) or data.get('pid') != int(match[1])
                or not re.fullmatch(r'[0-9]+', str(data.get('starttime', '')))):
            raise ValueError('目标 PID 或 starttime 无效。')
        result = {'pid': data['pid'], 'starttime': int(data['starttime'])}
        for field in TARGET_FIELDS:
            item = data.get(field)
            if not isinstance(item, dict) or item.get('status') not in (
                    'COLLECTED', 'UNSUPPORTED', 'PERMISSION_DENIED', 'EXECUTION_FAILED'):
                raise ValueError('目标进程快照字段无效：' + field)
            status = item['status']
            value = None
            reason = item.get('reason')
            if status == 'COLLECTED':
                if not isinstance(item.get('text'), str):
                    raise ValueError('目标进程快照缺少文本：' + field)
                try:
                    value = _target_field(field, item['text'])
                except ValueError as exc:
                    status, reason = 'PARSE_FAILED', str(exc)
            result[field] = {'status': status, 'source': item.get('source'),
                             'reason': reason, 'value': value}
        return result

    name = PurePosixPath(path).stem
    value = raw.strip()
    if category == 'environment_ascend':
        versions = []
        for line in raw.splitlines():
            key, sep, text = line.partition('=' if '=' in line else ':')
            if sep and VERSION_KEY.fullmatch(key.strip()) and text.strip():
                versions.append(text.strip().strip('"\''))
        value = ' / '.join(dict.fromkeys(versions))
    elif name == 'os_release':
        fields = _assignments(raw)
        value = next((fields[key].strip('"\'') for key in ('PRETTY_NAME', 'NAME', 'ID')
                      if fields.get(key)), '')
    elif name == 'thp':
        match = re.search(r'\[([^\]]+)\]', value)
        value = match[1] if match else value
    if not value:
        raise ValueError('未解析到可用的环境字段。')
    return value


def analyze_environment(snapshot, integrity):
    rows = [row for row in integrity.get('entries', []) if row.get('category') in ENV_CATEGORIES]
    result = {'status': 'UNKNOWN', 'host': [], 'ascend': [], 'targets': [], 'target_scope': 'UNKNOWN'}
    for entry in rows:
        category = entry['category']
        path = entry.get('raw_path') or ''
        status = entry['status']
        value = None
        if status == 'COLLECTED' and path in snapshot:
            value = parse_environment_value(category, path, snapshot[path])
        row = {'status': status, 'source': entry.get('source') or entry.get('command') or path,
               'raw_path': path or None, 'reason': entry.get('failure_reason'),
               'collected_at': entry.get('ended_at')}
        if category != 'environment_process':
            result['host' if category == 'environment_host' else 'ascend'].append(
                dict(row, id=PurePosixPath(path).stem, value=value))
            continue
        if status == 'NOT_REQUESTED':
            result['target_scope'] = 'NOT_REQUESTED'
            continue
        match = PID_PATH.fullmatch(path)
        if match:
            result['target_scope'] = 'REQUESTED'
            row['pid'] = int(match[1])
            if value:
                row.update(value)
                if any(value[field]['status'] != 'COLLECTED' for field in TARGET_FIELDS):
                    row['status'] = 'PARTIAL'
            result['targets'].append(row)
    if rows:
        result['status'] = 'AVAILABLE' if any(row['status'] in ('COLLECTED', 'PARTIAL')
                                             for key in ('host', 'ascend', 'targets')
                                             for row in result[key]) else 'UNAVAILABLE'
    return result
