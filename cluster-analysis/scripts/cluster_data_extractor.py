#!/usr/bin/env python3
"""
cluster_data_extractor.py
Ascend 集群性能数据提取器 — 自动识别 DB/TEXT 格式，提取全景数据，输出 JSON。
"""
import argparse
import csv
import json
import os
import sqlite3
import sys
from datetime import datetime


def detect_format(data_dir):
    """识别数据格式: db_old / db_new / text / mixed"""
    result = {"format": "unknown", "db_path": None, "text_dir": None}

    # 检查 cluster.db (旧格式)
    cluster_db = os.path.join(data_dir, "cluster.db")
    if os.path.exists(cluster_db):
        result["db_path"] = cluster_db
        result["format"] = "db_old"

    # 检查 cluster_analysis_output/cluster_communication_analyzer.db (新格式)
    new_db = os.path.join(data_dir, "cluster_analysis_output", "cluster_communication_analyzer.db")
    if os.path.exists(new_db):
        if result["format"] == "db_old":
            result["format"] = "mixed"
        else:
            result["format"] = "db_new"
        result["db_path"] = new_db

    # 检查 TEXT 文件
    text_csv = os.path.join(data_dir, "cluster_analysis_output", "cluster_step_trace_time.csv")
    if os.path.exists(text_csv):
        if result["format"] in ("db_old", "db_new"):
            result["format"] = "mixed"
        else:
            result["format"] = "text"
        result["text_dir"] = os.path.join(data_dir, "cluster_analysis_output")

    return result


def get_table_list(conn):
    cur = conn.cursor()
    cur.execute("SELECT name FROM sqlite_master WHERE type='table'")
    return [row[0] for row in cur.fetchall()]


def get_columns(conn, table):
    cur = conn.cursor()
    cur.execute(f"PRAGMA table_info({table})")
    return [row[1] for row in cur.fetchall()]


def query_to_dicts(conn, sql, params=None):
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


# ==================== DB 旧格式提取 ====================

def extract_db_old(db_path):
    data = {"format": "db_old", "tables_found": [], "tables_empty": []}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = get_table_list(conn)
    data["tables_found"] = tables

    # cluster_base_info
    if "cluster_base_info" in tables:
        rows = query_to_dicts(conn, "SELECT key, value FROM cluster_base_info")
        data["base_info"] = {row["key"]: row["value"] for row in rows}

    # step_statistic_info
    if "step_statistic_info" in tables:
        rows = query_to_dicts(conn, "SELECT * FROM step_statistic_info ORDER BY step_id, rank_id")
        if rows:
            data["step_statistic"] = rows
            # 按 step 汇总
            step_summary = {}
            for r in rows:
                sid = r.get("step_id", "?")
                if sid not in step_summary:
                    step_summary[sid] = {"compute": [], "comm": [], "free": [], "stage": [], "overlap": []}
                s = step_summary[sid]
                s["compute"].append(r.get("compute_time", 0))
                s["comm"].append(r.get("communication_time", 0))
                s["free"].append(r.get("free_time", 0))
                s["stage"].append(r.get("stage_time", 0))
                s["overlap"].append(r.get("overlap_communication_time", 0))
            data["step_summary"] = {}
            for sid, s in step_summary.items():
                n = len(s["compute"])
                data["step_summary"][sid] = {
                    "avg_compute": round(sum(s["compute"]) / n, 2),
                    "avg_comm": round(sum(s["comm"]) / n, 2),
                    "avg_free": round(sum(s["free"]) / n, 2),
                    "avg_stage": round(sum(s["stage"]) / n, 2),
                    "avg_overlap": round(sum(s["overlap"]) / n, 2),
                }
            # Rank 级汇总
            rank_summary = {}
            for r in rows:
                rid = str(r.get("rank_id", "?"))
                if rid not in rank_summary:
                    rank_summary[rid] = {"compute": [], "comm": [], "free": [], "stage": []}
                rs = rank_summary[rid]
                rs["compute"].append(r.get("compute_time", 0))
                rs["comm"].append(r.get("communication_time", 0))
                rs["free"].append(r.get("free_time", 0))
                rs["stage"].append(r.get("stage_time", 0))
            data["rank_summary"] = {}
            for rid, rs in rank_summary.items():
                n = len(rs["compute"])
                data["rank_summary"][rid] = {
                    "avg_compute": round(sum(rs["compute"]) / n, 2),
                    "avg_comm": round(sum(rs["comm"]) / n, 2),
                    "avg_free": round(sum(rs["free"]) / n, 2),
                    "avg_stage": round(sum(rs["stage"]) / n, 2),
                }
        else:
            data["tables_empty"].append("step_statistic_info")
    else:
        data["tables_empty"].append("step_statistic_info")

    # communication_time_info
    if "communication_time_info" in tables:
        rows = query_to_dicts(conn, "SELECT op_name, AVG(elapse_time) as avg_elapsed, AVG(transit_time) as avg_transit, AVG(wait_time) as avg_wait, AVG(synchronization_time) as avg_sync FROM communication_time_info GROUP BY op_name ORDER BY avg_elapsed DESC LIMIT 20")
        if rows:
            data["comm_time_ops"] = rows
        else:
            data["tables_empty"].append("communication_time_info")

    # communication_bandwidth_info
    if "communication_bandwidth_info" in tables:
        rows = query_to_dicts(conn, "SELECT transport_type, AVG(bandwidth_size) as avg_bw, AVG(transit_size) as avg_size, AVG(transit_time) as avg_time FROM communication_bandwidth_info GROUP BY transport_type")
        if rows:
            data["comm_bandwidth"] = rows
        else:
            data["tables_empty"].append("communication_bandwidth_info")

    # communication_matrix
    if "communication_matrix" in tables:
        rows = query_to_dicts(conn, "SELECT src_rank, dst_rank, transport_type, AVG(bandwidth) as avg_bw, AVG(transit_size) as avg_size FROM communication_matrix GROUP BY src_rank, dst_rank, transport_type LIMIT 50")
        if rows:
            data["comm_matrix"] = rows
        else:
            data["tables_empty"].append("communication_matrix")

    conn.close()
    return data


# ==================== DB 新格式提取 ====================

def extract_db_new(db_path):
    data = {"format": "db_new", "tables_found": [], "tables_empty": []}
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    tables = get_table_list(conn)
    data["tables_found"] = tables

    # ClusterStepTraceTime
    step_table = None
    for t in ["ClusterStepTraceTime", "step_statistic_info"]:
        if t in tables:
            step_table = t
            break
    if step_table:
        cols = get_columns(conn, step_table)
        # 自适应字段名
        f_step = "step" if "step" in cols else "step_id"
        f_type = "type" if "type" in cols else None
        f_index = "index" if "index" in cols else "rank_id"
        f_compute = "computing" if "computing" in cols else "compute_time"
        f_comm = "communication" if "communication" in cols else "communication_time"
        f_free = "free" if "free" in cols else "free_time"
        f_stage = "stage" if "stage" in cols else "stage_time"
        f_overlap = "overlapped" if "overlapped" in cols else "overlap_communication_time"

        type_filter = f" WHERE {f_type} = 'rank'" if f_type else ""
        rows = query_to_dicts(conn, f"SELECT * FROM {step_table}{type_filter} ORDER BY {f_step}, {f_index}")
        if rows:
            data["step_statistic"] = rows
            # 汇总
            step_summary = {}
            for r in rows:
                sid = str(r.get(f_step, "?"))
                if sid not in step_summary:
                    step_summary[sid] = {"compute": [], "comm": [], "free": [], "stage": [], "overlap": []}
                s = step_summary[sid]
                s["compute"].append(r.get(f_compute, 0))
                s["comm"].append(r.get(f_comm, 0))
                s["free"].append(r.get(f_free, 0))
                s["stage"].append(r.get(f_stage, 0))
                s["overlap"].append(r.get(f_overlap, 0))
            data["step_summary"] = {}
            for sid, s in step_summary.items():
                n = len(s["compute"])
                data["step_summary"][sid] = {
                    "avg_compute": round(sum(s["compute"]) / n, 2),
                    "avg_comm": round(sum(s["comm"]) / n, 2),
                    "avg_free": round(sum(s["free"]) / n, 2),
                    "avg_stage": round(sum(s["stage"]) / n, 2),
                    "avg_overlap": round(sum(s["overlap"]) / n, 2),
                }
        else:
            data["tables_empty"].append(step_table)
    else:
        data["tables_empty"].append("step_table")

    # ClusterCommunicationTime
    if "ClusterCommunicationTime" in tables:
        rows = query_to_dicts(conn, "SELECT hccl_op_name, AVG(elapsed_time) as avg_elapsed, AVG(transit_time) as avg_transit, AVG(wait_time) as avg_wait, AVG(synchronization_time) as avg_sync, AVG(idle_time) as avg_idle FROM ClusterCommunicationTime GROUP BY hccl_op_name ORDER BY avg_elapsed DESC LIMIT 20")
        if rows:
            data["comm_time_ops"] = rows
        else:
            data["tables_empty"].append("ClusterCommunicationTime")

    # ClusterCommunicationBandwidth
    if "ClusterCommunicationBandwidth" in tables:
        rows = query_to_dicts(conn, "SELECT band_type, AVG(bandwidth) as avg_bw, AVG(transit_size) as avg_size, AVG(transit_time) as avg_time FROM ClusterCommunicationBandwidth GROUP BY band_type")
        if rows:
            data["comm_bandwidth"] = rows
        else:
            data["tables_empty"].append("ClusterCommunicationBandwidth")

    conn.close()
    return data


# ==================== TEXT 模式提取 ====================

def extract_text(text_dir):
    data = {"format": "text", "tables_found": [], "tables_empty": []}

    # CSV
    csv_path = os.path.join(text_dir, "cluster_step_trace_time.csv")
    if os.path.exists(csv_path):
        data["tables_found"].append("cluster_step_trace_time.csv")
        rows = []
        with open(csv_path, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for row in reader:
                rows.append(row)
        if rows:
            rank_rows = [r for r in rows if r.get("Type") == "rank"]
            data["step_statistic"] = rank_rows
            # 汇总
            step_summary = {}
            for r in rank_rows:
                sid = str(r.get("Step", "?"))
                if sid not in step_summary:
                    step_summary[sid] = {"compute": [], "comm": [], "free": [], "stage": [], "overlap": []}
                s = step_summary[sid]
                s["compute"].append(float(r.get("Computing", 0)))
                s["comm"].append(float(r.get("Communication", 0)))
                s["free"].append(float(r.get("Free", 0)))
                s["stage"].append(float(r.get("Stage", 0)))
                s["overlap"].append(float(r.get("Overlapped", 0)))
            data["step_summary"] = {}
            for sid, s in step_summary.items():
                n = len(s["compute"])
                data["step_summary"][sid] = {
                    "avg_compute": round(sum(s["compute"]) / n, 2),
                    "avg_comm": round(sum(s["comm"]) / n, 2),
                    "avg_free": round(sum(s["free"]) / n, 2),
                    "avg_stage": round(sum(s["stage"]) / n, 2),
                    "avg_overlap": round(sum(s["overlap"]) / n, 2),
                }
            # Rank 汇总
            rank_summary = {}
            for r in rank_rows:
                rid = str(r.get("Index", "?"))
                if rid not in rank_summary:
                    rank_summary[rid] = {"compute": [], "comm": [], "free": [], "stage": []}
                rs = rank_summary[rid]
                rs["compute"].append(float(r.get("Computing", 0)))
                rs["comm"].append(float(r.get("Communication", 0)))
                rs["free"].append(float(r.get("Free", 0)))
                rs["stage"].append(float(r.get("Stage", 0)))
            data["rank_summary"] = {}
            for rid, rs in rank_summary.items():
                n = len(rs["compute"])
                data["rank_summary"][rid] = {
                    "avg_compute": round(sum(rs["compute"]) / n, 2),
                    "avg_comm": round(sum(rs["comm"]) / n, 2),
                    "avg_free": round(sum(rs["free"]) / n, 2),
                    "avg_stage": round(sum(rs["stage"]) / n, 2),
                }
        else:
            data["tables_empty"].append("cluster_step_trace_time.csv")
    else:
        data["tables_empty"].append("cluster_step_trace_time.csv")

    # JSON
    json_path = os.path.join(text_dir, "communication_group.json")
    if os.path.exists(json_path):
        data["tables_found"].append("communication_group.json")
        with open(json_path, "r", encoding="utf-8") as f:
            data["comm_group"] = json.load(f)
    else:
        data["tables_empty"].append("communication_group.json")

    return data


# ==================== 主入口 ====================

def extract_all(data_dir):
    fmt = detect_format(data_dir)
    if fmt["format"] in ("db_old", "mixed"):
        if fmt["db_path"]:
            return extract_db_old(fmt["db_path"])
    if fmt["format"] == "db_new":
        return extract_db_new(fmt["db_path"])
    if fmt["format"] == "text":
        return extract_text(fmt["text_dir"])
    if fmt["format"] == "mixed":
        # 优先 DB
        if fmt["db_path"]:
            return extract_db_old(fmt["db_path"])
    return {"format": "unknown", "error": "无法识别数据格式"}


def main():
    parser = argparse.ArgumentParser(description="集群数据提取器")
    parser.add_argument("--data-dir", required=True, help="集群数据目录路径")
    parser.add_argument("--output", default=None, help="输出 JSON 文件路径")
    args = parser.parse_args()

    data = extract_all(args.data_dir)
    data["data_dir"] = args.data_dir
    data["extract_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"数据已提取到: {args.output}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
