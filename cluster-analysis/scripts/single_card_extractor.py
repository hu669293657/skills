#!/usr/bin/env python3
"""
single_card_extractor.py
单卡 profiling 数据提取器 — 从单个 rank 目录提取算子/通信/Kernel/API 等数据，输出中间件 JSON。

参考 msprof-analyze compare_tools 的 NPUProfilingDbParser 和 NPUProfilingParser 设计。
支持两种数据源：
  1. DB 模式: mindstudio_insight_data.db (Insight DB) 或 ascend_pytorch_profiler_*.db (Profiler DB)
  2. TEXT 模式: CSV 文件 (kernel_details.csv, operator_details.csv, step_trace_time.csv 等)
"""
import argparse
import csv
import json
import os
import sqlite3
from datetime import datetime


def create_safe_connection(db_path):
    """安全 SQLite 连接，处理中文编码"""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    conn.text_factory = lambda x: x.decode('utf-8', errors='replace') if isinstance(x, bytes) else str(x)
    return conn


def query_to_dicts(conn, sql, params=None):
    cur = conn.cursor()
    if params:
        cur.execute(sql, params)
    else:
        cur.execute(sql)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def detect_format(rank_dir):
    """识别单卡数据格式: insight_db / profiler_db / text"""
    ascend_output = os.path.join(rank_dir, "ASCEND_PROFILER_OUTPUT")
    if not os.path.isdir(ascend_output):
        ascend_output = rank_dir

    # 1. mindstudio_insight_data.db
    insight_db = os.path.join(ascend_output, "mindstudio_insight_data.db")
    if os.path.exists(insight_db):
        return {"format": "insight_db", "db_path": insight_db, "ascend_output": ascend_output}

    # 2. ascend_pytorch_profiler_*.db
    for f in os.listdir(ascend_output):
        if f.startswith("ascend_pytorch_profiler") and f.endswith(".db"):
            return {"format": "profiler_db", "db_path": os.path.join(ascend_output, f), "ascend_output": ascend_output}

    # 3. TEXT 模式 (CSV)
    csv_path = os.path.join(ascend_output, "kernel_details.csv")
    if os.path.exists(csv_path):
        return {"format": "text", "db_path": None, "ascend_output": ascend_output}

    return {"format": "unknown", "db_path": None, "ascend_output": ascend_output}


def read_csv_safe(filepath, max_rows=None):
    """安全读取 CSV 文件"""
    if not os.path.exists(filepath):
        return []
    rows = []
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            reader = csv.DictReader(f)
            for i, row in enumerate(reader):
                if max_rows and i >= max_rows:
                    break
                rows.append(row)
    except Exception:
        pass
    return rows


def read_json_safe(filepath):
    if not os.path.exists(filepath):
        return {}
    try:
        with open(filepath, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


# ==================== TEXT 模式提取 ====================

def extract_text(ascend_output, rank_dir):
    data = {"format": "text", "tables_found": [], "tables_empty": []}

    # 1. step_trace_time.csv
    csv_path = os.path.join(ascend_output, "step_trace_time.csv")
    rows = read_csv_safe(csv_path)
    if rows:
        data["tables_found"].append("step_trace_time.csv")
        data["step_trace_time"] = rows
        # 计算 step 汇总
        step_summary = {}
        for r in rows:
            sid = str(r.get("Step", "?"))
            step_summary[sid] = {
                "computing": float(r.get("Computing", 0) or 0),
                "communication": float(r.get("Communication", 0) or 0),
                "free": float(r.get("Free", 0) or 0),
                "stage": float(r.get("Stage", 0) or 0),
                "overlapped": float(r.get("Overlapped", 0) or 0),
            }
        data["step_summary"] = step_summary
    else:
        data["tables_empty"].append("step_trace_time.csv")

    # 2. kernel_details.csv — 提取 Top20 + 按类型聚合
    csv_path = os.path.join(ascend_output, "kernel_details.csv")
    rows = read_csv_safe(csv_path)
    if rows:
        data["tables_found"].append("kernel_details.csv")
        # 按耗时降序 Top20
        sorted_rows = sorted(rows, key=lambda x: float(x.get("Duration(us)", 0) or 0), reverse=True)
        data["kernel_top20"] = [{
            "name": r.get("Name", "?"),
            "op_type": r.get("Type", "N/A"),
            "duration": float(r.get("Duration(us)", 0) or 0),
            "wait_time": float(r.get("Wait Time(us)", 0) or 0),
            "block_dim": int(r.get("Block Dim", 0) or 0),
            "step_id": r.get("Step Id", "?"),
        } for r in sorted_rows[:20]]
        # 按 Op Type 聚合
        type_stats = {}
        for r in rows:
            t = r.get("Type", "N/A") or "N/A"
            dur = float(r.get("Duration(us)", 0) or 0)
            if t not in type_stats:
                type_stats[t] = {"total_dur": 0, "count": 0}
            type_stats[t]["total_dur"] += dur
            type_stats[t]["count"] += 1
        data["kernel_type_stats"] = [{"op_type": t, "total_dur": round(v["total_dur"], 2),
                                       "count": v["count"], "avg_dur": round(v["total_dur"] / v["count"], 2)}
                                      for t, v in sorted(type_stats.items(), key=lambda x: x[1]["total_dur"], reverse=True)]
        data["kernel_total_count"] = len(rows)
    else:
        data["tables_empty"].append("kernel_details.csv")

    # 3. operator_details.csv — 提取 Top20
    csv_path = os.path.join(ascend_output, "operator_details.csv")
    rows = read_csv_safe(csv_path)
    if rows:
        data["tables_found"].append("operator_details.csv")
        sorted_rows = sorted(rows, key=lambda x: float(x.get("Device Total Duration(us)", 0) or 0), reverse=True)
        data["operator_top20"] = [{
            "name": r.get("Name", "?"),
            "host_dur": float(r.get("Host Total Duration(us)", 0) or 0),
            "device_dur": float(r.get("Device Total Duration(us)", 0) or 0),
            "device_self_dur": float(r.get("Device Self Duration(us)", 0) or 0),
            "input_shapes": (r.get("Input Shapes", "") or "")[:80],
        } for r in sorted_rows[:20]]
        data["operator_total_count"] = len(rows)
    else:
        data["tables_empty"].append("operator_details.csv")

    # 4. api_statistic*.csv — 在 mindstudio_profiler_output 下
    prof_output = os.path.join(rank_dir, "PROF_000001_20251108181156850_OCHGMJFGBILHDONB", "mindstudio_profiler_output")
    if not os.path.isdir(prof_output):
        # 尝试在 ascend_output 上级查找 PROF 目录
        parent = os.path.dirname(rank_dir)
        for d in os.listdir(parent) if os.path.isdir(parent) else []:
            if d.startswith("PROF_"):
                prof_output = os.path.join(parent, d, "mindstudio_profiler_output")
                if os.path.isdir(prof_output):
                    break

    api_csv = None
    if os.path.isdir(prof_output):
        for f in os.listdir(prof_output):
            if f.startswith("api_statistic") and f.endswith(".csv"):
                api_csv = os.path.join(prof_output, f)
                break
    if api_csv:
        rows = read_csv_safe(api_csv)
        if rows:
            data["tables_found"].append("api_statistic.csv")
            # 按 Level 分组
            api_by_level = {}
            for r in rows:
                level = r.get("Level", "unknown")
                if level not in api_by_level:
                    api_by_level[level] = []
                api_by_level[level].append({
                    "name": r.get("API Name", "?"),
                    "total_time": float(r.get("Time(us)", 0) or 0),
                    "count": int(r.get("Count", 0) or 0),
                    "avg_time": float(r.get("Avg(us)", 0) or 0),
                    "min_time": float(r.get("Min(us)", 0) or 0),
                    "max_time": float(r.get("Max(us)", 0) or 0),
                })
            data["api_by_level"] = api_by_level
            # Top20 by total_time
            all_apis = sorted(rows, key=lambda x: float(x.get("Time(us)", 0) or 0), reverse=True)
            data["api_top20"] = [{
                "name": r.get("API Name", "?"),
                "level": r.get("Level", "?"),
                "total_time": float(r.get("Time(us)", 0) or 0),
                "count": int(r.get("Count", 0) or 0),
                "avg_time": float(r.get("Avg(us)", 0) or 0),
            } for r in all_apis[:20]]
    else:
        data["tables_empty"].append("api_statistic.csv")

    # 5. op_summary*.csv
    op_summary_csv = None
    if os.path.isdir(prof_output):
        for f in os.listdir(prof_output):
            if f.startswith("op_summary") and f.endswith(".csv"):
                op_summary_csv = os.path.join(prof_output, f)
                break
    if op_summary_csv:
        rows = read_csv_safe(op_summary_csv)
        if rows:
            data["tables_found"].append("op_summary.csv")
            sorted_rows = sorted(rows, key=lambda x: float(x.get("Task Duration(us)", 0) or 0), reverse=True)
            data["op_summary_top20"] = [{
                "op_name": r.get("Op Name", "?"),
                "op_type": r.get("OP Type", "?"),
                "task_type": r.get("Task Type", "?"),
                "duration": float(r.get("Task Duration(us)", 0) or 0),
                "wait_time": float(r.get("Task Wait Time(us)", 0) or 0),
                "block_dim": int(r.get("Block Dim", 0) or 0),
            } for r in sorted_rows[:20]]
    else:
        data["tables_empty"].append("op_summary.csv")

    # 6. 元数据
    profiler_info = read_json_safe(os.path.join(rank_dir, "profiler_info_0.json"))
    if not profiler_info:
        for f in os.listdir(rank_dir) if os.path.isdir(rank_dir) else []:
            if f.startswith("profiler_info_") and f.endswith(".json"):
                profiler_info = read_json_safe(os.path.join(rank_dir, f))
                break
    if profiler_info:
        data["profiler_info"] = {
            "rank_id": profiler_info.get("rank_id", "?"),
            "cann_version": profiler_info.get("cann_version", "?"),
            "torch_npu_version": profiler_info.get("torch_npu_version", "?"),
        }
        config = profiler_info.get("config", {}).get("common_config", {})
        exp_config = profiler_info.get("config", {}).get("experimental_config", {})
        data["profiler_info"]["profiler_level"] = exp_config.get("_profiler_level", "?")
        data["profiler_info"]["active_steps"] = config.get("schedule", {}).get("active", "?")
        data["profiler_info"]["skip_first"] = config.get("schedule", {}).get("skip_first", "?")

    profiler_metadata = read_json_safe(os.path.join(rank_dir, "profiler_metadata.json"))
    if profiler_metadata:
        pg_info = profiler_metadata.get("parallel_group_info", {})
        data["profiler_info"]["parallel_groups"] = len(pg_info)

    return data


# ==================== Insight DB 模式提取 ====================

def extract_insight_db(db_path, ascend_output, rank_dir):
    data = {"format": "insight_db", "tables_found": [], "tables_empty": []}
    conn = create_safe_connection(db_path)
    tables = [r[0] for r in conn.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    data["tables_found"] = tables

    # 1. kernel_detail 表 — Top20 + 类型聚合
    if "kernel_detail" in tables:
        rows = query_to_dicts(conn, "SELECT * FROM kernel_detail ORDER BY duration DESC LIMIT 20")
        if rows:
            data["kernel_top20"] = [{
                "name": r.get("name", "?"),
                "op_type": r.get("op_type", "N/A"),
                "duration": float(r.get("duration", 0) or 0),
                "wait_time": float(r.get("wait_time", 0) or 0),
                "block_dim": int(r.get("block_dim", 0) or 0),
                "step_id": r.get("step_id", "?"),
            } for r in rows]
        # 类型聚合
        rows = query_to_dicts(conn,
            "SELECT op_type, SUM(duration) as total_dur, COUNT(*) as cnt, AVG(duration) as avg_dur "
            "FROM kernel_detail GROUP BY op_type ORDER BY total_dur DESC")
        if rows:
            data["kernel_type_stats"] = [{"op_type": r.get("op_type", "N/A") or "N/A",
                                           "total_dur": round(float(r.get("total_dur", 0)), 2),
                                           "count": r.get("cnt", 0),
                                           "avg_dur": round(float(r.get("avg_dur", 0)), 2)} for r in rows]
        rows = query_to_dicts(conn, "SELECT COUNT(*) as cnt FROM kernel_detail")
        data["kernel_total_count"] = rows[0]["cnt"] if rows else 0
    else:
        data["tables_empty"].append("kernel_detail")

    # 2. slice 表 — 通信算子统计
    if "slice" in tables:
        # 通信类事件 (cat 包含 communication 或 name 以 HCom/hcom 开头)
        rows = query_to_dicts(conn,
            "SELECT name, COUNT(*) as cnt, SUM(duration) as total_dur, AVG(duration) as avg_dur "
            "FROM slice WHERE name LIKE 'HCom%' OR name LIKE 'hcom%' OR cat LIKE '%comm%' "
            "GROUP BY name ORDER BY total_dur DESC LIMIT 20")
        if rows:
            data["comm_ops"] = [{"name": r.get("name", "?"), "count": r.get("cnt", 0),
                                  "total_dur": round(float(r.get("total_dur", 0)), 2),
                                  "avg_dur": round(float(r.get("avg_dur", 0)), 2)} for r in rows]
        else:
            data["tables_empty"].append("slice (no comm ops)")
        # SDMA 统计
        rows = query_to_dicts(conn,
            "SELECT COUNT(*) as cnt, SUM(duration) as total_dur FROM slice WHERE name = 'SDMA'")
        if rows and rows[0]["cnt"] and rows[0]["cnt"] > 0:
            data["sdma_stats"] = {"count": rows[0]["cnt"], "total_dur": round(float(rows[0]["total_dur"]), 2)}
    else:
        data["tables_empty"].append("slice")

    # 3. counter 表 — AI Core 频率
    if "counter" in tables:
        rows = query_to_dicts(conn, "SELECT * FROM counter")
        if rows:
            data["counter_info"] = [{"name": r.get("name", "?"), "args": r.get("args", "")} for r in rows]

    conn.close()

    # 补充 CSV 数据 (step_trace_time, operator_details 等 — Insight DB 可能不含这些)
    csv_data = extract_text(ascend_output, rank_dir)
    for key in ["step_trace_time", "step_summary", "operator_top20", "operator_total_count",
                "api_by_level", "api_top20", "op_summary_top20", "profiler_info"]:
        if key in csv_data and key not in data:
            data[key] = csv_data[key]
    for t in csv_data.get("tables_found", []):
        if t not in data["tables_found"]:
            data["tables_found"].append(t)
    for t in csv_data.get("tables_empty", []):
        if t not in data["tables_empty"]:
            data["tables_empty"].append(t)

    return data


# ==================== Profiler DB 模式提取 ====================

def extract_profiler_db(db_path, ascend_output, rank_dir):
    data = {"format": "profiler_db", "tables_found": [], "tables_empty": []}
    conn = create_safe_connection(db_path)
    tables = [r[0] for r in conn.cursor().execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    data["tables_found"] = tables

    # 1. STEP_TIME 表
    if "STEP_TIME" in tables:
        rows = query_to_dicts(conn, "SELECT * FROM STEP_TIME ORDER BY id")
        if rows:
            data["step_trace_time"] = [{"Step": r.get("id", "?")} for r in rows]
            data["step_summary"] = {}
            for r in rows:
                sid = str(r.get("id", "?"))
                data["step_summary"][sid] = {
                    "stage": float(r.get("endNs", 0) or 0) - float(r.get("startNs", 0) or 0),
                }
    else:
        data["tables_empty"].append("STEP_TIME")

    # 2. COMPUTE_TASK_INFO + TASK — 计算算子 Top20
    if "COMPUTE_TASK_INFO" in tables and "TASK" in tables:
        rows = query_to_dicts(conn,
            "SELECT TASK.startNs, TASK.endNs, round(TASK.endNs - TASK.startNs) as Duration, "
            "TASK.taskType FROM TASK "
            "INNER JOIN COMPUTE_TASK_INFO ON TASK.globalTaskId == COMPUTE_TASK_INFO.globalTaskId "
            "ORDER BY Duration DESC LIMIT 20")
        if rows:
            data["kernel_top20"] = [{
                "name": r.get("name", "?"),
                "op_type": r.get("opType", "N/A"),
                "duration": float(r.get("Duration", 0) or 0) / 1000.0,  # ns→us
                "wait_time": 0,
                "block_dim": 0,
                "step_id": "?",
            } for r in rows]
    else:
        data["tables_empty"].append("COMPUTE_TASK_INFO")

    conn.close()

    # 补充 CSV 数据
    csv_data = extract_text(ascend_output, rank_dir)
    for key in ["step_trace_time", "step_summary", "operator_top20", "operator_total_count",
                "api_by_level", "api_top20", "op_summary_top20", "kernel_top20", "kernel_type_stats",
                "kernel_total_count", "profiler_info"]:
        if key in csv_data and (key not in data or not data.get(key)):
            data[key] = csv_data[key]
    for t in csv_data.get("tables_found", []):
        if t not in data["tables_found"]:
            data["tables_found"].append(t)

    return data


# ==================== 主入口 ====================

def extract_all(rank_dir):
    fmt = detect_format(rank_dir)
    if fmt["format"] == "insight_db":
        return extract_insight_db(fmt["db_path"], fmt["ascend_output"], rank_dir)
    elif fmt["format"] == "profiler_db":
        return extract_profiler_db(fmt["db_path"], fmt["ascend_output"], rank_dir)
    elif fmt["format"] == "text":
        return extract_text(fmt["ascend_output"], rank_dir)
    return {"format": "unknown", "error": "无法识别单卡数据格式", "rank_dir": rank_dir}


def main():
    parser = argparse.ArgumentParser(description="单卡数据提取器")
    parser.add_argument("--rank-dir", required=True, help="单卡 profiling 目录路径 (如 *_ascend_pt 目录)")
    parser.add_argument("--output", default=None, help="输出 JSON 文件路径")
    args = parser.parse_args()

    data = extract_all(args.rank_dir)
    data["rank_dir"] = args.rank_dir
    data["extract_time"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    if args.output:
        with open(args.output, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
        print(f"数据已提取到: {args.output}")
        pi = data.get("profiler_info", {})
        print(f"  格式: {data.get('format')}")
        print(f"  Rank ID: {pi.get('rank_id', '?')}")
        print(f"  CANN 版本: {pi.get('cann_version', '?')}")
        print(f"  Step 数: {len(data.get('step_summary', {}))}")
        print(f"  Kernel Top20: {len(data.get('kernel_top20', []))} 条")
        print(f"  算子 Top20: {len(data.get('operator_top20', []))} 条")
        print(f"  API Top20: {len(data.get('api_top20', []))} 条")
        print(f"  空表: {data.get('tables_empty', [])}")
    else:
        print(json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
