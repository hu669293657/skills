#!/usr/bin/env python3
"""Phase 2 fallback: cluster analysis without msprof-analyze CLI.

Reads per-card text outputs (step_trace_time.csv / communication.json /
communication_matrix.json / profiler_metadata.json) and builds
<root>/cluster_analysis_output/cluster_analysis.db with the same table
schema as msprof-analyze cluster output, so MindStudio Insight and later
recipes can consume it.

Units: step trace in us (kept as-is); communication times in ms;
transit size in MB; bandwidth in GB/s.
"""
import argparse
import csv
import hashlib
import json
import os
import re
import sqlite3
import sys

from lib.common import is_junk, read_csv_rows, read_json, to_f

CARD_DIR_PATTERN = re.compile(r'.*_ascend_(pt|ms)$')
HOST_FROM_CARD = re.compile(r'^(.*)_\d+_\d+_ascend_(pt|ms)$')

BASE_TABLES = {
    'ClusterBaseInfo': ('key TEXT PRIMARY KEY, value TEXT'),
    'HostInfo': ('hostUid TEXT PRIMARY KEY, hostName TEXT'),
    'RankDeviceMap': ('rankId INTEGER, deviceId INTEGER, hostUid TEXT, profilePath TEXT'),
    'ClusterStepTraceTime': (
        'step TEXT, type TEXT, "index" INTEGER, computing REAL, '
        'communication_not_overlapped REAL, overlapped REAL, communication REAL, '
        'free REAL, stage REAL, bubble REAL, '
        'communication_not_overlapped_and_exclude_receive REAL, preparing REAL, '
        'dp_index INTEGER, pp_index INTEGER, tp_index INTEGER'),
    'CommunicationGroupMapping': (
        'type TEXT, rank_set TEXT, group_name TEXT, group_id TEXT, pg_name TEXT'),
    'ClusterCommunicationTime': (
        'step TEXT, rank_id INTEGER, hccl_op_name TEXT, group_name TEXT, '
        'start_timestamp REAL, elapsed_time REAL, transit_time REAL, wait_time REAL, '
        'synchronization_time REAL, idle_time REAL, '
        'synchronization_time_ratio REAL, wait_time_ratio REAL'),
    'ClusterCommunicationBandwidth': (
        'step TEXT, rank_id INTEGER, hccl_op_name TEXT, group_name TEXT, '
        'band_type TEXT, transit_size REAL, transit_time REAL, bandwidth REAL, '
        'large_packet_ratio REAL, package_size REAL, "count" INTEGER, total_duration REAL'),
    'ClusterCommunicationMatrix': (
        'step TEXT, hccl_op_name TEXT, group_name TEXT, src_rank REAL, dst_rank REAL, '
        'transport_type TEXT, op_name TEXT, transit_size REAL, transit_time REAL, '
        'bandwidth REAL'),
}


def find_card_dirs(root):
    cards = []
    for dirpath, dirnames, _ in os.walk(root):
        rel = os.path.relpath(dirpath, root)
        if rel.count(os.sep) >= 3:
            dirnames[:] = []
            continue
        dirnames[:] = [d for d in dirnames if not is_junk(d)]
        for d in dirnames:
            if CARD_DIR_PATTERN.match(d):
                cards.append(os.path.join(dirpath, d))
    return sorted(cards)


def read_rank(card_dir):
    for fn in sorted(os.listdir(card_dir)):
        if is_junk(fn):
            continue
        m = re.match(r'^profiler_info_(\d+)\.json$', fn)
        if m:
            data = read_json(os.path.join(card_dir, fn)) or {}
            return int(data.get('rank_id', m.group(1)))
    return None


def host_of(card_dir):
    m = HOST_FROM_CARD.match(os.path.basename(card_dir))
    if m:
        return m.group(1)
    return 'unknown-host'


def host_uid(hostname):
    return str(int(hashlib.md5(hostname.encode('utf-8')).hexdigest()[:16], 16))


def collect_card(card_dir):
    apo = os.path.join(card_dir, 'ASCEND_PROFILER_OUTPUT')
    rank = read_rank(card_dir)
    meta = read_json(os.path.join(card_dir, 'profiler_metadata.json')) or {}
    dist = meta.get('distributed_args', {}) if isinstance(meta, dict) else {}
    hostname = host_of(card_dir)
    card = {
        'dir': card_dir,
        'apo': apo,
        'rank': rank,
        'host': hostname,
        'host_uid': host_uid(hostname),
        'dist': dist,
        'groups': meta.get('parallel_group_info', {}) if isinstance(meta, dict) else {},
        'steps': read_csv_rows(os.path.join(apo, 'step_trace_time.csv')),
        'comm': read_json(os.path.join(apo, 'communication.json')) or {},
        'matrix': read_json(os.path.join(apo, 'communication_matrix.json')) or {},
    }
    return card


def parallel_indexes(rank, tp, pp):
    tp = max(1, tp)
    pp = max(1, pp)
    return rank // (tp * pp), (rank // tp) % pp, rank % tp  # dp, pp, tp


def write_cluster_db(root, cards):
    out_dir = os.path.join(root, 'cluster_analysis_output')
    os.makedirs(out_dir, exist_ok=True)
    db_path = os.path.join(out_dir, 'cluster_analysis.db')
    if os.path.isfile(db_path):
        os.remove(db_path)
    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    for table, ddl in BASE_TABLES.items():
        cur.execute(f'CREATE TABLE IF NOT EXISTS {table} ({ddl})')

    # ClusterBaseInfo
    dist0 = cards[0]['dist']
    world = dist0.get('world_size') or len(cards)
    base_info = [
        ('algorithm', dist0.get('algorithm') or 'unknown'),
        ('world_size', str(world)),
        ('tp_size', str(dist0.get('tensor_model_parallel_size', 1))),
        ('pp_size', str(dist0.get('pipeline_model_parallel_size', 1))),
        ('dp_size', str(dist0.get('data_parallel_size', world))),
        ('cp_size', str(dist0.get('context_parallel_size', 1))),
        ('ep_size', str(dist0.get('expert_model_parallel_size', 1))),
        ('rank_card_num', str(len(cards))),
        ('gen_by', 'npu-perf-analyzer cluster_fallback'),
    ]
    cur.executemany('INSERT INTO ClusterBaseInfo VALUES (?, ?)', base_info)

    # HostInfo / RankDeviceMap
    hosts = {}
    for c in cards:
        hosts.setdefault(c['host_uid'], c['host'])
    cur.executemany('INSERT INTO HostInfo VALUES (?, ?)', list(hosts.items()))
    for c in cards:
        cur.execute('INSERT INTO RankDeviceMap VALUES (?, ?, ?, ?)',
                    (c['rank'], c['rank'], c['host_uid'], c['dir']))

    # ClusterStepTraceTime (us)
    tp = int(dist0.get('tensor_model_parallel_size', 1) or 1)
    pp = int(dist0.get('pipeline_model_parallel_size', 1) or 1)
    for c in cards:
        for r in c['steps']:
            step = str(r.get('Step', '')).strip()
            if not step:
                continue
            dp_i, pp_i, tp_i = parallel_indexes(c['rank'], tp, pp)
            cur.execute(
                'INSERT INTO ClusterStepTraceTime VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                (step, 'rank', c['rank'],
                 to_f(r.get('Computing')),
                 to_f(r.get('Communication(Not Overlapped)')),
                 to_f(r.get('Overlapped')),
                 to_f(r.get('Communication')),
                 to_f(r.get('Free')),
                 to_f(r.get('Stage')),
                 to_f(r.get('Bubble')),
                 to_f(r.get('Communication(Not Overlapped and Exclude Receive)')),
                 to_f(r.get('Preparing')),
                 dp_i, pp_i, tp_i))

    # CommunicationGroupMapping from metadata parallel_group_info
    group_rows = set()
    for c in cards:
        for gname, info in (c['groups'] or {}).items():
            if not isinstance(info, dict):
                continue
            ranks = info.get('global_ranks') or []
            pg = info.get('group_name', '')
            group_rows.add(('collective', '(' + ','.join(str(x) for x in ranks) + ')',
                            gname, '', pg))
    cur.executemany('INSERT INTO CommunicationGroupMapping VALUES (?,?,?,?,?)',
                    sorted(group_rows))

    # ClusterCommunicationTime / Bandwidth (ms / MB / GB/s)
    for c in cards:
        for step, groups in c['comm'].items():
            for gtype in ('collective', 'p2p'):
                for op_key, info in (groups.get(gtype) or {}).items():
                    name, _, gid = op_key.partition('@')
                    ti = info.get('Communication Time Info', {})
                    cur.execute(
                        'INSERT INTO ClusterCommunicationTime VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                        (step, c['rank'], name, gid,
                         to_f(ti.get('Start Timestamp(us)')),
                         to_f(ti.get('Elapse Time(ms)')),
                         to_f(ti.get('Transit Time(ms)')),
                         to_f(ti.get('Wait Time(ms)')),
                         to_f(ti.get('Synchronization Time(ms)')),
                         to_f(ti.get('Idle Time(ms)')),
                         to_f(ti.get('Synchronization Time Ratio')),
                         to_f(ti.get('Wait Time Ratio'))))
                    for band, bi in (info.get('Communication Bandwidth Info') or {}).items():
                        size = to_f(bi.get('Transit Size(MB)'))
                        ttime = to_f(bi.get('Transit Time(ms)'))
                        if size <= 0 and ttime <= 0:
                            continue  # skip no-traffic band rows (msprof only records real traffic)
                        cur.execute(
                            'INSERT INTO ClusterCommunicationBandwidth VALUES (?,?,?,?,?,?,?,?,?,?,?,?)',
                            (step, c['rank'], name, gid, band,
                             size,
                             ttime,
                             to_f(bi.get('Bandwidth(GB/s)')),
                             to_f(bi.get('Large Packet Ratio')),
                             to_f(bi.get('Package Size')),
                             int(to_f(bi.get('Package Count'))),
                             to_f(bi.get('Total Duration(ms)'))))

    # ClusterCommunicationMatrix
    for c in cards:
        for step, groups in c['matrix'].items():
            for gtype in ('collective', 'p2p'):
                for op_key, links in (groups.get(gtype) or {}).items():
                    name, _, gid = op_key.partition('@')
                    for pair, link in (links or {}).items():
                        src, _, dst = pair.partition('-')
                        size = to_f(link.get('Transit Size(MB)'))
                        ttime = to_f(link.get('Transit Time(ms)'))
                        if size <= 0 and ttime <= 0:
                            continue  # skip no-traffic links
                        cur.execute(
                            'INSERT INTO ClusterCommunicationMatrix VALUES (?,?,?,?,?,?,?,?,?,?)',
                            (step, name, gid,
                             to_f(src, -1), to_f(dst, -1),
                             link.get('Transport Type', ''),
                             link.get('Op Name', ''),
                             size,
                             ttime,
                             to_f(link.get('Bandwidth(GB/s)'))))

    cur.execute('CREATE TABLE IF NOT EXISTS status_info (id INTEGER PRIMARY KEY AUTOINCREMENT, '
                'key TEXT, value TEXT)')
    cur.execute("INSERT INTO status_info (key, value) VALUES ('Cluster files parsing status', 'FINISH')")
    conn.commit()
    conn.close()
    return db_path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--root', required=True, help='multi-card root (cluster_analysis_output parent)')
    parser.add_argument('--force', action='store_true', help='overwrite existing cluster_analysis.db')
    args = parser.parse_args()

    root = os.path.abspath(args.root)
    card_dirs = find_card_dirs(root)
    if not card_dirs:
        print('ERROR: no card dirs (*_ascend_pt / *_ascend_ms) found', file=sys.stderr)
        sys.exit(1)

    out_dir = os.path.join(root, 'cluster_analysis_output')
    db_path = os.path.join(out_dir, 'cluster_analysis.db')
    if os.path.isfile(db_path) and not args.force:
        print(f'EXISTS: {db_path} (use --force to rebuild)')
        sys.exit(0)

    cards = []
    for cd in card_dirs:
        if not os.path.isdir(os.path.join(cd, 'ASCEND_PROFILER_OUTPUT')):
            print(f'skip (no ASCEND_PROFILER_OUTPUT): {cd}')
            continue
        c = collect_card(cd)
        if c['rank'] is None:
            c['rank'] = len(cards)
        if not c['steps'] and not c['comm']:
            print(f'skip (no step/comm data): {cd}')
            continue
        cards.append(c)
    if len(cards) < 2:
        print('ERROR: fewer than 2 usable cards; cluster analysis needs multi-card data',
              file=sys.stderr)
        sys.exit(1)
    cards.sort(key=lambda c: c['rank'])

    db_path = write_cluster_db(root, cards)

    conn = sqlite3.connect(db_path)
    cur = conn.cursor()
    counts = {}
    for t in BASE_TABLES:
        cur.execute(f'SELECT COUNT(*) FROM {t}')
        counts[t] = cur.fetchone()[0]
    conn.close()

    print(f'cluster_analysis.db written: {db_path}')
    for t, n in counts.items():
        print(f'  {t}: {n} rows')
    print('cards: ' + ', '.join(f"rank{c['rank']}@{c['host']}" for c in cards))


if __name__ == '__main__':
    main()
