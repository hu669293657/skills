#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""中文翻译器

将原版英文 compare 输出 xlsx 翻译为一个完整的中文 xlsx 文件，
每个 Sheet 保留为单独的标签页，表头和 Sheet 名称均翻译为中文。
同时输出独立 CSV 文件。

优化：接收已加载的 Workbook 对象，避免重复加载 xlsx 文件。
"""
import os
import csv as csv_mod
from .profile_identity import XLSX_HEADERS
from .xlsx import DATA_STYLE, HEADER_STYLE, STRIPED_STYLE, Sheet, write_workbook


# 英文 → 中文 表头映射
HEADER_MAP = {
    "Order Id": "序号",
    "Operator Name": "算子名称",
    "Input Shape": "输入Shape",
    "Input Type": "输入类型",
    "Kernel Details": "Kernel详情",
    "Allocated Details": "内存分配详情",
    "Device Duration(us)": "设备耗时(us)",
    "Diff Ratio": "差异比率",
    "Diff Duration(us)": "差异(us)",
    "Diff Size(KB)": "内存差异(KB)",
    "Size(KB)": "内存大小(KB)",
    "Top": "排名",
    "Base Device Duration(ms)": "基准-设备耗时(ms)",
    "Comparison Device Duration(ms)": "比对-设备耗时(ms)",
    "Base Operator Number": "基准-算子数量",
    "Comparison Operator Number": "比对-算子数量",
    "Diff Duration(ms)": "差异(ms)",
    "Base Allocated Duration(ms)": "基准-分配耗时(ms)",
    "Comparison Allocated Duration(ms)": "比对-分配耗时(ms)",
    "Base Allocated Memory(MB)": "基准-分配内存(MB)",
    "Comparison Allocated Memory(MB)": "比对-分配内存(MB)",
    "Diff Memory(MB)": "内存差异(MB)",
    "Communication OP Name": "通信算子名",
    "Task Name": "Task名",
    "Calls": "调用次数",
    "Total Duration(us)": "总耗时(us)",
    "Avg Duration(us)": "平均耗时(us)",
    "Max Duration(us)": "最大耗时(us)",
    "Min Duration(us)": "最小耗时(us)",
    "Module Class": "模块类",
    "Module Name": "模块名",
    "Device Self Time(ms)": "设备自身耗时(ms)",
    "Device Total Time(ms)": "设备总耗时(ms)",
    "Device Self Time Diff(ms)": "自身耗时差异(ms)",
    "Diff Total Ratio": "总耗时比率",
    "Device Total Time Diff(ms)": "总耗时差异(ms)",
    "Device Self Time(us)": "设备自身耗时(us)",
    "Device Total Time(us)": "设备总耗时(us)",
    "Device Self Time Diff(us)": "自身耗时差异(us)",
    "Device Total Time Diff(us)": "总耗时差异(us)",
    "Total Time Ratio": "总耗时比率",
    "Number": "数量",
    "Module Level": "模块层级",
    "Base Call Stack": "基准调用栈",
    "Comparison Call Stack": "比对调用栈",
    "Index": "指标(Index)",
    "Duration(ms)": "耗时(ms)",
    "Duration Ratio": "耗时占比",
    "api name": "API名称",
    "Total Duration(ms)": "总耗时(ms)",
    "Avg Duration(ms)": "平均耗时(ms)",
    "Self Time(ms)": "Self耗时(ms)",
    "Diff Self Ratio": "Self比率",
    "Diff Avg Ratio": "均耗比率",
    "Diff Calls Ratio": "次数比率",
    "Kernel": "Kernel名称",
    "Kernel Type": "Kernel类型",
    "Core Type": "核类型",
}

# Sheet 名称中英文映射
SHEET_NAME_MAP = {
    "OverallMetrics": "总体性能比对",
    "OperatorCompareStatistic": "算子统计比对",
    "OperatorCompare": "算子明细比对",
    "ModuleCompareStatistic": "模块统计比对",
    "ModuleCompare": "模块明细比对",
    "CommunicationCompare": "通信比对",
    "MemoryCompareStatistic": "内存统计比对",
    "MemoryCompare": "内存明细比对",
    "KernelCompare": "Kernel比对",
    "KernelTypeCompare": "Kernel类型比对",
    "ApiCompare": "API比对",
}

# 已知英文字段名列表
KNOWN_HEADERS = [
    'Order Id', 'Operator Name', 'Index', 'Top', 'Communication OP Name',
    'Module Class', 'api name', 'Kernel', 'Kernel Type',
]

def translate_header(header: str) -> str:
    """翻译单个表头"""
    header = header.strip() if header else ''
    return HEADER_MAP.get(header, header)


def _get_chinese_sheet_name(sheet_name: str) -> str:
    """获取中文 Sheet 名称"""
    for en_key, zh_val in SHEET_NAME_MAP.items():
        if en_key.lower() in sheet_name.lower():
            return zh_val
    return sheet_name


def _is_header_row(row_data: list) -> bool:
    """判断是否为表头行"""
    row_str = ' '.join(str(v) for v in row_data)
    return any(h in row_str for h in KNOWN_HEADERS)


def _sheet_title(name, workbook):
    title = name[:31]
    used = {key.casefold() for key in workbook}
    suffix = 1
    while title.casefold() in used:
        tail = str(suffix)
        title = name[:31 - len(tail)] + tail
        suffix += 1
    return title


def translate_to_chinese(wb_src, output_dir: str, host_kernel=None, kernel_wb=None, comparison=None):
    """Translate worksheet labels, preserving the workbook and CSV report structure."""
    csv_dir = os.path.join(output_dir, 'chinese_csv')
    os.makedirs(csv_dir, exist_ok=True)
    workbook = {}
    total_rows = 0

    for sheet_name, ws_src in wb_src.items():
        if kernel_wb and sheet_name in kernel_wb:
            ws_src = kernel_wb[sheet_name]
        chinese_name = _sheet_title(_get_chinese_sheet_name(sheet_name), workbook)
        sheet = workbook[chinese_name] = Sheet()
        header_row = None
        data_count = 0
        csv_path = os.path.join(csv_dir, f"{chinese_name}.csv")

        with open(csv_path, 'w', newline='', encoding='utf-8-sig') as csv_file:
            writer = csv_mod.writer(csv_file)
            for index, row in enumerate(ws_src.iter_rows(), 1):
                data = ['' if value is None else str(value) for value in row]
                if comparison and index <= 2:
                    for col, value in enumerate(data):
                        for role, prefix in XLSX_HEADERS.items():
                            if value.startswith(prefix):
                                data[col] = comparison[role]["short_label"]
                                break

                if not any(data):
                    sheet.rows.append(())
                    continue

                if header_row is None and _is_header_row(data):
                    data = [translate_header(value) for value in data]
                    header_row = index
                    style = HEADER_STYLE
                else:
                    style = STRIPED_STYLE if header_row is not None and data_count % 2 else DATA_STYLE
                    data_count += 1

                sheet.rows.append(data)
                sheet.row_styles[index] = style
                for col, value in enumerate(data, 1):
                    width = min(len(value) + 4, 50)
                    sheet.widths[col] = max(sheet.widths.get(col, 0), width)
                writer.writerow(data)
                total_rows += 1

        sheet.freeze = (header_row or 1, 0)
        print(f"  Sheet: {chinese_name} ({data_count} 行)")

    if host_kernel:
        from .host_kernel_report import spreadsheet_tables

        for title, rows in spreadsheet_tables(host_kernel, comparison):
            title = _sheet_title(title, workbook)
            widths = {
                col: min(52, max(15, max(len(str(row[col - 1] or "")) for row in rows) + 3))
                for col in range(1, len(rows[0]) + 1)
            }
            workbook[title] = Sheet(rows, {1: HEADER_STYLE}, widths, freeze=(1, 1), auto_filter=True)
            with open(os.path.join(csv_dir, title + ".csv"), "w", newline="", encoding="utf-8-sig") as csv_file:
                csv_mod.writer(csv_file).writerows(rows)
            total_rows += len(rows)

    output_path = os.path.join(output_dir, "compare_chinese_result.xlsx")
    write_workbook(output_path, workbook)

    print(f"\n  [完成] 中文 xlsx 已生成: {output_path}")
    print(f"  共 {len(workbook)} 个 Sheet, {total_rows} 行数据")
    print(f"  CSV 已同步导出到: {csv_dir}/")
    return output_path
