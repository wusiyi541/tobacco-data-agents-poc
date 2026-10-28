# -*- coding: utf-8 -*-
"""
数据质检 POC · 数据底座构建脚本
=================================================
把四份原始素材清洗整合为统一的 SQLite 底座，供 Mock API 读取。

输入（相对脚本上级目录）：
  1. dwd_dqc_quality_rule_样例数据.xlsx          质检规则定义 1000 条
  2. dwd_dqc_quality_rule_exec_样例数据.xlsx     规则执行记录 1000 条
  3. dwd_dqc_quality_problem_list_样例数据.xlsx  质检问题清单 1000 条
  4. YC用数需求和数据质量问题台账.xlsx            真实台账（问题工单 290 / 用数需求 254）

输出：dq_poc.db

设计要点：
  · 「数由代码算」——所有派生字段（校验字段、阈值、执行时长、质量维度中文化、
    数据域标签）在此处一次算清，模型侧不得再自行计算。
  · 「口径由人定」——数据域标签与维度映射使用显式规则表（见 DOMAIN_RULES /
    DIMENSION_MAP），可人工校对修改后重跑。
"""
import os
import re
import json
import sqlite3
import datetime as dt
import zipfile
import xml.etree.ElementTree as ET

import openpyxl

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.dirname(HERE)          # 数据质检智能体/
DB = os.path.join(HERE, "dq_poc.db")

F_RULE = "dwd_dqc_quality_rule_样例数据.xlsx"
F_EXEC = "dwd_dqc_quality_rule_exec_样例数据.xlsx"
F_PROB = "dwd_dqc_quality_problem_list_样例数据.xlsx"
F_LEDGER = "YC用数需求和数据质量问题台账.xlsx"

EXCEL_EPOCH = dt.datetime(1899, 12, 30)

# ---------------------------------------------------------------- 口径定义
# 质量维度：技术口径(英文) ↔ 业务口径(中文) ↔ 台账分类
# 【1008 口径对齐】技术规则库用的是 STABILITY，而业务台账与客户《数据质量检测报告》
# 模板用的都是「准确性」。按用户决定「以模板为准」，统一呈现为「准确性」（稳定性→准确性）。
# 技术口径原值仍保留在 dimension（EN）列，随时可回溯。
DIMENSION_MAP = {
    "COMPLETENESS": ("完整性问题", "完整性", "数据缺失或为空"),
    "CONSISTENT":   ("一致性问题", "一致性", "多源数据不一致"),
    "UNIQUENESS":   ("唯一性问题", "唯一性", "数据重复或主键重复"),
    "TIMELINESS":   ("及时性问题", "及时性", "数据未按约定时间更新"),
    "EFFECTIVE":    ("有效性问题", "有效性", "取值不符合业务规则或格式"),
    "STABILITY":    ("准确性问题", "准确性", "数值与业务真实值不符"),
}
# 台账中出现但技术规则库暂无对应分类的维度
DIMENSION_EXTRA = {
    "规范性问题": ("STANDARD", "规范性", "命名、注释、格式不符合规范"),
}
# 维度口径对照（写进 dimension_map 的 source 字段，供业务方核对）
DIM_ALIAS_NOTE = {"STABILITY": "技术规则库原名 STABILITY(稳定性)，按客户报告模板口径呈现为准确性"}

# 数据域标签规则（顺序敏感：先匹配者胜）
DOMAIN_RULES = [
    ("一号工程", r"一号工程|下行|HANA|hana|工业销存|在途|_di_|_d\b"),
    ("物流",     r"物流|仓储|wl_"),
    ("专卖",     r"专卖|许可证|案件|真烟|涉案|外流|zm_|_case_"),
    ("营销",     r"营销|零售|客户|销售|品规|订单|品牌|投放|价格|档位|拜访|"
                 r"支付|终端|进销存|购销|调存|易灵通|订货|市场|产品|cst|mkt|ord|sal"),
    ("综管",     r"综管|组织|部门|机构|待办|登录|登陆|日志|菜单|审计|人员|人事|org"),
]


def tag_domain(desc, table_name):
    """给表打数据域标签；desc 优先，表名兜底。"""
    for domain, pat in DOMAIN_RULES:
        if desc and re.search(pat, str(desc)):
            return domain
    for domain, pat in DOMAIN_RULES:
        if table_name and re.search(pat, str(table_name)):
            return domain
    return "未分类"


# ---------------------------------------------------------------- 工具函数
def xl_date(v):
    """Excel 日期序列号 / 字符串 → YYYY-MM-DD。"""
    if v is None or v == "":
        return None
    if isinstance(v, (dt.datetime, dt.date)):
        return (v.strftime("%Y-%m-%d") if isinstance(v, dt.date)
                else v.strftime("%Y-%m-%d"))
    s = str(v).strip()
    if not s:
        return None
    if re.fullmatch(r"\d+(\.\d+)?", s):
        n = float(s)
        if 20000 < n < 60000:            # Excel 序列号区间
            return (EXCEL_EPOCH + dt.timedelta(days=n)).strftime("%Y-%m-%d")
    m = re.match(r"(\d{4})[-/](\d{1,2})[-/](\d{1,2})", s)
    if m:
        return "%04d-%02d-%02d" % tuple(int(x) for x in m.groups())
    return s


def to_sec(t):
    """'2026-09-22 15:34:44' → epoch 秒。"""
    if not t:
        return None
    s = str(t).strip()
    for f in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return dt.datetime.strptime(s, f).timestamp()
        except ValueError:
            continue
    return None


def parse_props(s):
    """从 rule_properties JSON 中取校验字段。"""
    if not s:
        return None
    try:
        arr = json.loads(s)
        for it in arr:
            if it.get("propertyName", {}).get("value") == "validateField":
                return it.get("propertyValue", {}).get("value")
    except Exception:
        pass
    return None


def parse_cond(s):
    """从 validate_condition JSON 中取 (指标, 可读阈值表达式)。

    实测枚举：operator ∈ {SMALLER, SMALLER_OR_EQUAL, LARGE_OR_EQUAL, EQUAL, GREATER}
              metric   ∈ {ERROR_RATE, NORMAL_RATE, ERROR_NUMBER}
    输出形如 ('ERROR_RATE', 'ERROR_RATE < 10')。
    """
    if not s:
        return None, None
    OP = {"SMALLER": "<", "SMALLER_OR_EQUAL": "<=", "LARGE_OR_EQUAL": ">=",
          "GREATER": ">", "GREATER_OR_EQUAL": ">=", "LARGE": ">", "EQUAL": "="}
    try:
        d = json.loads(s)
        for sub in d.get("subConditions", []):
            if sub.get("type") == "EXPRESSION":
                metric = (sub.get("metric") or {}).get("value")
                op = OP.get(sub.get("operator"), sub.get("operator"))
                val = sub.get("value")
                if metric and op is not None and val is not None:
                    return metric, "%s %s %s" % (metric, op, val)
                return metric, None
    except Exception:
        pass
    return None, None


def parse_types(s):
    """从 last_problem_types JSON 取维度英文名列表。"""
    if not s:
        return []
    try:
        arr = json.loads(s)
        return [x.get("value") for x in arr if x.get("value")]
    except Exception:
        return []


def load_sample(fn):
    wb = openpyxl.load_workbook(os.path.join(SRC, fn), read_only=True, data_only=True)
    ws = wb.worksheets[0]
    it = ws.iter_rows(values_only=True)
    hdr_en = [str(h).strip() if h is not None else "" for h in next(it)]
    hdr_cn = [str(h).strip() if h is not None else "" for h in next(it)]
    rows = []
    for r in it:
        r = list(r) + [None] * (len(hdr_en) - len(r))
        rows.append(r[:len(hdr_en)])
    wb.close()
    return hdr_en, hdr_cn, rows


def load_ledger(sheet_index):
    """绕过 openpyxl 对台账 styles 的兼容问题，直接解 XML。"""
    P = os.path.join(SRC, F_LEDGER)
    NS = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    z = zipfile.ZipFile(P)
    shared = ["".join(t.text or "" for t in si.iter(NS + "t"))
              for si in ET.fromstring(z.read("xl/sharedStrings.xml")).iter(NS + "si")]

    def cidx(ref):
        m = re.match(r"([A-Z]+)", ref or "")
        n = 0
        for ch in (m.group(1) if m else ""):
            n = n * 26 + (ord(ch) - 64)
        return n - 1

    out = []
    for row in ET.fromstring(z.read("xl/worksheets/sheet%d.xml" % sheet_index)).iter(NS + "row"):
        cells = {}
        for c in row.iter(NS + "c"):
            ci = cidx(c.get("r"))
            t, v, isv = c.get("t"), c.find(NS + "v"), c.find(NS + "is")
            if t == "s" and v is not None:
                val = shared[int(v.text)]
            elif isv is not None:
                val = "".join(x.text or "" for x in isv.iter(NS + "t"))
            else:
                val = v.text if v is not None else ""
            cells[ci] = val
        if cells:
            out.append([cells.get(k, "") for k in range(max(cells) + 1)])
        else:
            out.append([])
    z.close()
    return out


# ---------------------------------------------------------------- 建库
SCHEMA = """
DROP TABLE IF EXISTS quality_rule;
CREATE TABLE quality_rule (
  rule_id TEXT PRIMARY KEY, rule_name TEXT, rule_desc TEXT,
  strength TEXT, strength_cn TEXT, dimension TEXT, dimension_cn TEXT,
  template_id TEXT, template_name TEXT, template_type TEXT,
  table_id TEXT, table_name TEXT, table_description TEXT, data_domain TEXT,
  biz_unit_name TEXT, project_name TEXT,
  validate_object_type TEXT, validate_object_name TEXT, validate_field TEXT,
  threshold_metric TEXT, threshold_expr TEXT,
  rule_status TEXT, creator_name TEXT, gmt_create TEXT, gmt_modified TEXT
);
DROP TABLE IF EXISTS rule_exec;
CREATE TABLE rule_exec (
  exec_id INTEGER PRIMARY KEY AUTOINCREMENT,
  rule_id TEXT, rule_name TEXT, biz_date TEXT, ds TEXT,
  task_status TEXT, is_pass TEXT, start_time TEXT, end_time TEXT,
  duration_sec INTEGER, dimension TEXT, dimension_cn TEXT,
  table_name TEXT, table_description TEXT, data_domain TEXT
);
DROP TABLE IF EXISTS problem;
CREATE TABLE problem (
  problem_object_id TEXT PRIMARY KEY,
  validate_object_type TEXT, problem_object_name TEXT, validate_object_name TEXT,
  table_id TEXT, table_name TEXT, table_desc TEXT, data_domain TEXT,
  rule_id TEXT, rule_name TEXT, rule_strength TEXT,
  dimension TEXT, dimension_cn TEXT,
  problem_status TEXT, problem_desc TEXT,
  submit_time TEXT, submitter TEXT, owner_names TEXT,
  validate_scope TEXT, validate_result TEXT, ds TEXT
);
DROP TABLE IF EXISTS problem_ticket;
CREATE TABLE problem_ticket (
  ticket_no INTEGER PRIMARY KEY,
  domain_l1 TEXT, domain_l2 TEXT, table_cn TEXT, table_en TEXT,
  problem_desc TEXT, submitter TEXT, submit_date TEXT,
  status TEXT, owner TEXT, category TEXT, dimension TEXT,
  cause_solution TEXT, plan_date TEXT, actual_date TEXT, lead_time_days INTEGER,
  remark TEXT, priority TEXT
);
DROP TABLE IF EXISTS requirement;
CREATE TABLE requirement (
  req_no INTEGER PRIMARY KEY, req_type TEXT, domain TEXT, req_desc TEXT,
  biz_caliber TEXT, tech_caliber TEXT, priority TEXT, expect_date TEXT,
  submitter TEXT, submit_date TEXT, communicated TEXT, status TEXT,
  handler TEXT, todo TEXT, conclusion TEXT, plan_date TEXT,
  actual_date TEXT, remark TEXT, lead_time_days INTEGER
);
DROP TABLE IF EXISTS dimension_map;
CREATE TABLE dimension_map (
  en TEXT PRIMARY KEY, cn TEXT, short_cn TEXT, definition TEXT, source TEXT
);
CREATE TABLE IF NOT EXISTS meta (
  key TEXT PRIMARY KEY, value TEXT
);
DROP TABLE IF EXISTS period_dim;
CREATE TABLE period_dim (
  period_id INTEGER PRIMARY KEY,   -- 202608 / 202609
  period_label TEXT,               -- 2026-08
  period_cn TEXT,                  -- 2026 年 8 月
  date_start TEXT, date_end TEXT,  -- 该期内 ds 的最小/最大
  is_current INTEGER               -- 1 = 本期，0 = 上一期
);
"""


def build():
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.executescript(SCHEMA)
    c = conn.cursor()
    stat = {}

    # ---- 1. 规则表 ----
    h, hcn, rows = load_sample(F_RULE)
    def g(row, name):
        return row[h.index(name)]
    recs = []
    for r in rows:
        rid = g(r, "rule_id")
        if rid is None or str(rid).strip() == "":
            continue
        dim = (str(g(r, "rule_catalogs") or "").strip() or None)
        strength = str(g(r, "strength") or "").strip()
        tname = str(g(r, "table_name") or "").strip()
        tdesc = str(g(r, "table_description") or "").strip()
        metric, expr = parse_cond(g(r, "validate_condition"))
        recs.append((
            str(rid).strip(), str(g(r, "rule_name") or ""), str(g(r, "rule_desc") or ""),
            strength, {"STRONG": "强规则", "WEAK": "弱规则"}.get(strength, strength),
            dim, (DIMENSION_MAP.get(dim) or (None,))[0],
            str(g(r, "template_id") or ""), str(g(r, "template_name") or ""),
            str(g(r, "template_type") or ""),
            str(g(r, "table_id") or ""), tname, tdesc, tag_domain(tdesc, tname),
            str(g(r, "biz_unit_name") or ""), str(g(r, "project_name") or ""),
            str(g(r, "validate_object_type") or ""), str(g(r, "validate_object_name") or ""),
            parse_props(g(r, "rule_properties")) or str(g(r, "validate_object_name") or ""),
            metric, expr,
            str(g(r, "rule_status") or ""), str(g(r, "creator_name") or ""),
            str(g(r, "gmt_create") or ""), str(g(r, "gmt_modified") or ""),
        ))
    c.executemany("INSERT INTO quality_rule VALUES (%s)" % ",".join("?" * 25), recs)
    stat["quality_rule"] = len(recs)

    # 规则维度速查：rule_id -> (dim, table_name, table_desc, domain)
    rule_dim = {x[0]: (x[5], x[11], x[12], x[13]) for x in recs}

    # ---- 2. 执行表 ----
    h2, _, rows2 = load_sample(F_EXEC)
    recs2 = []
    for r in rows2:
        rid = r[h2.index("rule_id")]
        if rid is None or str(rid).strip() == "":
            continue
        st = str(r[h2.index("start_time")] or "")
        et = str(r[h2.index("end_time")] or "")
        s1, s2 = to_sec(st), to_sec(et)
        dur = int(s2 - s1) if s1 and s2 and s2 >= s1 else None
        dim, tn, td, dom = rule_dim.get(str(rid).strip(), (None, None, None, None))
        recs2.append((
            str(rid).strip(), str(r[h2.index("rule_name")] or ""),
            str(xl_date(r[h2.index("biz_date")]) or ""), str(r[h2.index("ds")] or ""),
            str(r[h2.index("rule_task_status")] or ""),
            str(r[h2.index("is_validate_result")] or ""),
            st, et, dur, dim, (DIMENSION_MAP.get(dim) or (None,))[0], tn, td, dom,
        ))
    c.executemany("INSERT INTO rule_exec VALUES (NULL,%s)" % ",".join(["?"] * 14), recs2)
    stat["rule_exec"] = len(recs2)

    # ---- 3. 问题清单 ----
    h3, _, rows3 = load_sample(F_PROB)
    recs3 = []
    for r in rows3:
        pid = r[h3.index("problem_object_id")]
        if pid is None or str(pid).strip() == "":
            continue
        dims = parse_types(r[h3.index("last_problem_types")])
        dim = dims[0] if dims else None
        tname = str(r[h3.index("table_name")] or "").strip()
        tdesc = str(r[h3.index("table_desc")] or "").strip()
        recs3.append((
            str(pid).strip(),
            str(r[h3.index("validate_object_type")] or ""),
            str(r[h3.index("problem_object_name")] or ""),
            str(r[h3.index("validate_object_name")] or ""),
            str(r[h3.index("table_id")] or ""), tname, tdesc, tag_domain(tdesc, tname),
            str(r[h3.index("rule_id")] or ""), str(r[h3.index("rule_name")] or ""),
            str(r[h3.index("rule_strength")] or ""),
            dim, (DIMENSION_MAP.get(dim) or (None,))[0],
            str(r[h3.index("last_problem_status")] or ""),
            str(r[h3.index("problem_desc")] or ""),
            str(xl_date(r[h3.index("problem_submit_time")]) or ""),
            str(r[h3.index("problem_submitter_name")] or ""),
            str(r[h3.index("last_problem_owner_names")] or ""),
            str(r[h3.index("last_validate_scope")] or ""),
            str(r[h3.index("last_validate_result")] or ""),
            str(r[h3.index("ds")] or ""),
        ))
    c.executemany("INSERT INTO problem VALUES (%s)" % ",".join(["?"] * 21), recs3)
    stat["problem"] = len(recs3)

    # ---- 4. 台账·问题工单 ----
    qs = load_ledger(3)
    recs4 = []
    for i, r in enumerate(qs[1:], 1):
        def gg(k):
            return str(r[k]).replace("\n", " ").strip() if k < len(r) else ""
        if not gg(5) and not gg(4):
            continue
        cat = gg(10)
        dim = cat.replace("问题", "") if cat else None
        lead = None
        d1, d2 = xl_date(gg(7)), xl_date(gg(13))
        if d1 and d2:
            try:
                lead = (dt.datetime.strptime(d2, "%Y-%m-%d")
                        - dt.datetime.strptime(d1, "%Y-%m-%d")).days
            except Exception:
                lead = None
        recs4.append((
            i, gg(1), gg(2), gg(3), gg(4), gg(5), gg(6), d1, gg(8), gg(9), cat, dim,
            gg(11), xl_date(gg(12)), d2, lead, gg(14), gg(17),
        ))
    c.executemany("INSERT INTO problem_ticket VALUES (%s)" % ",".join("?" * 18), recs4)
    stat["problem_ticket"] = len(recs4)

    # ---- 5. 台账·用数需求 ----
    rs1 = load_ledger(1)
    recs5 = []
    for i, r in enumerate(rs1[1:], 1):
        def gg(k):
            return str(r[k]).replace("\n", " ").strip() if k < len(r) else ""
        if not gg(3) and not gg(2):
            continue
        lead = None
        d1, d2 = xl_date(gg(9)), xl_date(gg(16))
        if d1 and d2:
            try:
                lead = (dt.datetime.strptime(d2, "%Y-%m-%d")
                        - dt.datetime.strptime(d1, "%Y-%m-%d")).days
            except Exception:
                lead = None
        recs5.append((
            i, gg(1), gg(2), gg(3), gg(4), gg(5), gg(6), xl_date(gg(7)),
            gg(8), d1, gg(10), gg(11), gg(12), gg(13), gg(14),
            xl_date(gg(15)), d2, gg(17), lead,
        ))
    c.executemany("INSERT INTO requirement VALUES (%s)" % ",".join("?" * 19), recs5)
    stat["requirement"] = len(recs5)

    # ---- 6. 维度映射 ----
    dm = [(k, v[0], v[1], v[2], "技术规则库") for k, v in DIMENSION_MAP.items()]
    dm += [(v[0], k, v[1], v[2], "业务台账") for k, v in DIMENSION_EXTRA.items()]
    c.executemany("INSERT INTO dimension_map VALUES (?,?,?,?,?)", dm)
    stat["dimension_map"] = len(dm)

    # ---- 7. 期次维度（环比用）----
    per = [
        (202608, "2026-08", "2026 年 8 月", "2026-08-14", "2026-08-22", 0),   # 上一期
        (202609, "2026-09", "2026 年 9 月", "2026-09-14", "2026-09-22", 1),   # 本期
    ]
    fix = []
    for pid, label, cn, ds0, ds1, cur in per:
        r = c.execute("SELECT MIN(ds), MAX(ds) FROM rule_exec WHERE ds/100=?", (pid,)).fetchone()
        if r and r[0]:
            ds0, ds1 = str(r[0]), str(r[1])
        fix.append((pid, label, cn, ds0, ds1, cur))
    c.executemany("INSERT INTO period_dim VALUES (?,?,?,?,?,?)", fix)
    stat["period_dim"] = len(fix)

    # ---- 8. meta ----
    meta = [
        ("build_time", dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")),
        ("source_period", "2026-09-18 ~ 2026-09-22"),
        ("source_note", "三表为样例数据（8 月/9 月两期）；台账为真实历史工单（2023）"),
        ("single_period", "false"),
        ("current_period", "202609"),
        ("prev_period", "202608"),
        ("periods", "202608,202609"),
        ("dim_alias", "技术口径 STABILITY(稳定性) 按客户报告模板统一呈现为「准确性」"),
    ]
    c.executemany("INSERT INTO meta VALUES (?,?)", meta)

    # 索引
    for sql in [
        "CREATE INDEX ix_rule_domain ON quality_rule(data_domain)",
        "CREATE INDEX ix_rule_dim ON quality_rule(dimension)",
        "CREATE INDEX ix_exec_rule ON rule_exec(rule_id)",
        "CREATE INDEX ix_exec_status ON rule_exec(task_status)",
        "CREATE INDEX ix_prob_table ON problem(table_name)",
        "CREATE INDEX ix_prob_domain ON problem(data_domain)",
        "CREATE INDEX ix_prob_dim ON problem(dimension)",
        "CREATE INDEX ix_prob_status ON problem(problem_status)",
        "CREATE INDEX ix_ticket_cat ON problem_ticket(category)",
    ]:
        c.execute(sql)
    conn.commit()

    # ---- 统计输出 ----
    print("=" * 70)
    print("数据底座构建完成 ->", DB)
    for k, v in stat.items():
        print("  %-16s %d 行" % (k, v))
    print()
    print("--- 数据域分布（规则/问题）---")
    for row in c.execute("""SELECT data_domain,
                                   (SELECT COUNT(*) FROM quality_rule q WHERE q.data_domain=p.data_domain),
                                   COUNT(*)
                            FROM problem p GROUP BY data_domain ORDER BY 3 DESC"""):
        print("  %-8s 规则 %-5s 问题 %s" % row)
    print()
    print("--- 质量维度分布（本期 ds=20260922）---")
    for row in c.execute("""SELECT dimension_cn, COUNT(*), SUM(rule_strength='STRONG')
                            FROM problem WHERE ds=20260922 GROUP BY dimension_cn ORDER BY 2 DESC"""):
        print("  %-8s %-5s (其中强规则 %s)" % row)
    print()
    print("--- 环比（8 月 → 9 月）---")
    for pid, label, cn, ds0, ds1, cur in fix:
        t = c.execute("SELECT COUNT(*), SUM(rule_strength='STRONG') FROM problem WHERE ds=?",
                      (int(str(pid) + "22"),)).fetchone()
        e = c.execute("""SELECT SUM(task_status='SUCCESS'),
                                SUM(CASE WHEN task_status='SUCCESS' AND is_pass='Y' THEN 1 ELSE 0 END)
                         FROM rule_exec WHERE ds/100=?""", (pid,)).fetchone()
        done = e[0] or 0; ok = e[1] or 0
        print("  %s: 问题 %-5s 强规则 %-5s(%.1f%%) | 执行成功 %-5s 通过 %-5s 通过率 %.1f%%" % (
            cn, t[0], t[1], (t[1] / t[0] * 100 if t[0] else 0), done, ok, (ok / done * 100 if done else 0)))
    print()
    print("--- 台账工单分类分布 ---")
    for row in c.execute("SELECT category, COUNT(*) FROM problem_ticket GROUP BY category ORDER BY 2 DESC"):
        print("  %-10s %s" % row)
    print()
    print("--- 执行任务状态 ---")
    for row in c.execute("SELECT task_status, COUNT(*) FROM rule_exec GROUP BY task_status ORDER BY 2 DESC"):
        print("  %-10s %s" % row)
    conn.close()


if __name__ == "__main__":
    build()
