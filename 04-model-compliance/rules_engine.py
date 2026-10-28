# -*- coding: utf-8 -*-
"""
数据模型符合性检查 · 规则引擎（纯标准库，无第三方依赖）

设计原则：
  1) 数由代码算 —— 解析建表语句 / 表结构清单，逐条套用判据，结果完全确定、可复现；
  2) 话由模型说 —— 本模块只输出结构化事实（不符合项、位置、依据、建议），不生成叙述；
  3) 规则可维护 —— 每条规则 = 编号 + 检查项类型 + 属性 + 级别 + 判据 + 依据 + 提示语，
     对应「元数据检查规则表」，见同目录 元数据检查规则表.csv。

依据：《烟草商业系统元数据管理规范（示例稿）》
  5.2 数据库表元模型（表英文名、数据库表中文名称、表主键、表分区信息…）
  5.3 数据库表字段元模型（字段英文名、字段存储类型、数据长度、数据精度、是否允许为空…）
  7.2 元数据采集 · 规范性审核（完整性、准确性、格式规范性）

本地用法：
  python rules_engine.py ../samples/01_dwd_retail_cust_info.sql
  python rules_engine.py --all ../samples
"""

import csv, os
import io
import json
import re

# ------------------------------------------------------------------ 规则表
# 字段说明：id / 检查项类型 / 对应元模型属性 / 级别 / 判据 / 依据条款 / 依据强度 / 原文出处与关键句
# 依据强度三档（详见《规范条款与规则对照》）：
#   原文直判 —— 5.2/5.3 明确该属性必填，取不到值即不符
#   属性推导 —— 规范只定义属性含义，未给判据，需一句解释才成立
#   技术约定 —— 规范无对应条款，判据来自建模与数据库工程通例，须客户确认后方可作为判定依据
RULES = [
    # ---------------- 表级 ----------------
    ("T01", "命名", "表英文名", "违反",
     "表名未以分层前缀 ods_/dwd_/dim_/dws_/ads_ 开头",
     "5.2 表英文名；7.1 元模型规划（命名规范）",
     "技术约定",
     "7.1「…定义各类元数据管理对象的结构、属性及命名规范」——全文仅出现「命名规范」四字，未规定分层前缀"),
    ("T02", "命名", "表英文名", "风险",
     "表名含临时、备份、测试、日期或版本标记（tmp/test/bak/copy/old/new/_20xx/_v1）",
     "5.2 表英文名；7.3 注册发布（统一管理视图）",
     "属性推导",
     "7.3「注册发布：…正式注册发布至元数据管理平台，纳入统一管理视图」——临时/备份表不属注册对象"),
    ("T03", "命名", "表英文名", "风险",
     "表名含大写字母或非小写字母、数字、下划线字符",
     "5.2 表英文名；7.1 元模型规划（命名规范）",
     "技术约定",
     "7.1 仅要求「定义…命名规范」，未规定表名大小写与分隔符约定"),
    ("T04", "完整性", "数据库表中文名称", "违反",
     "表级注释（表中文名）为空",
     "5.2 数据库表中文名称；7.2 规范性审核（完整性）",
     "原文直判",
     "5.2-2「直观反映数据库表对应的业务对象、业务场景的标准业务名称」+ 7.2③ 完整性审核"),
    ("T05", "规范性", "表主键", "违反",
     "建表语句未定义主键（Hive 类数据源不适用）",
     "5.2 表主键；7.2 规范性审核",
     "原文直判",
     "5.2-10「该库表的主键信息」+ 7.2③ 完整性审核；Hive 无主键约束，POC 按方言豁免"),
    ("T06", "规范性", "表分区信息", "建议",
     "ods_/dwd_/dws_ 明细或汇总表字段数 ≥ 10，且未定义分区键",
     "5.2 表分区信息；7.2 规范性审核",
     "技术约定",
     "5.2-12「分区键名称、分区粒度（日/月）」只要求记录该信息，未要求明细表必须有分区；字段数阈值经 POC 拟制"),
    # ---------------- 字段级 ----------------
    ("C01", "命名", "字段英文名", "违反",
     "字段名不符合「大写下划线」命名基线",
     "5.3 字段英文名；7.1 元模型规划（命名规范）",
     "技术约定",
     "5.3-8「该字段英文名称注释」未给命名格式；大写下划线为建模通例"),
    ("C02", "命名", "字段英文名", "风险",
     "字段名长度超过 30 个字符",
     "5.3 字段英文名",
     "技术约定",
     "5.3-8 未规定长度上限；30 字符取自 Oracle 11g 标识符限制"),
    ("C03", "规范性", "字段英文名", "违反",
     "字段名使用 SQL 保留字（DESC/LEVEL/ORDER/USER/DATE/TIME/COMMENT/KEY 等）",
     "5.3 字段英文名；7.2 规范性审核（格式规范性）",
     "属性推导",
     "7.2③「格式规范性」——保留字作字段名须加引号包裹，属格式不合规；规范未附保留字清单"),
    ("C04", "完整性", "字段中文名", "违反",
     "字段注释（字段中文名）为空",
     "5.3 字段中文名；7.2 规范性审核（完整性）",
     "原文直判",
     "5.3-2「依据业务习惯、被普遍认可的该属性的中文语言描述的名称」+ 7.2②「确保元数据内容完整可读」"),
    ("C05", "类型", "字段存储类型", "违反",
     "金额、数量、比率类字段使用字符型存储",
     "5.3 字段存储类型；7.2 规范性审核（准确性）",
     "属性推导",
     "5.3-9「该字段在数据库里定义的存储类型」+ 7.2③「准确性」——金额用字符型与属性语义不符；规范未给类型选型矩阵"),
    ("C06", "类型", "字段存储类型", "违反",
     "日期、时间、月份类字段使用字符型存储，应为 DATE/DATETIME/TIMESTAMP",
     "5.3 字段存储类型；7.2 规范性审核（准确性）",
     "属性推导",
     "5.3-9 + 7.2③「准确性」——日期用字符型无法做时间运算与分区裁剪，与存储类型含义不符"),
    ("C07", "完整性", "数据长度", "违反",
     "字符型字段未定义长度",
     "5.3 数据长度；7.2 规范性审核（完整性）",
     "原文直判",
     "5.3-10「描述该字段所允许的数据长度」——未定义长度即该属性缺失"),
    ("C08", "长度精度", "数据长度", "风险",
     "非备注类字符型字段长度 ≥ 255，未按业务实际定长",
     "5.3 数据长度",
     "技术约定",
     "5.3-10 只要求记录长度，未规定长度上限；255 阈值为 POC 拟制"),
    ("C09", "长度精度", "数据精度", "风险",
     "金额、比率类 DECIMAL/NUMBER 未定义小数位",
     "5.3 数据精度",
     "属性推导",
     "5.3-11「描述该字段所表达的精确度，即小数点位数」——金额/比率类不给小数位即精度属性缺失"),
    ("C10", "规范性", "是否允许为空", "违反",
     "主键字段未声明 NOT NULL，或声明为允许为空",
     "5.3 是否允许为空；5.2 表主键",
     "原文直判",
     "5.3-12「是,否」+ 5.2-10「该库表的主键信息」——主键允许为空即主键信息不成立"),
    ("C11", "一致性", "字段英文名", "违反",
     "同一表内字段名重复（忽略大小写与下划线）",
     "5.3 字段英文名；7.3 清洗去重与标准化",
     "属性推导",
     "7.3「清洗去重与标准化：对采集的元数据进行去重」——该句针对元数据条目去重，非表内字段重名，属间接引用"),
    ("C12", "类型", "字段存储类型", "风险",
     "是否/标志类字段使用 VARCHAR，应为 CHAR(1)",
     "5.3 字段存储类型",
     "技术约定",
     "5.3-9 未规定标志位类型；CHAR(1)/TINYINT 为建模通例"),
    ("C13", "一致性", "字段存储类型", "风险",
     "编码、编号、主键类字段（_ID/_NO/_CODE/_KEY）使用数值型或日期型",
     "5.3 字段存储类型；7.2 规范性审核（准确性）",
     "技术约定",
     "5.3-9 未规定编码类字段应以字符型存储；前导零丢失为工程经验"),
    ("C14", "类型", "字段存储类型", "风险",
     "金额、比率类字段使用 FLOAT/DOUBLE 浮点型，存在精度丢失风险",
     "5.3 字段存储类型；5.3 数据精度",
     "技术约定",
     "5.3-11 只要求记录小数位，未禁止 FLOAT/DOUBLE；判据来自浮点精度丢失的通识"),
    ("C15", "完整性", "字段存储类型", "违反",
     "字段存储类型为空",
     "5.3 字段存储类型；7.2 规范性审核（完整性）",
     "原文直判",
     "5.3-9 为必填属性，取空值即完整性不符"),
    ("C16", "命名", "字段英文名", "风险",
     "字段名为拼音或拼音缩写，缺少可读的业务语义",
     "5.3 字段英文名；7.2 规范性审核（准确性）",
     "属性推导",
     "7.2②「确保元数据内容完整可读」+ 7.2③「准确性」——拼音缩写不具备业务可读语义"),
]

RULE_MAP = {r[0]: {"id": r[0], "type": r[1], "attr": r[2], "severity": r[3],
                   "criterion": r[4], "basis": r[5],
                   "strength": r[6], "quote": r[7]} for r in RULES}
SEQ = {r[0]: i for i, r in enumerate(RULES)}

SEV_ORDER = {"违反": 0, "风险": 1, "建议": 2}
SEV_WEIGHT = {"违反": 8, "风险": 4, "建议": 1}

RESERVED = {"DESC", "ASC", "LEVEL", "ORDER", "USER", "DATE", "TIME", "TIMESTAMP",
            "COMMENT", "KEY", "INDEX", "GROUP", "SORT", "RANK", "VALUE", "CHECK",
            "TABLE", "COLUMN", "SELECT", "FROM", "WHERE", "JOIN", "UNION", "ALL",
            "AND", "OR", "NOT", "NULL", "DEFAULT", "PRIMARY", "FOREIGN", "LIMIT"}

NUM_TYPES = {"DECIMAL", "NUMERIC", "NUMBER", "INT", "INTEGER", "BIGINT", "SMALLINT",
             "TINYINT", "DOUBLE", "FLOAT", "REAL", "LONG", "INT64", "DECIMAL128"}
FLOAT_TYPES = {"DOUBLE", "FLOAT", "REAL"}
CHAR_TYPES = {"CHAR", "VARCHAR", "VARCHAR2", "NVARCHAR", "NVARCHAR2", "TEXT",
              "STRING", "MEDIUMTEXT", "LONGTEXT", "CLOB", "NCHAR"}
DATE_TYPES = {"DATE", "DATETIME", "TIMESTAMP", "TIME", "DATETIME2"}

AMT_TOKENS = {"AMT", "AMOUNT", "MONEY", "PRICE", "QTY", "QUANTITY", "NUM", "CNT",
              "COUNT", "FEE", "COST", "BALANCE", "TOTAL", "WEIGHT", "VOLUME",
              "RATE", "RATIO", "SCORE", "PERCENT", "TAX", "SALARY"}
DATE_TOKENS = {"DATE", "TIME", "DT", "TS", "MONTH", "DAY", "DATETIME", "YEAR"}
CODE_TOKENS = {"ID", "NO", "CODE", "KEY", "NUM", "SERIAL"}
FLAG_TOKENS = {"FLAG", "YN", "IS", "VALID", "ENABLE"}
WIDE_TEXT_OK = {"REMARK", "NOTE", "MEMO", "DESC", "CONTENT", "ADDR", "ADDRESS",
                "URL", "PATH", "COMMENT", "REASON", "OPINION"}
# 拼音/缩写字段库（POC 阶段人工整理，正式版由数据运营组维护）
PINYIN_TOKENS = {"KHID", "KHMC", "DDH", "DDRQ", "PLDM", "SL", "JE", "YHJE", "ZT",
                 "BZ", "DJ", "RQ", "XSJE", "KC", "BM", "CJR", "SQR", "JLDW",
                 "XH", "SFZH", "LXDH", "DZ", "YZBM", "CPMC", "GYS"}
PY_AMT = {"JE", "YHJE", "XSJE", "SL", "DJ", "KC"}
PY_DATE = {"DDRQ", "RQ", "SXRQ", "LXRQ"}
# 常见拼音缩写 → 建议英文字段名（POC 字典，正式版由数据运营组扩充）
PINYIN_DICT = {
    "KHID": "CUST_NO", "KHBH": "CUST_NO", "KHMC": "CUST_NAME", "DDH": "ORDER_NO",
    "DDRQ": "ORDER_DATE", "RQ": "BIZ_DATE", "PLDM": "PRODUCT_CODE", "SL": "BIZ_QTY",
    "JE": "BIZ_AMT", "YHJE": "DISCOUNT_AMT", "XSJE": "SALE_AMT", "ZT": "STATUS_CODE",
    "BZ": "REMARK", "DJ": "UNIT_PRICE", "KC": "STOCK_QTY", "BM": "DEPT_CODE",
    "MC": "NAME", "DM": "CODE", "SFZH": "ID_CARD_NO", "LXDH": "CONTACT_PHONE",
}


# ================================================================ SQL 文本处理
def _strip_comments(s):
    out = []
    i, n, q = 0, len(s), None
    while i < n:
        c = s[i]
        if q:
            out.append(c)
            if c == q:
                q = None
            i += 1
            continue
        if c in "'\"`":
            q = c
            out.append(c)
            i += 1
            continue
        if s[i:i + 2] == "--":
            j = s.find("\n", i)
            i = n if j < 0 else j
            continue
        if s[i:i + 2] == "/*":
            j = s.find("*/", i + 2)
            i = n if j < 0 else j + 2
            continue
        out.append(c)
        i += 1
    return "".join(out)


def _split_top(s, sep=","):
    parts, buf, depth, q = [], [], 0, None
    for c in s:
        if q:
            buf.append(c)
            if c == q:
                q = None
            continue
        if c in "'\"`":
            q = c
            buf.append(c)
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(buf))
            buf = []
            continue
        buf.append(c)
    if "".join(buf).strip():
        parts.append("".join(buf))
    return parts


def _match_paren(s, start):
    """返回与 s[start]=='(' 配对的右括号下标。"""
    depth, q = 0, None
    for i in range(start, len(s)):
        c = s[i]
        if q:
            if c == q:
                q = None
            continue
        if c in "'\"`":
            q = c
            continue
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _get_comment(seg):
    m = re.search(r"\bCOMMENT\s*[=\s]\s*'((?:[^']|'')*)'", seg, re.I)
    if not m:
        m = re.search(r'\bCOMMENT\s*[=\s]\s*"((?:[^"]|"")*)"', seg, re.I)
    if m:
        return m.group(1).replace("''", "'").strip()
    return ""


# ================================================================ 解析：DDL
def parse_ddl(text):
    """解析 CREATE TABLE 语句，返回 [{table:{...}, columns:[...], constraints:{...}}]"""
    s = _strip_comments(text)
    tables = []
    for m in re.finditer(r"create\s+(?:or\s+replace\s+)?(?:external\s+)?table\s+"
                         r"(?:if\s+not\s+exists\s+)?", s, re.I):
        i = m.end()
        nm = re.match(r"([`\"\[\]\w.$#]+)", s[i:])
        if not nm:
            continue
        raw_name = nm.group(1)
        i += nm.end()
        p = s.find("(", i)
        if p < 0:
            continue
        end = _match_paren(s, p)
        if end < 0:
            continue
        body = s[p + 1:end]
        semi = s.find(";", end)
        tail = s[end + 1: semi if semi > 0 else min(len(s), end + 800)]

        t = _new_table(raw_name)
        _parse_columns(body, t)
        _parse_tail(tail, t)
        tables.append(t)
    return tables


def _new_table(raw_name):
    name = raw_name.replace("`", "").replace('"', "").replace("[", "").replace("]", "")
    parts = name.split(".")
    schema = parts[-2] if len(parts) > 1 else ""
    return {"name": parts[-1], "raw_name": name, "schema": schema, "cn_name": "",
            "comment": "", "columns": [], "pk": [], "partition": [],
            "partitioned": False, "dialect": "", "source": "ddl", "line": 0}


def _parse_columns(body, t):
    for idx, seg in enumerate(_split_top(body)):
        seg = seg.strip()
        if not seg:
            continue
        head = seg.split(None, 1)[0].upper().strip("`\"[")
        if head in ("PRIMARY", "UNIQUE", "KEY", "INDEX", "CONSTRAINT", "FOREIGN",
                    "PARTITION", "DISTRIBUTE", "SORT", "CLUSTERED", "COMMENT"):
            low = seg.upper()
            if low.startswith("PRIMARY"):
                pm = re.search(r"\((.*?)\)", seg, re.S)
                if pm:
                    t["pk"].extend([c.strip().strip("`\"[]").upper()
                                    for c in pm.group(1).split(",")])
            elif low.startswith("CONSTRAINT") and "PRIMARY KEY" in low:
                pm = re.search(r"PRIMARY\s+KEY\s*\((.*?)\)", seg, re.S | re.I)
                if pm:
                    t["pk"].extend([c.strip().strip("`\"[]").upper()
                                    for c in pm.group(1).split(",")])
                if not t["name"]:
                    t["name"] = ""
            continue

        col = _parse_column(seg)
        if col:
            col["line"] = idx + 1
            if col["pk"] and col["name"] not in t["pk"]:
                t["pk"].append(col["name"])
            t["columns"].append(col)


def _parse_column(seg):
    m = re.match(r"\s*(`[^`]+`|\"[^\"]+\"|\[[^\]]+\]|[A-Za-z_][A-Za-z0-9_$#]*)", seg)
    if not m:
        return None
    name = m.group(1).strip("`\"[]")
    rest = seg[m.end():].strip()
    tm = re.match(r"^([A-Za-z][A-Za-z0-9_]*)", rest)
    if not tm:
        return None
    base = tm.group(1).upper()
    after = rest[tm.end():]
    mw = re.match(r"^\s+(PRECISION|VARYING)\b", after, re.I)
    if mw:
        after = after[mw.end():]
        if mw.group(1).upper() == "PRECISION":
            base = "DOUBLE"
    plen, pscale = None, None
    pm = re.match(r"\s*\(\s*(\d+)\s*(?:,\s*(\d+)\s*)?\)", after)
    if pm:
        plen = int(pm.group(1))
        pscale = int(pm.group(2)) if pm.group(2) is not None else None
        after = after[pm.end():]
    cm = re.search(r"\bCOMMENT\s+'((?:[^']|'')*)'", after, re.I) or \
         re.search(r'\bCOMMENT\s+"((?:[^"]|"")*)"', after, re.I)
    dm = re.search(r"\bDEFAULT\s+(?:'([^']*)'|([^\s,]+))", after, re.I)
    default = ""
    if dm:
        default = dm.group(1) if dm.group(1) is not None else dm.group(2)
    return {"name": name, "type": base, "len": plen, "scale": pscale,
            "notnull": bool(re.search(r"\bNOT\s+NULL\b", after, re.I)),
            "default": default,
            "comment": (cm.group(1).replace("''", "'").strip() if cm else ""),
            "pk": bool(re.search(r"\bPRIMARY\s+KEY\b", after, re.I)),
            "partition": False}


def _parse_tail(tail, t):
    cm = re.search(r"\bCOMMENT\s*=\s*'((?:[^']|'')*)'", tail, re.I) or \
         re.search(r"\bCOMMENT\s+'((?:[^']|'')*)'", tail, re.I)
    if cm:
        t["comment"] = cm.group(1).replace("''", "'").strip()
    pm = re.search(r"PARTITIONED\s+BY\s*\(", tail, re.I)
    if pm:
        p = tail.find("(", pm.start())
        end = _match_paren(tail, p)
        if end > 0:
            t["partitioned"] = True
            for seg in _split_top(tail[p + 1:end]):
                seg = seg.strip()
                if not seg:
                    continue
                col = _parse_column(seg)
                if col:
                    col["partition"] = True
                    t["partition"].append(col["name"].upper())
                    t["columns"].append(col)
    if re.search(r"\bPARTITION\s+BY\b", tail, re.I) and not t["partitioned"]:
        t["partitioned"] = True
        t["partition"] = t["partition"] or ["PARTITION"]


# ================================================================ 解析：CSV
CSV_HEADERS = {
    "table_en": ["表英文名", "表名英文", "英文表名", "表名", "表名称", "table_name",
                 "tablename", "tableen", "table"],
    "table_cn": ["表中文名", "表中文名称", "表注释", "表描述", "表中文注释",
                 "table_comment", "tablecn", "表名中文"],
    "col_en": ["字段英文名", "字段名英文", "英文名", "字段名", "字段名称", "列名",
               "column_name", "columnname", "fieldname", "field_name", "字段"],
    "col_cn": ["字段中文名", "字段中文名称", "中文名", "字段注释", "字段描述", "注释",
               "备注", "说明", "column_comment", "fieldcn", "描述"],
    "dtype": ["数据类型", "字段类型", "字段数据类型", "类型", "data_type", "datatype", "type"],
    "length": ["数据长度", "长度", "字段长度", "length", "len", "size"],
    "scale": ["数据精度", "精度", "小数位", "scale", "decimal"],
    "pk": ["是否主键", "主键", "主键标识", "primary_key", "pk", "is_pk"],
    "nullable": ["是否允许为空", "允许为空", "是否可空", "可空", "nullable", "is_null"],
    "default": ["默认值", "default", "default_value"],
    "orderno": ["序号", "字段序号", "no", "index"],
}


def _norm_head(h):
    return re.sub(r"[\s　_（）()：:\-]", "", (h or "")).lower()


def _map_header(row):
    norm = [_norm_head(h) for h in row]
    idx = {}
    for key, names in CSV_HEADERS.items():
        for i, h in enumerate(norm):
            if h and any(h == _norm_head(n) for n in names):
                idx[key] = i
                break
        if key in idx:
            continue
        for i, h in enumerate(norm):
            if h and any(_norm_head(n) in h for n in names):
                idx[key] = i
                break
    return idx


def _guess_delim(text):
    line = ""
    for ln in text.splitlines():
        if ln.strip():
            line = ln
            break
    best, bestn = ",", -1
    for d in [",", "\t", ";", "|"]:
        n = line.count(d)
        if n > bestn:
            best, bestn = d, n
    return best


def parse_csv(text, filename=""):
    text = text.replace("\ufeff", "")
    d = _guess_delim(text)
    rows = list(csv.reader(io.StringIO(text), delimiter=d))
    rows = [r for r in rows if any((c or "").strip() for c in r)]
    if not rows:
        return []
    idx = _map_header(rows[0])

    def cell(r, key):
        i = idx.get(key)
        if i is None or i >= len(r):
            return ""
        return (r[i] or "").strip()

    if "col_en" not in idx:
        return []
    tables, cur = [], None
    for r in rows[1:]:
        if not any((c or "").strip() for c in r):
            continue
        tname = cell(r, "table_en")
        colname = cell(r, "col_en")
        if not colname:
            continue
        if cur is None or (tname and tname != cur["name"]):
            base = tname or (filename.rsplit(".", 1)[0] if filename else "") or "未命名表"
            cur = _new_table(base)
            cur["source"] = "csv"
            cur["cn_name"] = cell(r, "table_cn")
            cur["comment"] = cur["cn_name"]
            cur["_has_cn_col"] = "table_cn" in idx
            tables.append(cur)
        dtype = cell(r, "dtype").upper()
        plen = cell(r, "length")
        pscale = cell(r, "scale")
        pk = cell(r, "pk")
        nullable = cell(r, "nullable")
        col = {"name": colname, "type": dtype,
               "len": int(plen) if plen.isdigit() else None,
               "scale": int(pscale) if pscale.isdigit() else None,
               "notnull": (nullable in ("否", "N", "n", "no", "No", "NO", "FALSE")),
               "comment": cell(r, "col_cn"), "pk": pk in ("是", "Y", "y", "yes", "YES", "TRUE", "1"),
               "partition": False, "_len_given": bool(plen), "_scale_given": bool(pscale)}
        if col["pk"]:
            cur["pk"].append(col["name"].upper())
        cur["columns"].append(col)
    for t in tables:
        t["partitioned"] = False
    return tables


# ================================================================ 工具
def _upper_snake(n):
    s = re.sub(r"[^0-9A-Za-z]+", "_", n or "")
    s = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", "_", s)
    s = re.sub(r"_+", "_", s).strip("_").upper()
    return s


def _camel_split(n):
    """把 custNo / statMonth 这类驼峰名拆成 CUST_NO / STAT_MONTH 再切词，
    否则 saleAmt 会被当成单个词，识别不出 AMT。"""
    s = re.sub(r"([a-z0-9])([A-Z])", r"\1_\2", n or "")
    return s


def _tokens(n):
    s = _camel_split(n)
    return set(t for t in re.split(r"[_\W0-9]+", s.upper()) if t)


def _last_token(n):
    parts = [p for p in re.split(r"[_\W]+", _camel_split(n).upper()) if p]
    return parts[-1] if parts else ""


def _last2(n):
    parts = [p for p in re.split(r"[_\W]+", _camel_split(n).upper()) if p]
    return parts[-2] if len(parts) >= 2 else ""


def _col_norm(n):
    return re.sub(r"_", "", (n or "").upper())


# ================================================================ 规则检查
def check_table(t):
    """对单张表执行全部规则，返回 findings（已按规则分组）。"""
    hits = {}   # rule_id -> {"items":[{...}], ...}

    def add(rid, item=None, note=""):
        h = hits.setdefault(rid, {"items": [], "note": ""})
        if item:
            h["items"].append(item)
        if note:
            h["note"] = note

    name = t["name"]
    cols = t["columns"]
    dialect = t["dialect"]
    up = name.upper()

    # ---- T01 分层前缀
    if not re.match(r"^(ods|dwd|dim|dws|ads|app)_", name, re.I):
        hint = ""
        if re.match(r"^(tmp|temp|test|bak|copy|old|new)_", name, re.I):
            hint = "以临时/测试前缀开头，临时表不应纳入共享目录"
        add("T01", {"field": name, "current": name,
                    "expected": "dwd_" + re.sub(r"^(tmp|temp|test|bak|copy|old|new)_+", "", name, flags=re.I),
                    "note": hint})
    # ---- T02 临时/备份/日期/版本标记
    marks = re.findall(r"(?:^|_)(tmp|temp|test|bak|copy|old|new|bak\d*|20\d{6}|20\d{2}|v\d+)(?:_|$)",
                       name, re.I)
    if marks:
        cleaned = re.sub(r"^(?:tmp|temp|test|bak|copy|old|new)_", "", name, flags=re.I)
        cleaned = re.sub(r"_(?:tmp|temp|test|bak|copy|old|new|20\d{2,6}|v\d+)(?=_|$)", "",
                         cleaned, flags=re.I)
        add("T02", {"field": name, "current": name, "expected": cleaned,
                    "note": "命中标记：" + "、".join(sorted(set(m.lower() for m in marks)))})
    # ---- T03 大写/非法字符
    if not re.match(r"^[a-z][a-z0-9_]*$", name):
        add("T03", {"field": name, "current": name, "expected": name.lower(),
                    "note": ""})
    # ---- T04 表注释
    if not (t.get("comment") or t.get("cn_name")):
        add("T04", {"field": name, "current": "（空）", "expected": "填写表中文名/表注释",
                    "note": "表级业务含义无法识别"})
    # ---- T05 主键
    if dialect != "hive" and not t["pk"]:
        add("T05", {"field": name, "current": "未定义主键", "expected": "PRIMARY KEY (...)",
                    "note": ""})
    # ---- T06 分区
    if re.match(r"^(ods|dwd|dws)_", name, re.I) and len(cols) >= 10 and not t["partitioned"]:
        add("T06", {"field": name, "current": "无分区键", "expected": "按日/月分区（如 DS yyyyMMdd）",
                    "note": ""})

    # ---- 字段级
    seen = {}
    for c in cols:
        cn = c["name"]
        ctype = (c["type"] or "").upper()
        toks = _tokens(cn)
        last = _last_token(cn)
        is_pk = cn.upper() in t["pk"]
        # C01 命名基线（保留字交给 C03）
        if not re.match(r"^[A-Z][A-Z0-9_]*$", cn) and cn.upper() not in RESERVED:
            add("C01", {"field": cn, "current": cn, "expected": _upper_snake(cn), "note": ""})
        # C02 长度
        if len(cn) > 30:
            add("C02", {"field": cn, "current": "%d 字符" % len(cn),
                        "expected": "≤ 30 字符", "note": ""})
        # C03 保留字
        if cn.upper() in RESERVED:
            add("C03", {"field": cn, "current": cn,
                        "expected": _upper_snake("F_" + cn) if not cn.upper().startswith("F_") else cn,
                        "note": "SQL 保留字，需加业务前缀"})
        # C04 注释
        if not (c.get("comment") or "").strip():
            add("C04", {"field": cn, "current": "（空）", "expected": "补充字段中文名与业务含义",
                        "note": ""})
        # C05 金额/数量用字符型
        if ((last in AMT_TOKENS) or (cn.upper() in PY_AMT)) and ctype in CHAR_TYPES \
                and last not in ("NAME", "DESC"):
            add("C05", {"field": cn, "current": ctype, "expected": "DECIMAL(18,2)", "note": ""})
        # C06 日期用字符型
        if (last in DATE_TOKENS or cn.upper() in PY_DATE) and ctype in CHAR_TYPES and cn.upper() != "DS":
            add("C06", {"field": cn, "current": ctype, "expected": "DATE / DATETIME", "note": ""})
        # C07 字符型无长度
        if ctype in CHAR_TYPES and ctype not in ("TEXT", "STRING", "MEDIUMTEXT",
                                                 "LONGTEXT", "CLOB") and c["len"] is None:
            if t["source"] == "ddl" or c.get("_len_given") is False:
                add("C07", {"field": cn, "current": "%s（无长度）" % ctype,
                            "expected": "%s(业务定长)" % ctype, "note": ""})
        # C08 长度过宽
        if ctype in CHAR_TYPES and c["len"] and c["len"] >= 255:
            if not (toks & WIDE_TEXT_OK):
                add("C08", {"field": cn, "current": "%s(%d)" % (ctype, c["len"]),
                            "expected": "%s(按业务定长)" % ctype, "note": ""})
        # C09 小数位缺失
        if (toks & AMT_TOKENS) and ctype in ("DECIMAL", "NUMERIC", "NUMBER") \
                and c["len"] and c["scale"] is None:
            add("C09", {"field": cn, "current": "%s(%d)" % (ctype, c["len"]),
                        "expected": "%s(%d,2)" % (ctype, c["len"]), "note": ""})
        # C10 主键可空
        if is_pk and not c["notnull"]:
            add("C10", {"field": cn, "current": "允许为空", "expected": "NOT NULL", "note": ""})
        # C11 重复字段
        key = _col_norm(cn)
        if key in seen and key:
            add("C11", {"field": cn, "current": "与 %s 重复" % seen[key],
                        "expected": "删除或重命名其一", "note": ""})
        else:
            seen[key] = cn
        # C12 标志位用 varchar
        if (last in FLAG_TOKENS or cn.upper().startswith("IS_")) and ctype in ("VARCHAR", "VARCHAR2", "STRING", "TEXT") \
                and not (c["len"] == 1):
            add("C12", {"field": cn, "current": ctype, "expected": "CHAR(1)", "note": ""})
        # C13 编码类用数值/日期（id 类代理键除外）
        if last in ("ID", "NO", "CODE", "KEY", "SERIAL") and ctype in (NUM_TYPES | DATE_TYPES) \
                and cn.upper() != "ID":
            note = "编码类字段用整型易丢失前导零，建议统一为字符串" if ctype in ("INT", "BIGINT", "SMALLINT", "TINYINT") else ""
            add("C13", {"field": cn, "current": ctype, "expected": "VARCHAR(32)", "note": note})
        # C14 金额用浮点
        if ((last in AMT_TOKENS) or (cn.upper() in PY_AMT)) and ctype in FLOAT_TYPES:
            add("C14", {"field": cn, "current": ctype, "expected": "DECIMAL(18,2)", "note": ""})
        # C15 类型为空
        if not ctype:
            add("C15", {"field": cn, "current": "（空）", "expected": "填写字段存储类型", "note": ""})
        # C16 拼音字段名
        if cn.upper() in PINYIN_TOKENS or bool(toks & PINYIN_TOKENS):
            add("C16", {"field": cn, "current": cn,
                        "expected": PINYIN_DICT.get(cn.upper()) or PINYIN_DICT.get(last) or "按业务含义改为英文",
                        "note": ""})

    findings = []
    for rid, h in hits.items():
        r = RULE_MAP[rid]
        items = h["items"]
        fields = [it["field"] for it in items]
        findings.append({
            "rule_id": rid, "type": r["type"], "severity": r["severity"],
            "attr": r["attr"], "criterion": r["criterion"], "basis": r["basis"],
            "table": t["name"], "fields": fields, "count": len(fields),
            "note": h["note"], "items": items,
        })
    findings.sort(key=lambda f: (SEV_ORDER[f["severity"]], SEQ[f["rule_id"]]))
    return findings


# ================================================================ 主入口
PENDING_BIZ = [
    ("业务简介", "对承载的业务实体、数据范围、用途做文字概述", "业务填报"),
    ("业务场景", "本表支撑的业务办理、经营分析、监管报送场景", "业务填报"),
    ("更新频率", "由 ETL 任务调度决定，需与数据开发确认", "业务填报"),
    ("敏感等级", "1~4 级，按字段泄露危害程度判定", "业务填报"),
    ("业务负责人", "数源部门业务责任人", "业务填报"),
    ("技术负责人", "系统/数据开发责任人", "业务填报"),
]


def check_text(text, filename="", hint=""):
    """校验任意输入（DDL 语句、CSV 表结构、或含 DDL 的自然语言），返回结构化结果。"""
    text = (text or "").replace("\ufeff", "").replace("\r\n", "\n").strip()
    res = {"ok": False, "mode": "", "dialect": "", "error": "",
           "tables": [], "findings": [], "pending": [], "score": 0,
           "summary": {"total": 0, "违反": 0, "风险": 0, "建议": 0},
           "warnings": []}
    if not text:
        res["error"] = "输入为空：请粘贴建表语句（CREATE TABLE …），或上传 DDL / 表结构 CSV 文件。"
        return res

    has_ddl = bool(re.search(r"create\s+(?:or\s+replace\s+)?(?:external\s+)?table\b", text, re.I))
    tables = parse_ddl(text) if has_ddl else []
    if not tables and not has_ddl:
        tables = parse_csv(text, filename)

    if not tables:
        if re.search(r"create\s+table", text, re.I):
            res["error"] = "已识别到建表语句，但括号或字段定义不完整，无法解析。请检查是否缺右括号、字段间是否缺少逗号。"
        elif re.search(r"select\b", text, re.I):
            res["error"] = "未识别到建表语句：当前能力只做「表结构符合性检查」，不解析查询语句。请提供 CREATE TABLE 语句或表结构清单。"
        else:
            res["error"] = ("未识别到建表语句或表结构清单。\n"
                            "可以直接粘贴 CREATE TABLE 语句，或用「＋」上传 .sql / .csv 表结构文件；\n"
                            "若只想了解检查依据，可上传任意一张表的建表语句样例。")
        return res

    dialect = "mysql"
    if re.search(r"partitioned\s+by|stored\s+as|tblproperties|row\s+format", text, re.I):
        dialect = "hive"
    elif re.search(r"varchar2|comment\s+on\s+(table|column)|number\s*\(", text, re.I):
        dialect = "oracle"
    elif re.search(r"`", text):
        dialect = "mysql"
    res["dialect"] = dialect
    res["mode"] = "ddl" if has_ddl else "csv"

    all_findings = []
    for t in tables:
        t["dialect"] = dialect
        # 以 DS/DT 等日期字段承载分区的表（MySQL/Oracle 常见），视同已分区
        for c in t["columns"]:
            if c["name"].upper() in ("DS", "DT", "PARTITION_DATE", "PT"):
                c["partition"] = True
                t["partitioned"] = True
                if c["name"].upper() not in t["partition"]:
                    t["partition"].append(c["name"].upper())
        if not t.get("comment") and t.get("cn_name"):
            t["comment"] = t["cn_name"]
        # Oracle: COMMENT ON TABLE / COLUMN
        if not t["comment"]:
            m = re.search(r"comment\s+on\s+table\s+[\w.`\"]*%s[\w.`\"]*\s+is\s+'([^']*)'"
                          % re.escape(t["name"]), text, re.I)
            if m:
                t["comment"] = m.group(1).strip()
        for c in t["columns"]:
            if not c.get("comment"):
                m = re.search(r"comment\s+on\s+column\s+[\w.`\"]*%s[\w.`\"]*\.%s\s+is\s+'([^']*)'"
                              % (re.escape(t["name"]), re.escape(c["name"])), text, re.I)
                if m:
                    c["comment"] = m.group(1).strip()
        all_findings.extend(check_table(t))

    score = 100
    for f in all_findings:
        score -= SEV_WEIGHT[f["severity"]]
    score = max(0, score)

    summary = {"total": len(all_findings), "违反": 0, "风险": 0, "建议": 0}
    for f in all_findings:
        summary[f["severity"]] += 1

    res.update({"ok": True, "tables": tables, "findings": all_findings,
                "score": score, "summary": summary,
                "pending": [{"attr": a, "desc": d, "how": h} for a, d, h in PENDING_BIZ]})
    return res


# ================================================================ 证据文本（给模型）
def render_evidence(res):
    """把结构化结果压成给模型的紧凑证据文本；不含结论话术。"""
    if not res.get("ok"):
        return "【解析失败】" + res.get("error", "")
    lines = []
    multi = len(res["tables"]) > 1
    lines.append("【解析结果】输入形态：%s，方言：%s，识别表数：%d"
                 % ("建表语句 DDL" if res["mode"] == "ddl" else "表结构清单 CSV",
                    res["dialect"].upper(), len(res["tables"])))
    for t in res["tables"]:
        lines.append("")
        lines.append("【表 %s%s】" % (t["name"], ("（%s）" % t["comment"]) if t["comment"] else "（表注释缺失）"))
        lines.append("字段数：%d；主键：%s；分区键：%s；表注释：%s"
                     % (len([c for c in t["columns"] if not c.get("partition")]),
                        "、".join(t["pk"]) if t["pk"] else "未定义",
                        "、".join(t["partition"]) if t["partition"] else "无",
                        t["comment"] or "缺失"))
        fs = [f for f in res["findings"] if f["table"] == t["name"]]
        if not fs:
            lines.append("不符合项：0 项（全部通过）")
        else:
            v = sum(1 for f in fs if f["severity"] == "违反")
            r = sum(1 for f in fs if f["severity"] == "风险")
            s = sum(1 for f in fs if f["severity"] == "建议")
            lines.append("不符合项：%d 项（违反 %d / 风险 %d / 建议 %d）" % (len(fs), v, r, s))
            for i, f in enumerate(fs, 1):
                lines.append("%d. [%s｜%s｜%s｜%s] 判据：%s"
                             % (i, f["rule_id"], f["type"], f["severity"], f["attr"], f["criterion"]))
                if f["count"] == 1 and f["fields"][0] == t["name"]:
                    lines.append("   位置：表级（%s）" % t["name"])
                else:
                    lines.append("   涉及位置（%d 个）：%s" % (f["count"], "、".join(f["fields"])))
                if f["note"]:
                    lines.append("   说明：%s" % f["note"])
                pairs, seen_pairs = [], set()
                for it in f["items"][:12]:
                    if it.get("expected") and it["expected"] != it["current"]:
                        p = "%s → %s" % (it["current"], it["expected"])
                        if p not in seen_pairs:
                            seen_pairs.add(p)
                            pairs.append(p)
                if pairs:
                    lines.append("   建议改法：" + "；".join(pairs))
                lines.append("   依据：%s" % f["basis"])
    lines.append("")
    lines.append("合规评分：%d 分（满分 100，按违反 -8 / 风险 -4 / 建议 -1 逐项扣减）" % res["score"])
    lines.append("【建表语句中无法判断、需在编目页确认的元数据】")
    for p in res["pending"]:
        lines.append("- %s（%s）：%s" % (p["attr"], p["how"], p["desc"]))
    if multi:
        lines.append("注：本次共识别 %d 张表，逐表给出结论。" % len(res["tables"]))
    return "\n".join(lines)


def findings_payload(res):
    """给前端导出检查清单用的精简结构。"""
    return {
        "score": res.get("score", 0),
        "summary": res.get("summary", {}),
        "pending": res.get("pending", []),
        "tables": [{"name": t["name"], "cn": t.get("comment", ""),
                    "fields": len([c for c in t["columns"] if not c.get("partition")]),
                    "pk": t["pk"], "partition": t["partition"]} for t in res.get("tables", [])],
        "findings": [{"rule_id": f["rule_id"], "type": f["type"], "severity": f["severity"],
                      "attr": f["attr"], "criterion": f["criterion"], "basis": f["basis"],
                      "table": f["table"], "fields": f["fields"], "count": f["count"],
                      "note": f["note"],
                      "items": [{"field": it["field"], "current": it["current"],
                                 "expected": it["expected"]} for it in f["items"]]}
                     for f in res.get("findings", [])],
    }


# ================================================================ CLI
def _cli():
    import os
    import sys
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    if not args:
        print(__doc__)
        return
    if "--json" in sys.argv:
        with open(args[0], encoding="utf-8") as f:
            r = check_text(f.read(), os.path.basename(args[0]))
        print(json.dumps(findings_payload(r), ensure_ascii=False, indent=2))
        return
    if args[0].endswith(os.sep) or os.path.isdir(args[0]):
        files = sorted(os.path.join(args[0], f) for f in os.listdir(args[0])
                       if f.lower().endswith((".sql", ".csv", ".txt", ".md")))
        for p in files:
            with open(p, encoding="utf-8") as f:
                r = check_text(f.read(), os.path.basename(p))
            s = r["summary"]
            print("%-42s 评分 %3d  共 %2d 项（违反 %d/风险 %d/建议 %d）%s"
                  % (os.path.basename(p), r["score"], s["total"], s["违反"], s["风险"],
                     s["建议"], "" if r["ok"] else "  !! " + r["error"][:40]))
        return
    with open(args[0], encoding="utf-8") as f:
        r = check_text(f.read(), os.path.basename(args[0]))
    if not r["ok"]:
        print("解析失败：", r["error"])
        return
    print(render_evidence(r))


if __name__ == "__main__":
    _cli()
