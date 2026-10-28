# -*- coding: utf-8 -*-
"""
需求状态追踪 POC · 数据导入
把《需求单流转清单_20261020_0948(1).xlsx》导入 SQLite（tracking_poc.db）。

零改数原则：xlsx 原样入行，仅做：
  · 表头字符串清洗（去 \xa0 等不可见空白）；
  · 全部字段转 TEXT 落库，时间字段保持「YYYYMMDD HH:MM:SS」原样，
    计算时再解析（不回写）。

运行：
    python import_data.py
"""
import os
import sqlite3
import sys

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
XLSX = os.path.join(HERE, "..", "需求单流转清单_20261020_0948(1).xlsx")
DB = os.path.join(HERE, "tracking_poc.db")


def clean(v):
    if v is None:
        return ""
    s = str(v).replace("\xa0", " ").strip()
    return s


def main():
    wb = openpyxl.load_workbook(XLSX, data_only=True)
    ws = wb.worksheets[0]
    rows = list(ws.iter_rows(values_only=True))
    hdr = [clean(h) for h in rows[0]]
    data = [[clean(c) for c in r] for r in rows[1:] if any(clean(c) for c in r)]

    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    cols = ", ".join('c%d TEXT' % i for i in range(len(hdr)))
    conn.execute("CREATE TABLE flow_rows (%s)" % cols)
    conn.executemany(
        "INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * len(hdr)), data)
    conn.commit()

    n_req = conn.execute(
        "SELECT COUNT(DISTINCT c0) FROM flow_rows").fetchone()[0]
    print("导入完成: %d 行, %d 张需求单, %d 列" % (len(data), n_req, len(hdr)))
    print("字段:", " | ".join(hdr))
    conn.close()


if __name__ == "__main__":
    if not os.path.exists(XLSX):
        sys.exit("找不到源 xlsx: %s" % XLSX)
    main()
