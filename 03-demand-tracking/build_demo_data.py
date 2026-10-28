# -*- coding: utf-8 -*-
"""需求状态追踪 POC · 演示数据构建（全虚构）。

生成 tracking_poc.db（flow_rows 单表，21 列 c0~c20，列含义见 mock_api.COLS）。
全部单据、人员、单位均为脚本虚构。

运行：python build_demo_data.py
"""
import datetime
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "tracking_poc.db")

# 演示目录（与 01/05 的虚构目录词表对齐）
CATALOG = [
    ("POC-DEMO/001", "零售客户基本信息表"), ("POC-DEMO/002", "零售户终端销售信息表"),
    ("POC-DEMO/003", "案件基本信息表"), ("POC-DEMO/004", "客户订单月报信息表"),
    ("POC-DEMO/005", "物流配送交接数据明细表"), ("POC-DEMO/006", "零售许可证主表"),
    ("POC-DEMO/007", "卷烟商品信息拉链表"), ("POC-DEMO/008", "综管平台指标结果表"),
]
UNITS = ["A市烟草专卖局（公司）", "B市烟草专卖局（公司）", "C市烟草专卖局（公司）"]
DEPTS = ["信息中心", "营销中心", "安全管理科", "综合管理组", "专卖监督管理处"]
NAMES = ["张明", "李华", "王芳", "刘涛", "陈静", "杨帆", "赵磊", "黄敏", "周颖", "吴凯"]
NUM_P = ["138%08d", "139%08d", "136%08d"]

random.seed(2026)


def ts(d, h=None, m=None):
    return d.strftime("%Y%m%d") + " %02d:%02d:00" % (h or random.randint(9, 17), m or random.randint(0, 59))


def dt_of(s):
    return datetime.datetime.strptime(s, "%Y%m%d %H:%M:%S")


def main():
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    cur = conn.cursor()
    cur.execute("CREATE TABLE flow_rows (%s)" % ", ".join(f"c{i}" for i in range(21)))

    base = datetime.date(2026, 9, 1)
    row_id = 0
    for req in range(1, 41):  # 40 张虚构用数单
        d0 = base + datetime.timedelta(days=random.randint(0, 50))
        order_no = d0.strftime("%Y%m%d") + "%09d" % req
        unit = random.choice(UNITS)
        applicant = random.choice(NAMES)
        dept = random.choice(DEPTS)
        t0 = ts(d0)
        # 单头行（c1/c2/c3 为空）
        cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                    (order_no, "", "", "", applicant, dept, unit, t0, "信息中心",
                     "提交订单", "", t0, applicant, "", "", "", "", "", "", "", ""))
        row_id += 1
        n_items = random.randint(1, 3)
        stage = random.choices(
            ["审核中", "需求拒绝", "待实施", "实施完成(待签收)", "已签收(待评论)"],
            weights=[35, 10, 25, 20, 10])[0]
        for _ in range(n_items):
            code, cname = random.choice(CATALOG)
            item_id = "%032x" % random.getrandbits(128)
            t = dt_of(t0)
            # ① 派发用数部门审核
            t += datetime.timedelta(minutes=random.randint(1, 5))
            cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                        (order_no, item_id, code, cname, applicant, dept, unit, t0,
                         unit, "派发用数部门审核", "", t.strftime("%Y%m%d %H:%M:%S"),
                         random.choice(NAMES), "", "", "", stage, "", "", "", ""))
            # ② 用数部门审核通过
            t += datetime.timedelta(hours=random.randint(1, 20))
            cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                        (order_no, item_id, code, cname, applicant, dept, unit, t0,
                         unit, "用数部门审核", "用数部门审核", t.strftime("%Y%m%d %H:%M:%S"),
                         random.choice(NAMES), "通过",
                         (t - datetime.timedelta(minutes=random.randint(1, 30))).strftime("%Y%m%d %H:%M:%S"),
                         t.strftime("%Y%m%d %H:%M:%S"), stage, "", "", "", ""))
            if stage == "需求拒绝":
                t += datetime.timedelta(hours=random.randint(1, 24))
                cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                            (order_no, item_id, code, cname, applicant, dept, unit, t0,
                             unit, "主管部门审核", "主管部门审核", t.strftime("%Y%m%d %H:%M:%S"),
                             random.choice(NAMES), "拒绝",
                             (t - datetime.timedelta(minutes=10)).strftime("%Y%m%d %H:%M:%S"),
                             t.strftime("%Y%m%d %H:%M:%S"), "需求拒绝", "", "", "", ""))
                continue
            # ③ 主管部门审核通过
            t += datetime.timedelta(hours=random.randint(2, 30))
            cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                        (order_no, item_id, code, cname, applicant, dept, unit, t0,
                         unit, "主管部门审核", "主管部门审核", t.strftime("%Y%m%d %H:%M:%S"),
                         random.choice(NAMES), "通过",
                         (t - datetime.timedelta(minutes=random.randint(5, 60))).strftime("%Y%m%d %H:%M:%S"),
                         t.strftime("%Y%m%d %H:%M:%S"), stage, "", "", "", ""))
            if stage in ("待实施",):
                continue
            # ④ 共享实施
            t += datetime.timedelta(days=random.randint(1, 3))
            impl_no = "IM%s%04d" % (d0.strftime("%Y%m"), req)
            t_impl = t.strftime("%Y%m%d %H:%M:%S")
            cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                        (order_no, item_id, code, cname, applicant, dept, unit, t0,
                         unit, "共享实施", "共享实施", t_impl,
                         "信息中心", "通过", t_impl, t_impl, stage,
                         impl_no, "已完成" if stage != "实施完成(待签收)" else "实施中",
                         t_impl if stage == "已签收(待评论)" else "", ""))
            if stage == "已签收(待评论)":
                t += datetime.timedelta(days=random.randint(0, 2))
                cur.execute("INSERT INTO flow_rows VALUES (%s)" % ",".join("?" * 21),
                            (order_no, item_id, code, cname, applicant, dept, unit, t0,
                             unit, "数据签收", "数据签收", t.strftime("%Y%m%d %H:%M:%S"),
                             applicant, "已签收", "", t.strftime("%Y%m%d %H:%M:%S"), stage,
                             impl_no, "已完成", t.strftime("%Y%m%d %H:%M:%S"),
                             t.strftime("%Y%m%d %H:%M:%S")))

    conn.commit()
    n = cur.execute("SELECT COUNT(*) FROM flow_rows").fetchone()[0]
    conn.close()
    print(f"tracking_poc.db 生成完毕：{n} 行流转记录（全虚构）")


if __name__ == "__main__":
    main()
