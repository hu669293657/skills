#!/usr/bin/env python3
"""
generate_card_compare_csv.py
双卡比对 CSV 输出件生成器 — 参考 msprof-analyze compare_tools 的 ExcelView/WorkSheetCreator 设计。

生成多 sheet 的 Excel (.xlsx) 比对结果，包含：
  1. OverallMetrics  — Step 时间拆解对比 (计算/通信/空闲)
  2. OperatorCompare — 算子级明细对比 (按名称匹配)
  3. KernelCompare   — Kernel 类型级对比 (按 Op Type 聚合)
  4. ApiCompare      — API 级对比 (按名称匹配)
  5. CommCompare     — 通信算子对比 (如有数据)

每个 sheet 的结构参考 compare:
  - 顶部合并行: Base Profiling 路径 (绿色) / Comparison Profiling 路径 (黄色)
  - 表头行: 绿色 (Base 侧) / 黄色 (Comparison 侧)
  - 数据行: Diff Ratio > 1 标红
"""
import argparse
import json
import os
from datetime import datetime
from collections import OrderedDict

try:
    from xlsxwriter import Workbook
    HAS_XLSXWRITER = True
except ImportError:
    HAS_XLSXWRITER = False

try:
    import csv as csv_module
    HAS_CSV = True
except ImportError:
    HAS_CSV = False


def safe_div(a, b):
    if abs(b) < 1e-9:
        return 0
    return round(a / b, 4)


def calc_diff(base_val, comp_val):
    """计算差值和比率，参考 compare 的 calculate_diff_ratio"""
    if not base_val and not comp_val:
        return [0, 1.0]
    if not base_val:
        return [round(comp_val - base_val, 2), float('inf')]
    return [round(comp_val - base_val, 2), round(comp_val / base_val, 4)]


def us_to_ms(us_val):
    if us_val is None:
        return 0
    return round(float(us_val) / 1000.0, 2)


# ==================== 数据构建 ====================

def build_overall_metrics(data_a, data_b):
    """构建 OverallMetrics sheet 数据"""
    sa = data_a.get("step_summary", {})
    sb = data_b.get("step_summary", {})
    steps = sorted(set(list(sa.keys()) + list(sb.keys())), key=lambda x: int(x) if x.isdigit() else x)

    avg_a_comp = sum(s.get("computing", 0) for s in sa.values()) / len(sa) if sa else 0
    avg_b_comp = sum(s.get("computing", 0) for s in sb.values()) / len(sb) if sb else 0
    avg_a_comm = sum(s.get("communication", 0) for s in sa.values()) / len(sa) if sa else 0
    avg_b_comm = sum(s.get("communication", 0) for s in sb.values()) / len(sb) if sb else 0
    avg_a_free = sum(s.get("free", 0) for s in sa.values()) / len(sa) if sa else 0
    avg_b_free = sum(s.get("free", 0) for s in sb.values()) / len(sb) if sb else 0
    avg_a_stage = sum(s.get("stage", 0) for s in sa.values()) / len(sa) if sa else 0
    avg_b_stage = sum(s.get("stage", 0) for s in sb.values()) / len(sb) if sb else 0
    avg_a_overlap = sum(s.get("overlapped", 0) for s in sa.values()) / len(sa) if sa else 0
    avg_b_overlap = sum(s.get("overlapped", 0) for s in sb.values()) / len(sb) if sb else 0

    metrics = [
        ("E2E Time (Stage)", avg_a_stage, avg_b_stage, len(sa), len(sb)),
        ("Computing Time", avg_a_comp, avg_b_comp, len(sa), len(sb)),
        ("Communication Time", avg_a_comm, avg_b_comm, len(sa), len(sb)),
        ("Overlapped Time", avg_a_overlap, avg_b_overlap, len(sa), len(sb)),
        ("Free Time", avg_a_free, avg_b_free, len(sa), len(sb)),
    ]

    headers = ["Index", "Duration(us)", "Ratio", "Number", "Duration(us)", "Ratio", "Number", "Diff(us)", "Diff Ratio"]
    rows = []
    for name, a_val, b_val, a_num, b_num in metrics:
        diff, ratio = calc_diff(a_val, b_val)
        a_ratio = safe_div(a_val, avg_a_stage) if avg_a_stage else 0
        b_ratio = safe_div(b_val, avg_b_stage) if avg_b_stage else 0
        rows.append([name, round(a_val, 2), a_ratio, a_num, round(b_val, 2), b_ratio, b_num, diff, ratio])

    return {"headers": headers, "rows": rows, "overhead": ["B1:D1", "E1:G1"],
            "base_path": data_a.get("rank_dir", "?"), "comp_path": data_b.get("rank_dir", "?")}


def build_operator_compare(data_a, data_b):
    """构建 OperatorCompare sheet 数据 — 按算子名称匹配"""
    ops_a = {op.get("name", "?"): op for op in data_a.get("operator_top20", [])}
    ops_b = {op.get("name", "?"): op for op in data_b.get("operator_top20", [])}
    all_ops = set(list(ops_a.keys()) + list(ops_b.keys()))

    headers = ["Order", "Op Name", "Input Shape", "Host Dur(us)", "Device Dur(us)",
               "Op Name", "Input Shape", "Host Dur(us)", "Device Dur(us)",
               "Diff Dur(us)", "Diff Ratio"]
    rows = []
    for op_name in all_ops:
        oa = ops_a.get(op_name, {})
        ob = ops_b.get(op_name, {})
        a_device = float(oa.get("device_dur", 0) or 0)
        b_device = float(ob.get("device_dur", 0) or 0)
        a_host = float(oa.get("host_dur", 0) or 0)
        b_host = float(ob.get("host_dur", 0) or 0)
        a_shape = (oa.get("input_shapes", "") or "")[:50]
        b_shape = (ob.get("input_shapes", "") or "")[:50]
        diff, ratio = calc_diff(a_device, b_device)
        rows.append([None, op_name[:40], a_shape, a_host, a_device,
                     op_name[:40], b_shape, b_host, b_device, diff, ratio])
    rows.sort(key=lambda x: x[9] if x[9] is not None else 0, reverse=True)
    for i, row in enumerate(rows):
        row[0] = i + 1

    return {"headers": headers, "rows": rows, "overhead": ["B1:E1", "F1:I1"],
            "base_path": data_a.get("rank_dir", "?"), "comp_path": data_b.get("rank_dir", "?")}


def build_kernel_compare(data_a, data_b):
    """构建 KernelCompare sheet 数据 — 按 Op Type 聚合"""
    types_a = {t["op_type"]: t for t in data_a.get("kernel_type_stats", [])}
    types_b = {t["op_type"]: t for t in data_b.get("kernel_type_stats", [])}
    all_types = set(list(types_a.keys()) + list(types_b.keys()))

    headers = ["Order", "Kernel Type", "Total Dur(us)", "Avg Dur(us)", "Max Dur(us)", "Min Dur(us)", "Count",
               "Total Dur(us)", "Avg Dur(us)", "Max Dur(us)", "Min Dur(us)", "Count",
               "Diff Total Ratio", "Diff Avg Ratio"]
    rows = []
    for t in all_types:
        ta = types_a.get(t, {})
        tb = types_b.get(t, {})
        a_total = float(ta.get("total_dur", 0) or 0)
        b_total = float(tb.get("total_dur", 0) or 0)
        a_count = int(ta.get("count", 0) or 0)
        b_count = int(tb.get("count", 0) or 0)
        a_avg = float(ta.get("avg_dur", 0) or 0)
        b_avg = float(tb.get("avg_dur", 0) or 0)
        diff_total_ratio = calc_diff(a_total, b_total)[1]
        diff_avg_ratio = calc_diff(a_avg, b_avg)[1]
        rows.append([None, t, a_total, a_avg, 0, 0, a_count,
                     b_total, b_avg, 0, 0, b_count, diff_total_ratio, diff_avg_ratio])
    rows.sort(key=lambda x: x[12] if x[12] is not None and x[12] != float('inf') else 0, reverse=True)
    for i, row in enumerate(rows):
        row[0] = i + 1

    return {"headers": headers, "rows": rows, "overhead": ["C1:G1", "H1:L1"],
            "base_path": data_a.get("rank_dir", "?"), "comp_path": data_b.get("rank_dir", "?")}


def build_api_compare(data_a, data_b):
    """构建 ApiCompare sheet 数据 — 按 API 名称匹配"""
    apis_a = {a.get("name", "?"): a for a in data_a.get("api_top20", [])}
    apis_b = {a.get("name", "?"): a for a in data_b.get("api_top20", [])}
    all_apis = set(list(apis_a.keys()) + list(apis_b.keys()))

    headers = ["Order", "API Name", "Total Time(us)", "Count", "Avg(us)",
               "Total Time(us)", "Count", "Avg(us)",
               "Diff Total Ratio", "Diff Count Ratio", "Diff Avg Ratio"]
    rows = []
    for name in all_apis:
        aa = apis_a.get(name, {})
        ab = apis_b.get(name, {})
        a_total = float(aa.get("total_time", 0) or 0)
        b_total = float(ab.get("total_time", 0) or 0)
        a_count = int(aa.get("count", 0) or 0)
        b_count = int(ab.get("count", 0) or 0)
        a_avg = float(aa.get("avg_time", 0) or 0)
        b_avg = float(ab.get("avg_time", 0) or 0)
        rows.append([None, name[:40], a_total, a_count, a_avg,
                     b_total, b_count, b_avg,
                     calc_diff(a_total, b_total)[1], calc_diff(a_count, b_count)[1], calc_diff(a_avg, b_avg)[1]])
    rows.sort(key=lambda x: x[8] if x[8] is not None and x[8] != float('inf') else 0, reverse=True)
    for i, row in enumerate(rows):
        row[0] = i + 1

    return {"headers": headers, "rows": rows, "overhead": ["C1:E1", "F1:H1"],
            "base_path": data_a.get("rank_dir", "?"), "comp_path": data_b.get("rank_dir", "?")}


def build_comm_compare(data_a, data_b):
    """构建 CommCompare sheet 数据 — 通信算子对比"""
    comm_a = {c.get("name", "?"): c for c in data_a.get("comm_ops", [])}
    comm_b = {c.get("name", "?"): c for c in data_b.get("comm_ops", [])}
    all_comm = set(list(comm_a.keys()) + list(comm_b.keys()))

    if not all_comm:
        return None

    headers = ["Order", "Comm Op Name", "Calls", "Total Dur(us)", "Avg Dur(us)",
               "Calls", "Total Dur(us)", "Avg Dur(us)", "Diff Dur(us)", "Diff Ratio"]
    rows = []
    for name in all_comm:
        ca = comm_a.get(name, {})
        cb = comm_b.get(name, {})
        a_total = float(ca.get("total_dur", 0) or 0)
        b_total = float(cb.get("total_dur", 0) or 0)
        a_count = int(ca.get("count", 0) or 0)
        b_count = int(cb.get("count", 0) or 0)
        a_avg = float(ca.get("avg_dur", 0) or 0)
        b_avg = float(cb.get("avg_dur", 0) or 0)
        diff, ratio = calc_diff(a_total, b_total)
        rows.append([None, name[:30], a_count, a_total, a_avg,
                     b_count, b_total, b_avg, diff, ratio])
    rows.sort(key=lambda x: x[8] if x[8] is not None else 0, reverse=True)
    for i, row in enumerate(rows):
        row[0] = i + 1

    return {"headers": headers, "rows": rows, "overhead": ["C1:E1", "F1:H1"],
            "base_path": data_a.get("rank_dir", "?"), "comp_path": data_b.get("rank_dir", "?")}


# ==================== Excel 输出 (xlsxwriter) ====================

def generate_excel(sheets_data, output_path):
    """生成多 sheet Excel 报告，参考 compare 的 WorkSheetCreator"""
    if not HAS_XLSXWRITER:
        print("xlsxwriter 未安装，将生成 CSV 格式替代")
        generate_csv(sheets_data, output_path)
        return

    GREEN = "#C6EFCE"
    YELLOW = "#FFEB9C"
    RED = "#FFC7CE"

    with Workbook(output_path) as workbook:
        for sheet_name, data in sheets_data.items():
            if not data or not data.get("rows"):
                continue
            ws = workbook.add_worksheet(sheet_name)

            # 格式定义
            fmt_header_green = workbook.add_format({"font_name": "Arial", "font_size": 11, "bold": True,
                                                     "bg_color": GREEN, "border": True, "align": "left", "valign": "vcenter"})
            fmt_header_yellow = workbook.add_format({"font_name": "Arial", "font_size": 11, "bold": True,
                                                     "bg_color": YELLOW, "border": True, "align": "left", "valign": "vcenter"})
            fmt_float = workbook.add_format({"font_name": "Arial", "font_size": 11, "num_format": "#,##0.00",
                                             "border": True, "align": "left", "valign": "vcenter"})
            fmt_int = workbook.add_format({"font_name": "Arial", "font_size": 11, "num_format": "#,##0",
                                           "border": True, "align": "left", "valign": "vcenter"})
            fmt_str = workbook.add_format({"font_name": "Arial", "font_size": 11, "border": True,
                                           "align": "left", "valign": "vcenter"})
            fmt_ratio = workbook.add_format({"font_name": "Arial", "font_size": 11, "num_format": "0.00%",
                                            "border": True, "align": "left", "valign": "vcenter"})
            fmt_red_ratio = workbook.add_format({"font_name": "Arial", "font_size": 11, "num_format": "0.00%",
                                                "border": True, "bg_color": RED, "align": "left", "valign": "vcenter"})

            headers = data["headers"]
            rows = data["rows"]
            overhead = data.get("overhead", [])
            col_ids = "ABCDEFGHIJKLMNOPQRSTUVWXYZ"

            # 顶部合并行: Base / Comparison 路径
            row_id = 0
            if len(overhead) >= 2:
                ws.merge_range(overhead[0], f"Base: {data.get('base_path', '?')}", fmt_header_green)
                ws.merge_range(overhead[1], f"Comparison: {data.get('comp_path', '?')}", fmt_header_yellow)
                row_id = 2

            # 表头行
            overhead_range = []
            if len(overhead) >= 2:
                overhead_range = [col_ids.index(overhead[0].split(":")[0][0]),
                                  col_ids.index(overhead[1].split(":")[0][0])]

            diff_ratio_indices = []
            for col_idx, header in enumerate(headers):
                fmt = fmt_header_yellow if (overhead_range and col_idx >= overhead_range[1]) else fmt_header_green
                ws.set_column(col_idx, col_idx, 18)
                ws.write(row_id, col_idx, header, fmt)
                if "Ratio" in header:
                    diff_ratio_indices.append(col_idx)
            row_id += 1

            # 数据行
            for row_data in rows:
                for col_idx, cell_data in enumerate(row_data):
                    # 处理 inf/nan 值，参考 compare 的 INF 标记
                    if isinstance(cell_data, float) and (cell_data == float('inf') or cell_data != cell_data):
                        cell_data = "INF"
                    if col_idx in diff_ratio_indices and cell_data is not None and not isinstance(cell_data, str):
                        if isinstance(cell_data, (int, float)) and cell_data > 1:
                            ws.write(row_id, col_idx, cell_data, fmt_red_ratio)
                        else:
                            ws.write(row_id, col_idx, cell_data if cell_data is not None else "", fmt_ratio)
                    elif isinstance(cell_data, str) and cell_data == "INF":
                        ws.write(row_id, col_idx, "INF", fmt_red_ratio)
                    elif isinstance(cell_data, float):
                        ws.write(row_id, col_idx, cell_data, fmt_float)
                    elif isinstance(cell_data, int):
                        ws.write(row_id, col_idx, cell_data, fmt_int)
                    else:
                        ws.write(row_id, col_idx, cell_data if cell_data is not None else "", fmt_str)
                row_id += 1

    print(f"Excel 比对结果已生成: {output_path}")


# ==================== CSV 回退输出 ====================

def generate_csv(sheets_data, output_path):
    """当 xlsxwriter 不可用时，生成多个 CSV 文件"""
    base_path = os.path.splitext(output_path)[0]
    for sheet_name, data in sheets_data.items():
        if not data or not data.get("rows"):
            continue
        csv_path = f"{base_path}_{sheet_name}.csv"
        with open(csv_path, "w", encoding="utf-8-sig", newline="") as f:
            writer = csv_module.writer(f)
            writer.writerow([f"Base: {data.get('base_path', '?')}"])
            writer.writerow([f"Comparison: {data.get('comp_path', '?')}"])
            writer.writerow(data["headers"])
            for row in data["rows"]:
                writer.writerow([("" if c is None else c) for c in row])
        print(f"  CSV sheet: {csv_path}")
    print(f"CSV 比对结果已生成到: {base_path}_*.csv")


# ==================== 主入口 ====================

def main():
    parser = argparse.ArgumentParser(description="双卡比对 CSV/Excel 生成器")
    parser.add_argument("--data-a", required=True, help="Rank A JSON 路径")
    parser.add_argument("--data-b", required=True, help="Rank B JSON 路径")
    parser.add_argument("--output", required=True, help="输出文件路径 (.xlsx 或 .csv)")
    args = parser.parse_args()

    with open(args.data_a, "r", encoding="utf-8") as f:
        data_a = json.load(f)
    with open(args.data_b, "r", encoding="utf-8") as f:
        data_b = json.load(f)

    # 构建所有 sheet 数据
    sheets = OrderedDict()
    sheets["OverallMetrics"] = build_overall_metrics(data_a, data_b)
    sheets["OperatorCompare"] = build_operator_compare(data_a, data_b)
    sheets["KernelCompare"] = build_kernel_compare(data_a, data_b)
    sheets["ApiCompare"] = build_api_compare(data_a, data_b)
    comm_data = build_comm_compare(data_a, data_b)
    if comm_data:
        sheets["CommCompare"] = comm_data

    # 输出
    if args.output.endswith(".xlsx") and HAS_XLSXWRITER:
        generate_excel(sheets, args.output)
    elif args.output.endswith(".csv"):
        generate_csv(sheets, args.output)
    else:
        # 默认尝试 Excel，回退 CSV
        if HAS_XLSXWRITER:
            generate_excel(sheets, args.output if args.output.endswith(".xlsx") else args.output + ".xlsx")
        else:
            generate_csv(sheets, args.output if args.output.endswith(".csv") else args.output + ".csv")


if __name__ == "__main__":
    main()
