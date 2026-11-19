"""Reconcile native Kernel sheets with DB tasks omitted by some msprof versions."""
from openpyxl import Workbook

from npu_source import db_kernels, is_main_db


def db_groups(source, by_type, step):
    groups, affected = {}, set()
    for row in db_kernels(source):
        if step is not None and row["Step Id"] != step:
            continue
        if by_type and row["Accelerator Core"] == "COMMUNICATION":
            continue
        kind = row["Type"]
        if kind in ("", "N/A"):
            continue
        key = kind, row["Accelerator Core"] if by_type else row["Input Shapes"] or "N/A"
        us = float(row["Duration(us)"])
        total, count, low, high = groups.get(key, (0, 0, us, us))
        groups[key] = total + us, count + 1, min(low, us), max(high, us)
        if row["comm_detail"]:
            affected.add(key)
    out = {}
    for key in affected:
        total, count, low, high = groups[key]
        if by_type:
            out[key] = [total, total/count, high, low, count]
        else:
            total = round(total, 2)
            out[key] = [total, round(total/count, 2), round(high, 2), round(low, 2), count]
    return out


def reconcile_kernel_sheets(wb, sources, steps):
    """Keep the upstream workbook intact. Replace only evidenced groups in derived outputs."""
    result = Workbook()
    result.remove(result.active)
    if len(sources) != 2 or not all(sources) or not any(is_main_db(p) for p in sources):
        return result
    for name in ("KernelCompare", "KernelTypeCompare"):
        if name not in wb.sheetnames:
            continue
        by_type = name == "KernelTypeCompare"
        sides = [db_groups(p, by_type, step) if is_main_db(p) else {} for p, step in zip(sources, steps)]
        affected = sides[0].keys() | sides[1].keys()
        if not affected:
            continue
        rows = iter(wb[name].values)
        prefix = []
        for row in rows:
            prefix.append(row)
            if row[:2] == ("Order Id", "Kernel Type" if by_type else "Kernel") and len(row) == 15:
                break
        else:
            continue
        sheet = result.create_sheet(name)
        for row in prefix:
            sheet.append(row)
        data = {(r[1], r[2]): list(r) for r in rows if r[1]}
        for key in sorted(affected):
            row = data.setdefault(key, [len(data)+1, *key] + [None]*12)
            for i, side in enumerate(sides):
                if key in side:
                    start = 3 + 5*i
                    row[start:start+5] = side[key]
            for i in (0, 1):
                base, comp = row[3+i] or 0, row[8+i] or 0
                row[13+i] = round(comp/base, 4) if base else "inf" if comp else 1
        for row in data.values():
            sheet.append(row)
    return result
