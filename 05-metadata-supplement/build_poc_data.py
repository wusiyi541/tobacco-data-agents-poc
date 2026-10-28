# -*- coding: utf-8 -*-
"""生成元数据补充 POC 的演示数据：catalog_5.json（虚构目录 5 条）+ meta_poc.db（SQLite 模拟库表）。

全部数据为脚本虚构，不含任何真实企业/系统数据。
运行：python build_poc_data.py
"""
import io, json, os, random, sqlite3, datetime, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

BASE = os.path.dirname(os.path.abspath(__file__))

# ---------- 1. 虚构目录 5 条（编码与 poc_server.py 的 TASK/LINEAGE/SVC MAP 对齐） ----------
PICKED = [
    {"catalog_code": "POC-DEMO/001", "catalog_name": "一户一图-零售户基础信息表",
     "description": "零售户基础属性信息表（虚构演示）", "org": "信息中心",
     "data_type": "系统库表", "share_type": "有条件共享", "applied_count": 35,
     "fields": ["零售户uuid", "许可证编号", "客户编码", "企业名称", "经营地址",
                "联系电话", "联系人", "入网时间", "许可证有效期止", "经营状态",
                "档位", "所属市局", "客户经理", "业态类型", "更新时间"]},
    {"catalog_code": "POC-DEMO/002", "catalog_name": "零售户终端销售信息表",
     "description": "终端销售明细基础层（虚构演示）", "org": "营销中心",
     "data_type": "系统库表", "share_type": "有条件共享", "applied_count": 12,
     "fields": ["销售流水号", "客户编码", "品规编码", "品规名称", "销售数量",
                "销售金额", "销售日期", "终端类型", "渠道类型", "市局编码",
                "是否20支终端", "上传时间"]},
    {"catalog_code": "POC-DEMO/003", "catalog_name": "案件基本信息表",
     "description": "专卖案件主表（虚构演示）", "org": "专卖监督管理处",
     "data_type": "系统库表", "share_type": "不予共享", "applied_count": 8,
     "fields": ["案件编号", "案件类型", "案发地市", "涉案品规", "涉案数量",
                "案值金额", "办案单位", "立案日期", "结案日期", "案件状态",
                "办案人", "备注"]},
    {"catalog_code": "POC-DEMO/004", "catalog_name": "真烟流入品规分析表",
     "description": "外流品规分析结果表（虚构演示）", "org": "专卖监督管理处",
     "data_type": "系统库表", "share_type": "有条件共享", "applied_count": 3,
     "fields": ["分析批次", "品规编码", "品规名称", "流入数量", "流出地",
                "流入地", "分析期间", "生成时间", "数据来源", "置信度"]},
    {"catalog_code": "POC-DEMO/005", "catalog_name": "综管平台指标结果表",
     "description": "综合管理指标加工结果（虚构演示）", "org": "信息中心",
     "data_type": "系统库表", "share_type": "无条件共享", "applied_count": 21,
     "fields": ["指标编码", "指标名称", "统计周期", "指标值", "计量单位",
                "同比增幅", "环比增幅", "责任部门", "更新时间", "数据来源"]},
]
# 字段截断标记（便于页面展示）
for x in PICKED:
    x["fields_truncated"] = len(x["fields"]) > 20

print("虚构目录 5 条:")
for x in PICKED:
    print(" ", x["catalog_code"], x["catalog_name"], "字段数=", len(x["fields"]))

with open(os.path.join(BASE, "catalog_5.json"), "w", encoding="utf-8") as f:
    json.dump(PICKED, f, ensure_ascii=False, indent=2)

# ---------- 2. 生成模拟库表（SQLite，行数据全部随机虚构） ----------
random.seed(2026)
TABLE_DB = os.path.join(BASE, "meta_poc.db")
if os.path.exists(TABLE_DB):
    os.remove(TABLE_DB)
conn = sqlite3.connect(TABLE_DB)

def CN(n=3):
    s = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严金魏陶姜"
    return "".join(random.choice(s) for _ in range(n))

def rand_dt():
    d = datetime.date(2025, 1, 1) + datetime.timedelta(days=random.randint(0, 640))
    return d.strftime("%Y-%m-%d %H:%M:%S")

def guess_type(name):
    if any(k in name for k in ("时间", "日期")): return "TEXT"
    if any(k in name for k in ("数量", "金额", "面积", "年龄")): return "REAL"
    return "TEXT"

def gen_val(name, i):
    n = str(name)
    if "uuid" in n or "标识" in n or "编码" in n or "编号" in n or "流水号" in n:
        return f"{abs(hash(n)) % 10000:04d}-{i:06d}"
    if "名称" in n or "姓名" in n or "联系人" in n:
        return CN() + ("烟酒店" if random.random() < .3 else "")
    if "电话" in n:
        return "1" + random.choice("3456789") + "".join(str(random.randint(0, 9)) for _ in range(9))
    if "时间" in n or "日期" in n: return rand_dt()
    if "是否" in n: return random.choice(["是", "否"])
    if "金额" in n: return round(random.uniform(50, 20000), 2)
    if "数量" in n or "次数" in n or "件数" in n: return random.randint(0, 500)
    if "地址" in n: return "某省" + random.choice("A B C D E F".split()) + "市" + CN(2) + "路" + str(random.randint(1, 200)) + "号"
    if "状态" in n: return random.choice(["正常", "停业", "注销", "已结案", "在办"])
    if "档位" in n: return random.choice(["一档", "二档", "三档", "四档", "五档"])
    if "增幅" in n: return round(random.uniform(-15, 30), 1)
    if "置信度" in n: return round(random.uniform(0.6, 0.99), 2)
    if "市局" in n or "地市" in n or "流出地" in n or "流入地" in n:
        return random.choice(["A市", "B市", "C市", "D市", "E市"])
    if "单位" in n: return random.choice(["万支", "箱", "元", "户", "件"])
    if "周期" in n or "期间" in n: return random.choice(["2026-01", "2026-02", "2026-03", "2026-Q1"])
    return CN(2) + str(i % 97)

for x in PICKED:
    tname = "t_" + x["catalog_code"].split("/")[1]
    flds = x["fields"]
    cols = ", ".join(f'"{f}" {guess_type(f)}' for f in flds)
    conn.execute(f'CREATE TABLE "{tname}" ({cols})')
    n = random.randint(80, 100)
    for i in range(1, n + 1):
        vals = [gen_val(f, i) for f in flds]
        conn.execute(f'INSERT INTO "{tname}" VALUES ({",".join("?" * len(flds))})', vals)
    print("  建表", tname, "行数", n)

conn.commit()
conn.close()
print("meta_poc.db 生成完毕（数据全部虚构）")
