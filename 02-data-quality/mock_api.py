# -*- coding: utf-8 -*-
"""
数据质检 POC · Mock API 服务
=================================================
零依赖（仅标准库），从 dq_poc.db 读取，对外提供质检相关的确定性接口。

设计原则 —— 对应 V3「数由代码算，话由模型说」：
  · 本服务只负责**算数字、给证据**（统计、分布、匹配到的处置规则与案例），
    不生成任何叙述性结论；
  · 叙述由 Dify 侧的模型基于本服务返回的结构化事实组织。
  · 所有返回的 beizhu/source 字段都指向可核查的来源（台账工单号、样本规则名）。

启动：
    python mock_api.py [--port 8090]
接口文档：
    GET  /openapi.json      自动生成的 OpenAPI 3.0 规范（可直接导入 Dify 自定义工具）
    GET  /                   接口清单
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dq_poc.db")

# ------------------------------------------------------------------ 关键词 → 维度
DIM_KEYWORDS = [
    ("完整性", r"空值|缺失|为空|空串|空字符串|null|NULL|没数据|数据量少|补录|少了|缺少"),
    ("唯一性", r"重复|主键|唯一|duplicate|多条|去重|多档位"),
    ("一致性", r"不一致|对不上|不平|差异|对账|多源|两个表|跨表|笛卡尔"),
    ("有效性", r"格式|枚举|取值|有效|位数|不规范|不符合|日期格式|字典"),
    ("及时性", r"未及时|延迟|没更新|未更新|时点|T\+1|迟到|未发布|调度"),
    ("稳定性", r"波动|突增|突降|数据量异常|基线|趋势"),
    ("准确性", r"不准|精度|偏差|错值|偏大|偏小|统计错|口径不符"),
    ("规范性", r"命名|注释|规范|字段名|模型设计"),
]

TEMPLATE_BY_DIM = {
    "完整性": ("字段空值校验", "字段空字符串校验"),
    "唯一性": ("字段唯一性校验",),
    "一致性": ("多表一致性校验",),
    "有效性": ("字段有效性校验",),
    "及时性": ("单表时间字段比较",),
    "稳定性": ("表稳定性校验",),
    "准确性": ("字段有效性校验",),
    "规范性": ("字段有效性校验",),
}

# 阈值表达式 → 中文说明
METRIC_CN = {"ERROR_RATE": "错误率", "NORMAL_RATE": "正常率", "ERROR_NUMBER": "错误条数"}
OP_CN = {"<": "低于", "<=": "不高于", ">": "高于", ">=": "不低于", "=": "等于"}


def threshold_cn(expr):
    """'ERROR_RATE < 10' → '错误率低于 10%'；'ERROR_NUMBER = 0' → '错误条数等于 0'。"""
    if not expr:
        return None
    parts = str(expr).split()
    if len(parts) != 3:
        return expr
    m, o, v = parts
    mn, on = METRIC_CN.get(m, m), OP_CN.get(o, o)
    unit = "%" if m in ("ERROR_RATE", "NORMAL_RATE") else ""
    return "%s%s %s%s" % (mn, on, v, unit)


# ------------------------------------------------------------------ DB
def q(sql, args=()):
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    try:
        return [dict(r) for r in conn.execute(sql, args).fetchall()]
    finally:
        conn.close()


def q1(sql, args=()):
    r = q(sql, args)
    return r[0] if r else None


def pct(a, b):
    return round(a * 100.0 / b, 1) if b else 0.0


# ---------------------------------------------------------------- 期次（环比）
def period_rows():
    """全部期次，本期在前。"""
    rows = q("SELECT * FROM period_dim ORDER BY is_current DESC, period_id DESC")
    if rows:
        return rows
    # 兜底：无 period_dim 表时从数据推断
    ds = [r["ds"] for r in q("SELECT DISTINCT ds FROM problem ORDER BY ds DESC")]
    return [{"period_id": d // 100, "period_label": "%s-%s" % (str(d)[:4], str(d)[4:6]),
             "period_cn": "%s 年 %s 月" % (str(d)[:4], int(str(d)[4:6])),
             "date_start": str(d), "date_end": str(d),
             "is_current": 1 if i == 0 else 0} for i, d in enumerate(ds)]


def period_ds(pid):
    """期次 id（202609）→ 该期的代表性 ds（20260922）。"""
    r = q1("SELECT MAX(ds) AS n FROM problem WHERE ds/100 = ?", (int(pid),))
    return int(r["n"]) if r and r["n"] else int(str(pid) + "22")


def cur_prev_ds():
    ps = period_rows()
    cur = next((p for p in ps if p["is_current"]), ps[0] if ps else None)
    prev = next((p for p in ps if not p["is_current"]), None)
    return (period_ds(cur["period_id"]) if cur else 20260922,
            period_ds(prev["period_id"]) if prev else None)


def _to_ds(period):
    """把入参统一成 ds 分区号。

    坑（2026-10-08）：期次 id 是 6 位（202608），数据分区 ds 是 8 位（20260822）。
    早期直接把 6 位期间号当 ds 用，导致 WHERE ds = 202608 命中 0 行 ——
    表现出来是「报告出来了，但所有数字都是 0」，最难查的一类错。
    """
    if not period:
        cur, _ = cur_prev_ds()
        return cur
    p = int(str(period).strip())
    return period_ds(p) if p < 10000000 else p


def _period_block(ds, domain=None):
    """某一期的核心指标（供环比复用同一套口径）。domain 给定时只统计该域。

    注意口径差异：problem 每期只有一个 ds（期末快照），而 rule_exec 每期有多个 ds
    （期内每天一批），所以执行侧必须按「期」过滤（ds/100），不能按单日。
    """
    if not ds:
        return None
    pid = int(str(ds)[:6])
    w, a = "ds = ?", [ds]
    if domain:
        w += " AND data_domain = ?"
        a.append(domain)
    p = q1("SELECT COUNT(*) AS n, SUM(rule_strength='STRONG') AS s FROM problem WHERE " + w, a)
    ew, ea = "ds/100 = ?", [pid]
    if domain:
        ew += " AND data_domain = ?"
        ea.append(domain)
    e = q1("""SELECT SUM(task_status='SUCCESS') AS done,
                     SUM(CASE WHEN task_status='SUCCESS' AND is_pass='Y' THEN 1 ELSE 0 END) AS ok
              FROM rule_exec WHERE """ + ew, ea)
    n = p["n"] or 0
    s = p["s"] or 0
    done = e["done"] or 0
    ok = e["ok"] or 0
    return {"ds": ds, "period_id": pid, "domain": domain, "problems": n, "strong": s,
            "strong_share": pct(s, n),
            "exec_success": done, "exec_pass": ok, "pass_rate": pct(ok, done)}


def detect_dimension(text):
    if not text:
        return None
    for dim, pat in DIM_KEYWORDS:
        if re.search(pat, str(text), re.I):
            return dim
    return None


# ------------------------------------------------------------------ 各接口实现
def _scope(domain=None, period=None):
    """问题清单类查询的统一口径：一律限定到某一期（默认当期）。

    坑（2026-10-08）：入库两期之后，「不加 ds 条件」等于把两期加在一起。
    domain_list / table_list / overview 曾因此返回 2000 行的口径，
    而报告是 1000 行的口径 —— 同一个数字在两处不一样，很难解释。
    **教训：任何计数都要和同组列表用完全一致的过滤条件，包括期次。**
    """
    ds = _to_ds(period)
    cond, args = "WHERE ds = ?", [ds]
    if domain:
        cond += " AND data_domain = ?"
        args.append(domain)
    return ds, cond, args


def domain_list(period=None):
    ds, cond, args = _scope(None, period)
    rows = q(f"""SELECT data_domain AS domain,
                        COUNT(*) AS problem_count,
                        COUNT(DISTINCT table_name) AS table_count
                 FROM problem {cond}
                 GROUP BY data_domain ORDER BY problem_count DESC""", args)
    return {"ok": True, "period_ds": ds, "domains": rows}


def table_list(domain=None, period=None):
    ds, cond, args = _scope(domain, period)
    rows = q(f"""SELECT table_name, table_desc, data_domain AS domain,
                        COUNT(*) AS problem_count,
                        SUM(rule_strength='STRONG') AS strong_count
                 FROM problem {cond}
                 GROUP BY table_name ORDER BY problem_count DESC""", args)
    return {"ok": True, "period_ds": ds, "count": len(rows), "tables": rows}


def overview(domain=None, period=None):
    ds, cond, args = _scope(domain, period)

    total = q1(f"SELECT COUNT(*) AS n FROM problem {cond}", args)["n"]
    tables = q1(f"SELECT COUNT(DISTINCT table_name) AS n FROM problem {cond}", args)["n"]
    strong = q1(f"SELECT COUNT(*) AS n FROM problem {cond} "
                f"AND rule_strength='STRONG'", args)["n"]

    dims = q(f"""SELECT dimension_cn AS dimension, COUNT(*) AS count,
                        SUM(rule_strength='STRONG') AS strong_count
                 FROM problem {cond} GROUP BY dimension_cn ORDER BY count DESC""", args)
    for d in dims:
        d["share"] = pct(d["count"], total)

    status = q(f"SELECT problem_status AS status, COUNT(*) AS count FROM problem {cond} "
               f"GROUP BY problem_status ORDER BY count DESC", args)

    prow = next((x for x in period_rows() if period_ds(x["period_id"]) == ds), None)
    return {
        "ok": True,
        "scope": {"domain": domain or "全部数据域",
                  "period_ds": ds,
                  "period": prow["period_cn"] if prow else str(ds),
                  "single_period": False,        # 已有两期数据，不再是单期口径
                  "available_periods": [x["period_cn"] for x in period_rows()]},
        "totals": {"problem_count": total, "affected_tables": tables,
                   "strong_rule_problems": strong},
        "by_dimension": dims,
        "by_status": status,
    }


def _suggestions(scope_cond, args, limit=8):
    """按 scope 内出现的维度，匹配 handling_rule（处置规则）。

    problem.dimension_cn 形如「完整性问题」，handling_rule.dimension 形如「完整性」，
    两者用去掉「问题」后缀的方式对齐。
    """
    dims = []
    for r in q(f"SELECT DISTINCT dimension_cn FROM problem {scope_cond}", args):
        cn = r["dimension_cn"]
        if cn and cn not in dims:
            dims.append(cn)
    dims = [d[:-2] if d.endswith("问题") else d for d in dims]
    if not dims:
        return []
    ph = ",".join("?" * len(dims))
    sub_extra = (" " + scope_cond.replace("WHERE", "AND")) if scope_cond else ""
    rows = q(f"""SELECT hr.dimension, hr.symptom, hr.handling, hr.grade, hr.grade_cn,
                        hr.owner_role, hr.evidence_count,
                        (SELECT COUNT(*) FROM problem p
                         WHERE p.dimension_cn = hr.dimension || '问题'{sub_extra}) AS in_scope_count
                 FROM handling_rule hr
                 WHERE hr.dimension IN ({ph})
                 ORDER BY hr.evidence_count DESC, hr.grade ASC
                 LIMIT ?""", list(args) + dims + [limit])
    return rows


def report(domain=None, table=None, dimension=None, period=None, compare=False):
    conds, args = [], []
    if domain:
        conds.append("data_domain = ?")
        args.append(domain)
    if table:
        conds.append("table_name = ?")
        args.append(table)
    if dimension:
        conds.append("dimension_cn = ?")
        args.append(dimension)
    cur, prev = cur_prev_ds()
    ds = _to_ds(period)
    conds.append("ds = ?")
    args.append(ds)
    where = ("WHERE " + " AND ".join(conds)) if conds else ""

    total = q1(f"SELECT COUNT(*) AS n FROM problem {where}", args)["n"]
    if total == 0:
        return {"ok": False, "error": "该范围内没有质检问题记录",
                "scope": {"domain": domain, "table": table, "dimension": dimension}}

    strong = q1(f"SELECT COUNT(*) AS n FROM problem {where} "
                f"{'AND' if where else 'WHERE'} rule_strength='STRONG'", args)["n"]

    # ① 问题剖析
    dims = q(f"""SELECT dimension_cn AS dimension, COUNT(*) AS count,
                        SUM(rule_strength='STRONG') AS strong_count
                 FROM problem {where} GROUP BY dimension_cn ORDER BY count DESC""", args)
    for d in dims:
        d["share"] = pct(d["count"], total)
    status = q(f"SELECT problem_status AS status, COUNT(*) AS count FROM problem {where} "
               f"GROUP BY problem_status ORDER BY count DESC", args)

    rules = q(f"""SELECT rule_name, rule_strength, COUNT(*) AS count
                  FROM problem {where} GROUP BY rule_name
                  ORDER BY count DESC LIMIT 8""", args)
    for r in rules:
        r["share"] = pct(r["count"], total)

    top_tables = q(f"""SELECT table_name, table_desc,
                              data_domain AS domain, COUNT(*) AS count,
                              SUM(rule_strength='STRONG') AS strong_count,
                              COUNT(DISTINCT validate_object_name) AS object_count
                       FROM problem {where} GROUP BY table_name
                       ORDER BY count DESC LIMIT 8""", args)
    for t in top_tables:
        t["share"] = pct(t["count"], total)

    # ② 数据影响
    affects = q(f"""SELECT table_name, table_desc,
                           SUM(rule_strength='STRONG') AS strong_count,
                           COUNT(DISTINCT validate_object_name) AS fields,
                           GROUP_CONCAT(DISTINCT dimension_cn) AS dims
                    FROM problem {where}
                    GROUP BY table_name
                    HAVING SUM(rule_strength='STRONG') > 0
                    ORDER BY strong_count DESC LIMIT 6""", args)
    owners = q(f"""SELECT owner_names AS owner, COUNT(*) AS count
                   FROM problem {where} GROUP BY owner ORDER BY count DESC LIMIT 6""", args)
    _sw = f"{'AND' if where else 'WHERE'} rule_strength='STRONG'"
    impact = {
        "strong_rule_problem_count": strong,
        "strong_rule_share": pct(strong, total),
        # 「影响面」口径 = 命中强规则的范围，与下方 strong_rule_tables / lineage_note 一致。
        # 曾误用全量问题计数（16 表 / 154 对象），与 strong=71 不自洽，已修正。
        "affected_table_count": q1(
            f"SELECT COUNT(DISTINCT table_name) AS n FROM problem {where} {_sw}", args)["n"],
        "affected_object_count": q1(
            f"SELECT COUNT(DISTINCT validate_object_name) AS n FROM problem {where} {_sw}", args)["n"],
        # 全量问题覆盖范围（不限强弱），供对照
        "problem_table_count": q1(
            f"SELECT COUNT(DISTINCT table_name) AS n FROM problem {where}", args)["n"],
        "problem_object_count": q1(
            f"SELECT COUNT(DISTINCT validate_object_name) AS n FROM problem {where}", args)["n"],
        "strong_rule_tables": affects,
        "owner_distribution": owners,
        "lineage_note": "本期 POC 未接入血缘接口；此处以「命中强规则的表」近似影响面，"
                        "接入血缘后替换为真实下游链路。",
    }

    # ③ 当前建议
    suggestions = _suggestions(where, args)

    # ④ 环比（两期数据已就绪）
    p_cur = _period_block(ds)
    p_prev = _period_block(prev) if (compare and prev and not (table or dimension)) else None
    ring = None
    if p_prev:
        ring = {
            "period_cn": next((x["period_cn"] for x in period_rows() if not x["is_current"]), "上一期"),
            "problems": {"prev": p_prev["problems"], "cur": p_cur["problems"],
                         "delta": p_cur["problems"] - p_prev["problems"],
                         "rate": pct(p_cur["problems"] - p_prev["problems"], p_prev["problems"])},
            "strong": {"prev": p_prev["strong"], "cur": p_cur["strong"],
                       "delta": p_cur["strong"] - p_prev["strong"],
                       "rate": pct(p_cur["strong"] - p_prev["strong"], p_prev["strong"])},
            "pass_rate": {"prev": p_prev["pass_rate"], "cur": p_cur["pass_rate"],
                          "delta": round(p_cur["pass_rate"] - p_prev["pass_rate"], 1)},
        }

    prow = next((x for x in period_rows() if period_ds(x["period_id"]) == ds), None)
    return {
        "ok": True,
        "scope": {
            "domain": domain or "全部数据域",
            "table": table, "dimension": dimension,
            "period_ds": ds,
            "period": prow["period_cn"] if prow else str(ds),
            "single_period": False,
            "available_periods": [x["period_cn"] for x in period_rows()],
        },
        "part1_problem_analysis": {
            "total": total,
            "by_dimension": dims,
            "by_rule": rules,
            "by_status": status,
            "top_tables": top_tables,
        },
        "part2_data_impact": impact,
        "part3_suggestion": suggestions,
        "part4_ring_comparison": ring,
        "caveats": [
            "本期为 %s；环比基准为%s，两期数据口径一致、可直接比较。" % (
                prow["period_cn"] if prow else ds,
                ("（" + ring["period_cn"] + "）") if ring else "上一期（未取）"),
            "数据量级为样例规模（千级），真实环境为 5 万+ 条，占比结构可参考、绝对数不可直接外推。",
            "质量维度按客户《数据质量检测报告》模板口径呈现（技术原值 STABILITY 统一称「准确性」）。",
            "所有处置建议均来自台账真实工单归纳，按「AI 不改数」原则，任何更正动作须人工确认后由业务系统执行。",
        ],
    }


# 业务名词 → 字段名关键词。用户说的是「联系电话」，库里叫 tax_tel / *_mobile。
# 没这张表，检核对象就只能留 `<目标表>.<目标字段>` 占位符，演示时看着像没做出来。
# 命中后仍要回到 quality_rule 里核对字段真实存在，不凭映射凭空造字段。
FIELD_SYNONYMS = [
    ("联系电话", ["tel", "mobile", "phone"]),
    ("电话号码", ["tel", "mobile", "phone"]),
    ("手机号", ["mobile", "phone"]),
    ("电话", ["tel", "phone"]),
    ("客户名称", ["cust_name", "customer_name", "shop_name"]),
    ("客户名", ["cust_name", "shop_name"]),
    ("门店名称", ["shop_name"]),
    ("地址", ["addr", "address"]),
    ("主键", ["uuid", "id", "code"]),
    ("编码", ["code", "uuid"]),
    ("价格", ["price", "amount"]),
    ("金额", ["amount", "price", "fee"]),
    ("数量", ["quantity", "qty", "num"]),
    ("日期", ["date", "time"]),
    ("状态", ["status", "flag"]),
]


def guess_field(text):
    """从问句里猜字段名关键词，返回 (中文名, [关键词]) 或 (None, [])。"""
    t = text or ""
    for cn, keys in FIELD_SYNONYMS:
        if cn in t:
            return cn, keys
    return None, []


def recommend_rule(problem_description=None, dimension=None, table=None, field=None,
                   domain=None):
    """规则推荐。domain 传入时，参考依据只取该域的同类规则。

    曾有的坑：问「营销域」也会拿全域 1000 条规则算「N 条同类规则采用」，
    域内配置实际只有该域自己的规则 —— 数字对不上，客户一问就露。
    """
    text = " ".join([x for x in [problem_description, dimension, field, table,
                                 domain] if x])
    dim = dimension or detect_dimension(text) or "完整性"
    dom = domain if domain else None

    tpls = TEMPLATE_BY_DIM.get(dim, ("字段有效性校验",))
    tpl = tpls[0]

    # 该模板在规则库中的实际配置分布（参考依据）
    dcond, dargs = ("AND data_domain = ?", [dom]) if dom else ("", [])
    refs = q("""SELECT rule_id, rule_name, table_name, validate_object_name AS object_name,
                       validate_field, threshold_metric, threshold_expr, strength
                FROM quality_rule WHERE template_name = ? %s
                ORDER BY rule_id LIMIT 5""" % dcond, [tpl] + dargs)
    n_tpl = q1("SELECT COUNT(*) AS n FROM quality_rule WHERE template_name=? " + dcond,
               [tpl] + dargs)["n"]
    n_strong = q1("""SELECT COUNT(*) AS n FROM quality_rule
                     WHERE template_name=? AND strength='STRONG' """ + dcond,
                  [tpl] + dargs)["n"]
    fmap = q1("SELECT * FROM feature_rule_map WHERE template_name=?", (tpl,))

    # 建议阈值：优先取同类规则中出现最多的口径
    thr_rows = q("""SELECT threshold_expr, COUNT(*) AS n FROM quality_rule
                    WHERE template_name=? AND threshold_expr IS NOT NULL
                      AND threshold_expr != '' """ + dcond + """
                    GROUP BY threshold_expr ORDER BY n DESC LIMIT 3""", [tpl] + dargs)

    # 目标表 + 字段的候选（若指定了表，给出该表当前已有的同类规则，便于比对去重）
    existing = []
    if table:
        existing = q("""SELECT rule_name, validate_field, threshold_expr, strength
                        FROM quality_rule WHERE table_name=? AND template_name=?""",
                     (table, tpl))

    # 字段未指定时，从该表已有同类规则里猜一个候选字段
    field_hint = None
    if not field and table:
        hr = q("""SELECT validate_field, COUNT(*) AS n FROM quality_rule
                  WHERE table_name=? AND template_name=? AND validate_field != ''
                  GROUP BY validate_field ORDER BY n DESC LIMIT 1""", (table, tpl))
        if hr:
            field_hint = hr[0]["validate_field"]

    # 业务名词 → 真实字段：优先级 field 参数 > 问句里的业务名词 > 表内同类规则众数
    cn_name, keys = guess_field(problem_description)
    syn_field = None
    if not field and keys:
        where_keys = " OR ".join(["LOWER(validate_field) LIKE ?"] * len(keys))
        kargs = ["%" + k + "%" for k in keys]
        conds = [where_keys]
        a2 = list(kargs)
        if table:
            conds.append("table_name = ?")
            a2.append(table)
        if dim:
            conds.append("dimension_cn = ?")
            a2.append(dim + "问题" if not dim.endswith("问题") else dim)
        if dom:
            conds.append("data_domain = ?")
            a2.append(dom)
        rows = q("""SELECT validate_field, table_name, COUNT(*) AS n
                    FROM quality_rule WHERE """ + " AND ".join(conds) + """
                    GROUP BY validate_field, table_name ORDER BY n DESC LIMIT 5""", a2)
        if rows:
            syn_field = rows[0]["validate_field"]
            table = table or rows[0]["table_name"]

    use_field = field or syn_field or field_hint
    obj = ".".join([x for x in [table, use_field] if x]) or "<目标表>.<目标字段>"
    if table:
        rule_name = "%s_%s_%s" % (table.split(".")[-1], use_field or "目标字段", tpl)
    else:
        rule_name = "%s_%s" % (tpl, use_field or "目标字段")

    field_basis = None
    if field:
        field_basis = "用户指定字段"
    elif syn_field:
        field_basis = ("按业务名词「%s」在规则库中匹配到真实字段 %s（同名字段共 %d 条规则在用）"
                       % (cn_name, syn_field, len(rows)))
    elif field_hint:
        field_basis = "取该表同类规则中出现最多的字段"

    raw_thr = thr_rows[0]["threshold_expr"] if thr_rows else None
    if not raw_thr and fmap:
        raw_thr = fmap.get("suggested_threshold")
    return {
        "ok": True,
        "detected_dimension": dim,
        "input_summary": {"problem_description": problem_description, "table": table,
                          "field": field},
        "suggested_rule": {
            "rule_name": rule_name,
            "dimension": dim,
            "template_name": tpl,
            "validate_object": obj,
            "target_field": use_field,
            "field_source": field_basis or "未指定",
            "logic": _logic_text(dim, use_field),
            "threshold_suggestion": raw_thr,
            "threshold_cn": threshold_cn(raw_thr),
            "threshold_basis": ["%s（%s；%d 条同类规则采用）"
                                % (r["threshold_expr"], threshold_cn(r["threshold_expr"]), r["n"])
                                for r in thr_rows],
            "strength_suggestion": "STRONG" if dim in ("唯一性", "一致性") else "WEAK",
            "strength_reason": "同类规则在库中强规则占比 %.0f%%" % (
                pct(n_strong, n_tpl) if n_tpl else 0),
        },
        "reference": {
            "same_template_total": n_tpl,
            "same_template_strong_share": pct(n_strong, n_tpl),
            "in_library_examples": refs,
            "feature_map": ({"data_feature": fmap["data_feature"],
                             "note": fmap["note"]} if fmap else None),
        },
        "existing_rules_on_target": existing,
        "compliance": "按「AI 不改数」原则，本建议仅供确认，落地执行需业务系统完成。",
    }


def _logic_text(dim, field):
    f = field or "该字段"
    return {
        "完整性": f"统计 {f} 的空值（NULL）条数，空值率 = 空值条数 ÷ 总条数，超过阈值即判为不符合。",
        "唯一性": f"按主键/业务唯一键分组，统计重复组数，正常率 = 去重后条数 ÷ 总条数。",
        "一致性": "比对两表同一指标在同一时间区间的汇总值，差异率 = |A-B| ÷ A。",
        "有效性": f"校验 {f} 的取值是否落在字典枚举/格式允许范围内，不符合条数占比。",
        "及时性": "校验时间字段先后顺序（如结束时间不早于开始时间），或分区数据量是否为 0。",
        "稳定性": "按日环比计算表/分区数据量波动，超出历史基线区间即告警。",
        "准确性": f"将 {f} 与权威源比对，统计不一致条数与偏差幅度。",
        "规范性": f"校验 {f} 的命名、注释、类型长度是否符合元数据管理规范。",
    }.get(dim, f"对 {f} 执行 {dim} 类校验。")


# 检索用停用词（避免整句 LIKE）
# ------------------------------------------------------------------ 意图路由聚合
DOMAIN_ALIASES = [
    ("营销", r"营销|零售|客户|销售|订单|品规|品牌|投放|终端|进销存|购销|价格|档位"),
    ("专卖", r"专卖|许可证|案件|涉案|真烟|外流"),
    ("物流", r"物流|仓储"),
    ("综管", r"综管|组织|机构|部门|人事|日志|权限"),
    ("一号工程", r"一号工程|下行|在途"),
]

# 台账「所属主题域」里的主体名：营销域的主体叫「智慧营销」，综管域就叫「综管」。
# 报告标题按主体来写；工单表的域字段存的也是这类原值（智慧营销 / DWD_智慧营销 / 营销…）。
DOMAIN_SUBJECT = {"营销": "智慧营销", "专卖": "智慧专卖", "物流": "智慧物流",
                  "综管": "综管", "一号工程": "一号工程"}


def domain_subject(domain):
    """域短名 → 报告标题用的主体名。"""
    d = (domain or "").replace("域", "")
    return DOMAIN_SUBJECT.get(d, d or "数据")


def detect_domain(text):
    if not text:
        return None
    for d, pat in DOMAIN_ALIASES:
        if re.search(pat, text):
            return d
    return None


# 期间别名：中文月份写法 → 月号
_CN_MONTH = {"一": 1, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6,
             "七": 7, "八": 8, "九": 9, "十": 10, "十一": 11, "十二": 12}


def detect_period(text):
    """从问句里识别期次，返回 period_id（YYYYMM）或 None（= 用当期）。

    支持：2026年8月 / 2026-08 / 202608 / 8月 / 八月 / 上个月 / 上月 / 上期 /
          上一期 / 本月 / 当期 / 这个月。识别不出就返回 None，由调用方取当期。
    只在库内已存在的期次里匹配，避免凭空造出一个不存在的期次。
    """
    if not text:
        return None
    t = str(text)
    rows = period_rows()
    ids = [r["period_id"] for r in rows]
    cur = next((r["period_id"] for r in rows if r["is_current"]), ids[0] if ids else None)
    prev = next((r["period_id"] for r in rows if not r["is_current"]), None)

    # 1) 带年份：2026年8月 / 2026-08 / 2026/8 / 202608
    m = re.search(r"(20\d{2})\s*[年\-/.]\s*(\d{1,2})\s*月?", t)
    if m:
        pid = int(m.group(1)) * 100 + int(m.group(2))
        if pid in ids:
            return pid
    m = re.search(r"\b(20\d{2})(0[1-9]|1[0-2])\b", t)
    if m:
        pid = int(m.group(1)) * 100 + int(m.group(2))
        if pid in ids:
            return pid

    # 2) 相对说法
    if re.search(r"上个月|上月|上个期|上期|上一期|前一个?月|前一?期", t):
        return prev
    if re.search(r"本月|当期|这个月|本期|当前期", t):
        return cur

    # 3) 不带年份的月份：8月 / 八月（年份取当期所在年）
    y = (cur // 100) if cur else None
    m = re.search(r"(\d{1,2})\s*月份?", t)
    if m:
        mo = int(m.group(1))
        pid = y * 100 + mo if y and 1 <= mo <= 12 else None
        if pid in ids:
            return pid
    m = re.search(r"(十[一二]|[一二三四五六七八九十])月", t)
    if m:
        mo = _CN_MONTH.get(m.group(1))
        pid = y * 100 + mo if (y and mo) else None
        if pid in ids:
            return pid
    return None


def _bigrams(s):
    s = re.sub(r"[^\u4e00-\u9fa5A-Za-z0-9]", "", s or "")
    return {s[i:i + 2] for i in range(len(s) - 1)}


def _all_tables():
    """问题清单与规则库里的全部表（去重）。"""
    return q("""SELECT table_name, table_desc, SUM(n) AS n FROM (
                    SELECT table_name, table_desc, COUNT(*) AS n FROM problem
                    GROUP BY table_name
                    UNION ALL
                    SELECT table_name, table_description AS table_desc, COUNT(*) AS n
                    FROM quality_rule GROUP BY table_name)
                GROUP BY table_name ORDER BY n DESC""")


def match_table(text):
    """在库内找出用户可能指代的表：先精确（表名/描述包含），再 2-gram 模糊。"""
    if not text:
        return None
    rows = _all_tables()

    # ① 精确：表短名或表描述被原样包含
    for r in rows:
        tn, td = r["table_name"], r["table_desc"] or ""
        short = tn.split(".")[-1]
        if short and short.lower() in text.lower():
            return {"table_name": tn, "table_desc": td, "problem_count": r["n"],
                    "match": "exact_table"}
    for r in rows:
        td = r["table_desc"] or ""
        if len(td) >= 4 and td in text:
            return {"table_name": r["table_name"], "table_desc": td,
                    "problem_count": r["n"], "match": "exact_desc"}

    # ② 模糊：抽取「…表」短语，用 2-gram 覆盖率打分
    best = None
    for kw in re.findall(r"[\u4e00-\u9fa5A-Za-z0-9_]{2,20}表", text):
        kb = _bigrams(kw)
        if not kb:
            continue
        for r in rows:
            db = _bigrams(r["table_desc"] or "")
            if not db:
                continue
            score = len(kb & db) / len(kb)
            if score >= 0.5 and (best is None or score > best[0]):
                best = (score, r)
    if best:
        r = best[1]
        return {"table_name": r["table_name"], "table_desc": r["table_desc"],
                "problem_count": r["n"], "match": "fuzzy", "score": round(best[0], 2)}
    return None


def ask(query, intent_hint=None):
    """统一入口：服务端做意图路由 + 取数，模型只负责成文。

    对应 V3「一个入口 + 意图路由」；路由规则确定、可审计，不依赖模型工具调用。
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query 不能为空"}

    # 意图判定（顺序敏感）
    # 【分类器预留】上游若已判出意图（Dify 问题分类器 / dev 模型），可传 intent_hint 覆盖
    # 规则判定；只接受白名单取值，非法或未传则退回规则。将来换分类器不必改服务端与工作流。
    hint = (intent_hint or "").strip().lower()
    if hint in ("report", "rule", "analysis", "compare", "out_of_scope"):
        return _ask_by_intent(query, hint, "上游分类器判定")

    # 注意：「规则执行情况」同时含「规则」与「执行」，必须先判强执行特征，
    # 否则会被 rule 分支抢先命中（曾出现该误路由）。强特征用短语而非单字，避免误伤。
    # 2026-10-20 重排：「默认 report」是「问啥都出报告」的根因（「哪些表问题最多」
    # 「质量问题怎么处置」实测都被判成 report）。现在先判非质检域 → 再判执行/案例/
    # 规则 → 分析类需命中明确的问数特征 → 只有明确要「报告」才走 report。
    intent, reason = None, ""
    if _out_of_scope(query):
        intent, reason = "out_of_scope", "命中闲聊/越界特征且不含质检业务词"
    elif re.search(r"执行情况|执行状态|执行结果|执行进度|执行得怎么样|跑得怎么样|跑了多少|"
                   r"跑了几次|通过率|失败率|完成情况|有没有执行|有没有跑|"
                   r"(哪些|哪条|哪个|哪几).{0,4}规则.{0,4}(失败|没通过|异常|报错)|"
                   r"失败.{0,4}规则|没通过的规则|报错的规则", query):
        # 2026-10-20 按用户要求：执行情况问答下线。
        # 用户在问真实存在的功能，不能当闲聊敷衍 → 明确说明该能力本期不提供，
        # 并指向仍可用的「出报告」（报告里本来就含规则通过率与执行统计）。
        intent, reason = "retired_exec", "问的是执行情况，该问答能力已按需求下线"
    elif re.search(r"规则|校验|检核|监测|加个|加一条|配置一条|生成一条|怎么配", query):
        intent, reason = "rule", "命中「规则/校验」关键词"
    elif re.search(r"以前|历史|案例|同类|怎么处理|怎么解决|处置过|怎么处置|如何处置|"
                   r"处置建议|怎么整改|如何整改|根因", query):
        intent, reason = "retired_case", "问的是历史案例/处置，该问答能力已按需求下线"
    elif re.search(r"进度|执行|失败", query):
        intent, reason = "retired_exec", "命中「执行/失败」关键词，该问答能力已按需求下线"
    elif re.search(r"对比|环比|同比|趋势|变化|两个?月|两期|上月.*本月|比上月", query):
        intent, reason = "compare", "命中对比/趋势特征（跨期，不出报告）"
    elif _report_feature(query) and not _analysis_feature(query):
        intent, reason = "report", "命中「报告/检测报告」等明确出报告特征"
    elif _analysis_feature(query):
        intent, reason = "analysis", "命中排名/分布/哪些表等问数特征（不出报告）"
    else:
        intent, reason = "unsupported", "未命中任何质检业务特征"

    return _ask_by_intent(query, intent, reason)


def _ask_by_intent(query, intent, reason):
    """按已确定的意图取数。intent 由规则判定，或由上游分类器传入。"""
    dim = detect_dimension(query)
    domain = detect_domain(query)
    table = match_table(query)
    period = detect_period(query)

    out = {"ok": True, "query": query, "intent": intent, "intent_reason": reason,
           "detected": {"dimension": dim, "domain": domain,
                        "table": table["table_name"] if table else None,
                        "period": period}}

    if intent == "rule":
        field = None
        m = re.search(r"([a-z][a-z0-9_]{2,40})\s*(?:字段|列)", query)
        if m:
            field = m.group(1)
        out["payload"] = recommend_rule(query, dim,
                                        table["table_name"] if table else None, field,
                                        domain=domain)
    elif intent in ("retired_exec", "retired_case"):
        out["payload"] = retired_hint(intent)
    elif intent == "analysis":
        out["payload"] = analyze(domain, table["table_name"] if table else None,
                                 dim, period)
    elif intent == "compare":
        out["payload"] = compare(domain, table["table_name"] if table else None,
                                 dim, period)
    elif intent == "out_of_scope":
        out["payload"] = capability_hint("out_of_scope", query)
        out["assumed"] = None
        return out
    elif intent == "unsupported":
        out["payload"] = capability_hint("unsupported", query)
        out["assumed"] = None
        return out
    else:
        if not domain:
            out["assumed"] = "未识别到数据域，默认以「营销域」为例作答；请提示用户可指定数据域。"
            domain = "营销"
        out["payload"] = report(domain, table["table_name"] if table else None, None,
                                period=period, compare=True)

    return out


# ------------------------------------------------------------------ 意图路由辅助
# 质检业务词：出现任意一个就说明用户确实在问质检，不走越界分支。
# 曾踩的坑：问「质量问题怎么处置」含「怎么处置」，但当时没有 analysis 概念，
# 直接落进 report 默认分支 —— 出报告没问题，出的是「没被要求的那份」。
_BIZ_WORDS = (r"质量|质检|检核|校验|规则|问题|空值|重复|主键|完整性|唯一性|有效性|"
              r"准确性|一致性|及时性|维度|报告|执行|通过率|失败|异常|数源|数据|表|字段|"
              r"台账|处置|整改|营销|专卖|物流|综管|域")
_OUT_WORDS = (r"天气|气温|下雨|股票|彩票|笑话|讲故事|唱歌|翻译|写诗|写代码|编程|"
              r"你是谁|你叫什么|几点|日期是几号|算一下|计算|等于几|1\+1|心情")


def _out_of_scope(q):
    """越界判定：有闲聊/无关特征、且完全没有质检业务词。"""
    return bool(re.search(_OUT_WORDS, q)) and not re.search(_BIZ_WORDS, q)


def _report_feature(q):
    """明确要报告：报告/检测报告/出/生成/导出/下载 + 报告类词。"""
    return bool(re.search(r"报告", q)) or bool(
        re.search(r"(出|生成|导出|下载|来|做|整)一?份", q))


def _analysis_feature(q):
    """问数类特征：排名/分布/哪些表/哪些字段/最多/最少/趋势/对比。"""
    return bool(re.search(
        r"哪些表|哪些字段|哪张表|哪个表|排行|排名|最多|最少|最高|最低|前\s*\d|TOP|"
        r"分布|集中在|主要是|情况如何|怎么样|多少|几个|几条|统计|汇总|对比|环比|"
        r"趋势|变化|占比|构成|列表|清单|列一下|列出来", q, re.I))


# 对客能力清单：**只有三项**（2026-10-20 按用户要求下线「规则执行情况」
# 与「历史案例与处置」）。界面上任何地方出现的能力说明都必须以此为唯一来源。
CAPABILITIES = [
    {"name": "生成质检报告", "example": "给我出一份营销域的数据质量检测报告"},
    {"name": "推荐质检规则", "example": "联系电话空值，帮我生成一条校验规则"},
    {"name": "质检情况追问", "example": "哪些表质量问题最多"},
]


def capability_hint(kind, query=None):
    """能力边界说明。模型不得自行编造，只能照此成文。"""
    if kind == "out_of_scope":
        r = "这个问题不属于数据质量检核的范围"
    else:
        r = "没能识别出您具体想了解哪一类质检信息"
    return {"ok": True, "kind": kind, "reason": r, "capabilities": CAPABILITIES,
            "note": "请说明能做什么、并给出 capabilities 里的示例问题，不要生成报告。"}


def retired_hint(kind):
    """用户问的是**已下线**的功能：不能说"不知道"，要说明去路。

    这两项原来各有一条问答链路，本期按需求下线。报告里仍含规则通过率、
    执行统计与处置建议，《问题清单》附录仍有根因描述 —— 这些话要讲清楚，
    否则用户会以为功能坏了。
    """
    if kind == "retired_exec":
        what = "规则执行情况的单独问答"
        fallback = ("规则的通过率与执行统计已包含在质检报告里，"
                    "可以说「给我出一份XX域的数据质量检测报告」查看。")
    else:
        what = "历史案例与处置方式的追溯问答"
        fallback = ("处置建议与典型问题已在报告的「问题处理与管理成效」一章"
                    "与《问题清单》附录中体现。")
    return {"ok": True, "kind": kind, "retired": what, "fallback": fallback,
            "capabilities": CAPABILITIES,
            "note": "请明确告知该问答能力本期不提供，按 fallback 指路，"
                    "同时可列出其余能力；不要编造执行数据或案例。"}


def analyze(domain=None, table=None, dimension=None, period=None):
    """追问类取数：按当前范围给出可解释的分布，不生成报告文件。

    dimension 传入时形如「完整性」（不带「问题」后缀），与 problem.dimension_cn
    的「完整性问题」用 LIKE 对齐。
    """
    ds, cond, args = _scope(domain, period)
    if table:
        cond += " AND table_name = ?"
        args.append(table)
    if dimension:
        cond += " AND dimension_cn LIKE ?"
        args.append(dimension.replace("问题", "") + "%")

    total = q1(f"SELECT COUNT(*) AS n FROM problem {cond}", args)["n"]
    prow = next((x for x in period_rows() if period_ds(x["period_id"]) == ds), None)

    by_domain = q(f"""SELECT data_domain AS domain, COUNT(*) AS problem_count,
                             COUNT(DISTINCT table_name) AS table_count
                      FROM problem {cond}
                      GROUP BY data_domain ORDER BY problem_count DESC""", args)
    by_dim = q(f"""SELECT dimension_cn AS dimension, COUNT(*) AS problem_count,
                          SUM(rule_strength='STRONG') AS strong_count
                   FROM problem {cond}
                   GROUP BY dimension_cn ORDER BY problem_count DESC""", args)
    by_table = q(f"""SELECT table_name, table_desc, data_domain AS domain,
                            COUNT(*) AS problem_count,
                            SUM(rule_strength='STRONG') AS strong_count
                     FROM problem {cond}
                     GROUP BY table_name ORDER BY problem_count DESC LIMIT 5""", args)
    by_field = q(f"""SELECT table_name, validate_object_name AS object_name,
                            problem_desc, COUNT(*) AS problem_count
                     FROM problem {cond}
                     GROUP BY table_name, validate_object_name, problem_desc
                     ORDER BY problem_count DESC LIMIT 5""", args)
    by_status = q(f"""SELECT problem_status AS status, COUNT(*) AS count
                      FROM problem {cond}
                      GROUP BY problem_status ORDER BY count DESC""", args)
    for r in by_dim:
        r["share"] = pct(r["problem_count"], total)

    return {"ok": True,
            "scope": {"domain": domain or "全部数据域", "table": table,
                      "dimension": dimension, "period_ds": ds,
                      "period": prow["period_cn"] if prow else str(ds)},
            "totals": {"problem_count": total,
                       "table_count": q1(f"SELECT COUNT(DISTINCT table_name) AS n "
                                         f"FROM problem {cond}", args)["n"],
                       "strong_count": q1(f"SELECT COUNT(*) AS n FROM problem {cond} "
                                          f"AND rule_strength='STRONG'", args)["n"]},
            "by_domain": by_domain, "by_dimension": by_dim,
            "top_tables": by_table, "top_issues": by_field, "by_status": by_status,
            "caveats": ["样例规模：本地 POC 为千级数据，真实环境 5 万+ 条。"]}


def compare(domain=None, table=None, dimension=None, period=None):
    """跨期对比：当期 vs 上一期，同一范围、同一组过滤条件。

    口径提醒（来自《数据服务API清单-v3》第二节）：
      · **问题数、强规则命中数可加**，直接比；
      · **规则通过率不可加**：必须 sum(pass)/sum(checked) 重算，
        不能把两期的通过率做算术平均（真值会差几个百分点）。
    """
    ds, cond, args = _scope(domain, period)
    rows = period_rows()
    ids = sorted([r["period_id"] for r in rows], reverse=True)
    cur = next((r["period_id"] for r in rows if r["is_current"]), ids[0])
    prev = next((i for i in ids if i < cur), None)

    def side(pid):
        if not pid:
            return None
        d = period_ds(pid)
        c, a = "WHERE ds = ?", [d]
        if domain:
            c += " AND data_domain = ?"
            a.append(domain)
        if table:
            c += " AND table_name = ?"
            a.append(table)
        if dimension:
            c += " AND dimension_cn LIKE ?"
            a.append(dimension.replace("问题", "") + "%")
        pc = q1(f"SELECT COUNT(*) AS n FROM problem {c}", a)["n"]
        st = q1(f"SELECT COUNT(*) AS n FROM problem {c} AND rule_strength='STRONG'", a)["n"]
        pid6 = int(str(pid)[:6])
        ec = "WHERE ds/100 = ?"
        ea = [pid6]
        if domain:
            ec += " AND data_domain = ?"
            ea.append(domain)
        checked = q1(f"SELECT COUNT(*) AS n FROM rule_exec {ec} AND is_pass IN ('Y','N')",
                     ea)["n"]
        passed = q1(f"SELECT COUNT(*) AS n FROM rule_exec {ec} AND is_pass='Y'", ea)["n"]
        return {"period_id": pid, "ds": d,
                "period_cn": next((x["period_cn"] for x in rows
                                   if x["period_id"] == pid), str(pid)),
                "problem_count": pc, "strong_count": st,
                "checked": checked, "passed": passed,
                "pass_rate": pct(passed, checked)}

    c_, p_ = side(period or cur), side(prev)
    out = {"ok": True, "scope": {"domain": domain or "全部数据域", "table": table,
                                 "dimension": dimension},
           "cur": c_, "prev": p_, "compare_available": bool(c_ and p_),
           "rule": "通过率按 sum(passed)/sum(checked) 重算，不对两期通过率取平均。",
           "caveats": ["样例两期数据，仅用于演示对比口径；真实环境按期次扩展。"]}
    if c_ and p_:
        out["delta"] = {
            "problem_count": c_["problem_count"] - p_["problem_count"],
            "problem_rate": round((c_["problem_count"] - p_["problem_count"]) * 100.0
                                  / p_["problem_count"], 1)
            if p_["problem_count"] else None,
            "strong_count": c_["strong_count"] - p_["strong_count"],
            "pass_rate_pp": (round(c_["pass_rate"] - p_["pass_rate"], 1)
                             if (c_["pass_rate"] is not None
                                 and p_["pass_rate"] is not None) else None),
        }
    return out


# ================================================================== 报告模板数据
# 模板「质量评估维度」表为静态知识（来自客户《数据质量检测报告》模板），原样固化。
DOC_DIM_DEFS = [
    ("唯一性", "主键检测", "计算指定数据集中重复主键数。"),
    ("完整性", "NULL或空字符串检查", "计算指定数据集中指定字段取值为NULL或空字符串的记录数。"),
    ("完整性", "覆盖度检查", "根据设置的基数，统计目标表目标字段的覆盖度（地域覆盖度、时间覆盖度等）。"),
    ("有效性", "正则表达式检查", "计算指定数据集中指定字段取值与给定正则表达式不匹配的记录数。"),
    ("有效性", "枚举值检查", "计算指定数据集中指定字段取值不在给定枚举值列表中的记录数。"),
    ("有效性", "数值范围检查", "计算指定数据集中指定字段取值不在给定范围内的记录数。"),
    ("准确性", "跨表数据值准确性检查", "检查两个数据集数值是否相同。"),
    ("准确性", "日期准确性检查", "计算指定数据集中指定字段取值与给定日期（正则表达式）不匹配的记录数。"),
    ("一致性", "存在一致性检查", "计算源表指定字段不存在与目标表对应字段的记录数。（表间关联字段、主表明细表存在一致性）"),
    ("一致性", "逻辑一致性检查", "检查字段值与实际统计结果是否相同。"),
    ("一致性", "跨表数据量对账", "检查两个数据集数量是否相同。（数据量对账）"),
    ("一致性", "元数据稽核", "检查源端与入湖数据的字段数据类型是否一致。"),
    ("及时性", "及时性检查", "检查单个数据集是否满足SLA时间要求，在需要的时间点获取或者提供数据。"),
]
# 模板维度顺序（库内 dimension_cn 带「问题」后缀）；短名用于表格展示
DIM_ORDER = ["完整性问题", "唯一性问题", "有效性问题", "准确性问题", "一致性问题", "及时性问题"]
SHORT = lambda x: (x or "").replace("问题", "")
STATUS_COL = {"VERIFY": "处理中", "NEW": "待处理", "IGNORE": "无需处理", "FINISHED": "已解决"}
STATUS_CN = {"NEW": "待处理", "VERIFY": "处理中", "IGNORE": "无需处理", "FINISHED": "已解决"}


def report_data(domain="营销", period=None):
    """按客户《数据质量检测报告》模板，产出渲染所需的全量结构化数据。

    全部数字由 SQL 确定性计算，脚本只负责拼装，不做任何推测。
    """
    cur, prev = cur_prev_ds()
    ds = _to_ds(period)
    prow = next((x for x in period_rows() if period_ds(x["period_id"]) == ds), None)
    pprow = next((x for x in period_rows() if not x["is_current"]), None)
    dlabel = (domain or "营销").replace("域", "") + "域"
    subject = domain_subject(domain)

    # --- 总体概况：规则库规模（严格只取该域） ---
    rtot = q1("SELECT COUNT(*) AS n FROM quality_rule WHERE data_domain = ?",
              (domain,))["n"]
    ptot = q1("SELECT COUNT(*) AS n FROM problem WHERE ds = ? AND data_domain = ?",
              (ds, domain))["n"]
    running = q1("""SELECT COUNT(DISTINCT rule_id) AS n FROM rule_exec
                    WHERE ds/100 = ? AND data_domain = ?""", (ds // 100, domain))["n"]

    # --- 评估范围（该域本期实际被检出的表 / 对象）---
    scope_t = q1("""SELECT COUNT(DISTINCT table_name) AS n FROM problem
                    WHERE ds = ? AND data_domain = ?""", (ds, domain))["n"]
    scope_o = q1("""SELECT COUNT(DISTINCT validate_object_name) AS n FROM problem
                    WHERE ds = ? AND data_domain = ?""", (ds, domain))["n"]
    scope_r = q1("""SELECT COUNT(DISTINCT rule_id) AS n FROM problem
                    WHERE ds = ? AND data_domain = ?""", (ds, domain))["n"]

    # --- 表2：评估维度 / 制定规则数 / 常态化运行规则数（严格只取该域） ---
    made = {r["dimension_cn"]: r["n"] for r in q(
        """SELECT dimension_cn, COUNT(*) AS n FROM quality_rule
           WHERE data_domain = ? GROUP BY 1""", (domain,))}
    run = {r["dimension_cn"]: r["n"] for r in q(
        """SELECT dimension_cn, COUNT(DISTINCT rule_id) AS n FROM rule_exec
           WHERE ds/100 = ? AND data_domain = ? GROUP BY 1""", (ds // 100, domain))}
    rule_stats = [{"dimension": SHORT(d), "made": made.get(d, 0), "running": run.get(d, 0)}
                  for d in DIM_ORDER]
    rule_stats.append({"dimension": "总计",
                       "made": sum(x["made"] for x in rule_stats),
                       "running": sum(x["running"] for x in rule_stats)})

    # --- 表3：问题分类 × 状态 ---
    grid = {d: {c: 0 for c in ["处理中", "待处理", "无需处理", "已解决"]} for d in DIM_ORDER}
    for r in q("""SELECT dimension_cn AS dim, problem_status AS st, COUNT(*) AS n
                  FROM problem WHERE ds = ? AND data_domain = ?
                  GROUP BY 1, 2""", (ds, domain)):
        col = STATUS_COL.get(r["st"])
        if col and r["dim"] in grid:
            grid[r["dim"]][col] += r["n"]
    prob_by_dim = []
    for d in DIM_ORDER:
        g = grid[d]
        prob_by_dim.append({"dimension": SHORT(d), "doing": g["处理中"], "pending": g["待处理"],
                            "ignore": g["无需处理"], "done": g["已解决"],
                            "total": sum(g.values())})
    prob_by_dim.append({"dimension": "总计",
                        "doing": sum(x["doing"] for x in prob_by_dim),
                        "pending": sum(x["pending"] for x in prob_by_dim),
                        "ignore": sum(x["ignore"] for x in prob_by_dim),
                        "done": sum(x["done"] for x in prob_by_dim),
                        "total": sum(x["total"] for x in prob_by_dim)})

    # --- 主要问题说明：按责任方把处置规则分组 ---
    # 支撑例数不再用全库 evidence_count，改为「该域本期、同维度的真实问题数」，
    # 保证报告里每个数字都只来自该域的问题（台账「所属主题域」口径）。
    dim_cnt = {r["dimension_cn"]: r["n"] for r in q(
        """SELECT dimension_cn, COUNT(*) AS n FROM problem
           WHERE ds = ? AND data_domain = ? GROUP BY 1""", (ds, domain))}

    def _dom_cnt(rule):
        return dim_cnt.get((rule["dimension"] or "") + "问题", 0)

    rules_all = q("SELECT * FROM handling_rule ORDER BY evidence_count DESC")

    def _focus_items(pool):
        pool = [x for x in pool if _dom_cnt(x) > 0]
        pool.sort(key=lambda x: -_dom_cnt(x))
        return ["%s 整改措施：%s（该域同类问题 %d 例）" % (x["symptom"], x["handling"], _dom_cnt(x))
                for x in pool[:6]]

    src_side = [x for x in rules_all if "业务" in (x["owner_role"] or "")]
    dev_side = [x for x in rules_all if "业务" not in (x["owner_role"] or "")]
    focus = [
        {"title": "%s数源问题" % dlabel.replace("域", ""),
         "items": _focus_items(src_side)},
        {"title": "数据开发问题",
         "items": _focus_items(dev_side)},
    ]

    # --- 未解决问题：台账中未闭环的工单（严格只取该域） ---
    # 工单域字段是「所属主题域」原值（智慧营销 / DWD_智慧营销 / 营销…），
    # 用域短名做包含匹配即可同时命中这些写法；综管等无工单的域自然为空。
    unresolved = [{"no": r["ticket_no"], "table": r["table_cn"] or r["table_en"],
                   "desc": r["problem_desc"], "status": r["status"],
                   "owner": r["owner"], "cause": r["cause_solution"]}
                  for r in q("""SELECT * FROM problem_ticket
                                WHERE status IN ('处理中', '待处理')
                                  AND (domain_l1 LIKE ? OR domain_l2 LIKE ?)
                                ORDER BY submit_date LIMIT 12""",
                             ("%" + domain + "%", "%" + domain + "%"))]

    # --- 问题整改方法：按责任方归纳（只保留本域确有对应问题的维度） ---
    rm = {}
    for x in rules_all:
        if _dom_cnt(x) <= 0:
            continue
        k = x["owner_role"] or "其他"
        rm.setdefault(k, []).append(x)
    rectify = [{"owner": k, "count": len(v),
                "items": [y["handling"] for y in v[:3]]}
               for k, v in sorted(rm.items(), key=lambda kv: -len(kv[1]))[:5]]

    # --- 质量管理成效：环比 ---
    # 注意：若用户显式要的就是「上一期」（ds == prev），不能拿它跟自己做环比
    # （会算出全 0 的假环比）→ 此时视为无可比期次，宁可不出数字也不出错的数字。
    pc = _period_block(ds, domain)
    prev_ds = prev if (prev and prev != ds) else None
    pp = _period_block(prev_ds, domain) if prev_ds else None
    effect = {"cur": pc, "prev": pp,
              "ring_available": bool(pp),
              "cur_cn": prow["period_cn"] if prow else str(ds),
              "prev_cn": pprow["period_cn"] if (pprow and prev_ds) else "无可比期次"}
    if pp and pp["problems"]:
        effect["delta"] = {
            "problems": pc["problems"] - pp["problems"],
            "problems_rate": pct(pc["problems"] - pp["problems"], pp["problems"]),
            "strong": pc["strong"] - pp["strong"],
            "strong_rate": pct(pc["strong"] - pp["strong"], pp["strong"]),
            "pass_rate": round(pc["pass_rate"] - pp["pass_rate"], 1),
        }

    # --- 附录A 问题清单（强规则 / 未处理优先，封顶 200 行）---
    APPENDIX_LIMIT = 200
    rows = q("""SELECT * FROM problem WHERE ds = ? AND data_domain = ?
                ORDER BY (rule_strength='STRONG') DESC,
                         (problem_status IN ('NEW','VERIFY')) DESC, problem_object_id
                LIMIT ?""", (ds, domain, APPENDIX_LIMIT))
    hand_by_dim = {}
    for x in rules_all:
        hand_by_dim.setdefault(x["dimension"], x["handling"])
    appendix = [{
        "table_en": r["table_name"],
        "desc": r["problem_desc"] or ("%s：%s" % (r["validate_object_name"], r["rule_name"])),
        "submitter": r["submitter"] or "系统",
        "submit_date": (r["submit_time"] or "")[:10],
        "status_cn": STATUS_CN.get(r["problem_status"], r["problem_status"]),
        "owner": r["owner_names"] or "",
        "category": r["dimension_cn"],
        "cause": hand_by_dim.get((r["dimension_cn"] or "").replace("问题", ""), ""),
        "actual_date": "",
    } for r in rows]

    return {
        "ok": True,
        "doc": {
            "project": "企业数据中台建设项目（示例）",
            "project_no": "ZDA2022-ZY-045",
            "title": "%s数据质量检测报告" % subject,
            "author": "某科技有限公司",
            "date_cn": (prow["period_cn"] if prow else str(ds)).replace(" ", ""),
            "domain_cn": dlabel,
        },
        # 无可比期次时 prev 必须为 None，否则会出现「上一期 = 本期」的自我环比假象
        "period": {"current": prow, "prev": (pprow if prev_ds else None)},
        "overview": {"rule_total": rtot, "problem_total": ptot, "running_rules": running,
                     "table_total": q1("""SELECT COUNT(DISTINCT table_name) AS n FROM quality_rule
                                          WHERE data_domain = ?""", (domain,))["n"],
                     "object_total": q1("""SELECT COUNT(DISTINCT validate_object_name) AS n FROM quality_rule
                                           WHERE data_domain = ?""", (domain,))["n"],
                     "rule_count_scope": scope_r, "table_count_scope": scope_t,
                     "object_count_scope": scope_o},
        "dim_defs": [{"dimension": a, "category": b, "desc": c} for a, b, c in DOC_DIM_DEFS],
        "dim_rule_stats": rule_stats,
        "problem_by_dim": prob_by_dim,
        "focus": focus,
        "unresolved": unresolved,
        "rectify": rectify,
        "effect": effect,
        "appendix_meta": {"total_in_scope": ptot, "shown": len(appendix),
                          "strong_in_scope": q1("""SELECT COUNT(*) AS n FROM problem
                                                   WHERE ds=? AND data_domain=?
                                                   AND rule_strength='STRONG'""",
                                                (ds, domain))["n"]},
        "appendix": appendix,
        "caveats": [
            "样例规模：本地 POC 为千级数据，真实环境 5 万+ 条，占比可参考、绝对数不可外推。",
            "血缘未接入：影响面以「命中强规则的表」近似，非真实下游链路。",
            "「元数据稽核」「数据对账」仅为模板章节占位，本期样例数据未单独覆盖。",
        ],
    }


# ------------------------------------------------------------------ 报告文件落盘
OUT_DIR = os.path.join(HERE, "out")


def report_pack(query, intent_hint=None):
    """一站式入口：意图路由 + 取数；若为质检报告意图，顺带把渲染报告所需的
    全量结构化数据一并返回，供沙箱内的报告生成脚本一次取齐。

    非 report 意图返回 report=None —— 沙箱据此判定「本次不产文件」，
    不再出现「问什么都给一份报告」。
    """
    a = ask(query, intent_hint=intent_hint)
    if not a.get("ok"):
        return a
    intent = a.get("intent")
    if intent != "report":
        return {"ok": True, "intent": intent, "ask": a, "report": None}
    det = a.get("detected") or {}
    dom = det.get("domain") or "营销"
    period = det.get("period")
    a.setdefault("assumed", None)
    if not det.get("domain"):
        a["assumed"] = "未识别到数据域，默认以「营销域」为例作答；请提示用户可指定数据域。"
    return {"ok": True, "intent": "report", "domain": dom, "period": period, "ask": a,
            "report": report_data(dom, period)}


def save_report(filename, b64):
    """接收沙箱内生成的 docx（base64）落盘，返回可下载 URL。"""
    import base64
    print("[save_report] filename=%r b64_len=%d" % (filename, len(b64 or "")))
    # 非报告意图（规则/案例/执行）也会走到落盘节点，此时既没有文件名也没有内容。
    # 早期这里会用「质检报告.docx」兜底写一个 0 字节空文件，既污染目录又容易误导，
    # 改为明确跳过并返回 skipped，让上游知道"本次本来就没有文件"。
    if not (b64 or "").strip() or not (filename or "").strip():
        return {"ok": False, "skipped": True, "filename": "", "bytes": 0, "url": "",
                "reason": "本次无非报告文件需要落盘（非质检报告意图）"}
    safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", str(filename))
    if not safe.lower().endswith(".docx"):
        safe += ".docx"
    os.makedirs(OUT_DIR, exist_ok=True)
    path = os.path.join(OUT_DIR, safe)
    raw = base64.b64decode(b64 or "")
    with open(path, "wb") as f:
        f.write(raw)
    # 文件名含中文，URL 必须百分号转义，否则调用方按 ascii 编码请求头会报错
    q = urllib.parse.quote(safe)
    return {"ok": True, "filename": safe, "bytes": len(raw),
            "url": "http://localhost:%d/files/%s" % (PORT, q),
            "host_url": "http://host.docker.internal:%d/files/%s" % (PORT, q),
            "saved_to": path}


# ------------------------------------------------------------------ HTTP
ROUTES = {
    ("GET", "/health"): lambda p: {"ok": True, "service": "dq-mock-api",
                                   "time": dt.datetime.now().strftime("%Y-%m-%d %H:%M:%S")},
    ("GET", "/dq/domains"): lambda p: domain_list(p.get("period", [None])[0]),
    ("GET", "/dq/tables"): lambda p: table_list(p.get("domain", [None])[0],
                                                      p.get("period", [None])[0]),
    ("GET", "/dq/overview"): lambda p: overview(p.get("domain", [None])[0],
                                                      p.get("period", [None])[0]),
    ("GET", "/dq/ask"): lambda p: ask(p.get("query", [""])[0]),
    ("GET", "/dq/report_data"): lambda p: report_data(p.get("domain", ["营销"])[0],
                                                      p.get("period", [None])[0]),
    ("GET", "/dq/report_pack"): lambda p: report_pack(
        p.get("query", [""])[0], p.get("intent_hint", [None])[0]),
    ("POST", "/dq/report"): lambda p, b: report(b.get("domain"), b.get("table"), b.get("dimension")),
    ("POST", "/dq/recommend_rule"): lambda p, b: recommend_rule(
        b.get("problem_description"), b.get("dimension"), b.get("table"), b.get("field")),
    ("POST", "/dq/ask"): lambda p, b: ask(b.get("query"), b.get("intent_hint")),
    ("GET", "/dq/compare"): lambda p: compare(p.get("domain", [None])[0],
                                              p.get("table", [None])[0],
                                              p.get("dimension", [None])[0],
                                              p.get("period", [None])[0]),
    ("GET", "/dq/analyze"): lambda p: analyze(p.get("domain", [None])[0],
                                              p.get("table", [None])[0],
                                              p.get("dimension", [None])[0],
                                              p.get("period", [None])[0]),
    ("POST", "/dq/save_report"): lambda p, b: save_report(b.get("filename"), b.get("docx_b64")),
}


class Handler(BaseHTTPRequestHandler):
    server_version = "DQMockAPI/1.0"

    def _send(self, obj, code=200):
        data = json.dumps(obj, ensure_ascii=False, indent=2).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path == "/":
            return self._send({
                "service": "数据质检 Mock API",
                "endpoints": [
                    {"method": m, "path": p} for (m, p) in ROUTES
                ] + [{"method": "GET", "path": "/openapi.json"},
                     {"method": "GET", "path": "/files/<文件名>"}],
            })
        if u.path == "/openapi.json":
            return self._send(openapi_spec())
        if u.path.startswith("/files/"):
            return self._send_file(u.path[len("/files/"):])
        fn = ROUTES.get(("GET", u.path))
        if not fn:
            return self._send({"ok": False, "error": "not found: " + u.path}, 404)
        try:
            self._send(fn(parse_qs(u.query)))
        except Exception as e:
            self._send({"ok": False, "error": str(e)}, 500)

    def _send_file(self, name):
        """下载沙箱产出的报告文件。"""
        import urllib.parse as _up
        name = _up.unquote(name)
        safe = re.sub(r"[^\w\u4e00-\u9fa5.\-]", "_", name)
        path = os.path.join(OUT_DIR, safe)
        if not os.path.isfile(path):
            return self._send({"ok": False, "error": "file not found: " + safe}, 404)
        with open(path, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type",
                         "application/vnd.openxmlformats-officedocument."
                         "wordprocessingml.document")
        self.send_header("Content-Disposition",
                         "attachment; filename*=UTF-8''" + _up.quote(safe))
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        u = urlparse(self.path)
        fn = ROUTES.get(("POST", u.path))
        if not fn:
            return self._send({"ok": False, "error": "not found: " + u.path}, 404)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(n).decode("utf-8") if n else ""
            ctype = (self.headers.get("Content-Type") or "").lower()
            if "x-www-form-urlencoded" in ctype:
                # 表单编码：用 parse_qs 解出 {k: [v]}，便于承载大 base64，免去 JSON 转义
                body = {k: v[0] for k, v in parse_qs(raw, keep_blank_values=True).items()}
            else:
                body = json.loads(raw) if raw else {}
        except Exception:
            body = {}
        try:
            self._send(fn(parse_qs(u.query), body))
        except Exception as e:
            self._send({"ok": False, "error": str(e)}, 500)

    def log_message(self, fmt, *args):
        print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % args))


def openapi_spec():
    base = "http://host.docker.internal:%d" % PORT

    def op(op_id, summary, desc, params=None, body=None):
        d = {"summary": summary, "description": desc,
             "operationId": op_id,
             "responses": {"200": {"description": "成功",
                                   "content": {"application/json": {
                                       "schema": {"type": "object",
                                                  "additionalProperties": True}}}}}}
        if params:
            d["parameters"] = params
        if body:
            d["requestBody"] = {"required": False, "content": {"application/json": {
                "schema": {"type": "object", "properties": dict(body)}}}}
        return d

    def qp(name, desc):
        return {"name": name, "in": "query", "required": False,
                "description": desc, "schema": {"type": "string"}}

    def bp(name, desc):
        return (name, {"type": "string", "description": desc})

    return {
        "openapi": "3.0.0",
        "info": {"title": "数据质检计算服务", "version": "1.0.0",
                 "description": "数据共享中心智能体 · 数据质检场景的确定性计算接口。"
                                "只返回统计数字与匹配到的处置规则，不生成叙述结论。"},
        "servers": [{"url": base}],
        "paths": {
            "/dq/domains": {"get": op("dq_domains", "查询数据域清单",
                                      "返回各数据域及其问题数、涉及表数。若要问某个数据域的问题，先调此接口确认域名称。")},
            "/dq/tables": {"get": op("dq_tables", "查询表清单",
                                     "返回指定数据域下的表及问题数。",
                                     [qp("domain", "数据域名称，如：营销")])},
            "/dq/overview": {"get": op("dq_overview", "查询质检概览",
                                       "返回指定范围的质检总量、维度分布、状态分布。用于快速作答。",
                                       [qp("domain", "数据域名称，留空为全部")])},
            "/dq/report": {"post": op("dq_report", "生成质检报告素材",
                                      "返回质检报告三章节所需的全部结构化数据："
                                      "问题剖析（维度/规则/状态/TOP表）、数据影响（强规则命中、"
                                      "影响对象数、负责人分布）、当前建议（匹配台账处置规则）。"
                                      "模型据此组织叙述，不得自行编造数字。",
                                      body=[bp("domain", "数据域，如：营销"),
                                            bp("table", "表名，可留空"),
                                            bp("dimension", "质量维度中文名，如：完整性")])},
            "/dq/recommend_rule": {"post": op("dq_recommend_rule", "推荐质量规则",
                                              "根据问题描述/维度/目标表字段，推荐一条质量规则："
                                              "含模板、判定逻辑、建议阈值（取同类规则众数）、"
                                              "建议强弱等级与参考依据。",
                                              body=[bp("problem_description", "问题描述（自然语言）"),
                                                    bp("dimension", "质量维度，可留空由描述推断"),
                                                    bp("table", "目标表名（库表全名）"),
                                                    bp("field", "目标字段名")])},
            "/dq/ask": {"post": op("dq_ask", "质检统一问答取数",
                                   "【首选接口】传入用户原话，服务端完成意图路由与取数，"
                                   "返回 intent（report/rule/analysis/compare）"
                                   "、识别到的数据域/维度/表，"
                                   "以及对应的结构化 payload。模型只需据 payload 成文，不得改动数字。",
                                   body=[bp("query", "用户原话")])},
        },
    }


PORT = 8090


def main():
    global PORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8090)
    args = ap.parse_args()
    PORT = args.port
    if not os.path.exists(DB):
        raise SystemExit("找不到数据库 %s，请先运行 build_base.py" % DB)
    srv = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print("数据质检 Mock API 已启动 -> http://0.0.0.0:%d" % PORT)
    print("接口清单 GET /   ·  OpenAPI GET /openapi.json")
    srv.serve_forever()


if __name__ == "__main__":
    main()
