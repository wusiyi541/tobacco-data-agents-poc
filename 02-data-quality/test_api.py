# -*- coding: utf-8 -*-
"""Mock API 全接口自测脚本。"""
import json
import urllib.request

BASE = "http://127.0.0.1:8090"


def get(path):
    with urllib.request.urlopen(BASE + path, timeout=15) as r:
        return json.loads(r.read().decode("utf-8"))


def post(path, body):
    req = urllib.request.Request(
        BASE + path, data=json.dumps(body).encode("utf-8"),
        headers={"Content-Type": "application/json"}, method="POST")
    with urllib.request.urlopen(req, timeout=20) as r:
        return json.loads(r.read().decode("utf-8"))


def show(t, obj, keys=None):
    print("=" * 72)
    print("▶", t)
    if keys:
        for k in keys:
            print("   %s = %s" % (k, obj.get(k)))
    else:
        print(json.dumps(obj, ensure_ascii=False, indent=2)[:1200])


show("GET /health", get("/health"), ["ok", "service"])
show("GET /dq/domains", get("/dq/domains"), ["ok"])
show("GET /dq/tables?domain=营销", get("/dq/tables?domain=%E8%90%A5%E9%94%80"), ["ok", "count"])

ov = get("/dq/overview?domain=%E8%90%A5%E9%94%80")
print("=" * 72)
print("▶ GET /dq/overview?domain=营销")
print("   totals =", ov["totals"])
print("   by_dimension =", [(d["dimension"], d["count"], d["share"]) for d in ov["by_dimension"]])

rp = post("/dq/report", {"domain": "营销"})
print("=" * 72)
print("▶ POST /dq/report  {domain:营销}")
print("   ok =", rp["ok"])
p1 = rp["part1_problem_analysis"]
print("   P1 总数 =", p1["total"])
print("   P1 维度 =", [(d["dimension"], d["count"], str(d["share"]) + "%") for d in p1["by_dimension"]])
print("   P1 Top表 =", [(t["table_name"][:30], t["count"]) for t in p1["top_tables"][:3]])
p2 = rp["part2_data_impact"]
print("   P2 强规则问题 =", p2["strong_rule_problem_count"], "占比", p2["strong_rule_share"], "%")
print("   P2 影响表/对象 =", p2["affected_table_count"], "/", p2["affected_object_count"])
print("   P2 强规则表 =", [(t["table_name"][:28], t["strong_count"]) for t in p2["strong_rule_tables"][:3]])
print("   P3 建议条数 = %d" % len(rp["part3_suggestion"]))
for s in rp["part3_suggestion"][:5]:
    print("      · [%s]%s %s（台账案例%s / 范围内%s）→ %s"
          % (s["dimension"], s["grade_cn"], s["symptom"][:24],
             s["evidence_count"], s["in_scope_count"], s["handling"][:30]))

rr = post("/dq/recommend_rule", {"problem_description": "零售客户信息表的客户编码有重复，主键不唯一",
                                 "table": "jcpt_st_prod.dwd_mkt_cst_base_info_s",
                                 "field": "cust_code"})
print("=" * 72)
print("▶ POST /dq/recommend_rule  （主键重复）")
print("   识别维度 =", rr["detected_dimension"])
sr = rr["suggested_rule"]
print("   规则名 =", sr["rule_name"])
print("   模板 =", sr["template_name"], "| 检核对象 =", sr["validate_object"])
print("   判定逻辑 =", sr["logic"])
print("   建议阈值 =", sr["threshold_suggestion"], "| 依据 =", sr["threshold_basis"][:2])
print("   建议强弱 =", sr["strength_suggestion"], "（%s）" % sr["strength_reason"])
print("   库内同模板 =", rr["reference"]["same_template_total"], "条，强规则占比",
      rr["reference"]["same_template_strong_share"], "%")

rr2 = post("/dq/recommend_rule", {"problem_description": "biz_date 字段大量为空值"})
print("-" * 72)
print("▶ POST /dq/recommend_rule  （空值）")
print("   识别维度 =", rr2["detected_dimension"], "| 模板 =", rr2["suggested_rule"]["template_name"],
      "| 阈值 =", rr2["suggested_rule"]["threshold_suggestion"])

cs = post("/dq/cases", {"query": "主键重复"})
print("=" * 72)
print("▶ POST /dq/cases  {query:主键重复}")
print("   识别维度 =", cs["detected_dimension"], "| 案例数 =", len(cs["cases"]))
for c in cs["cases"][:3]:
    print("      · 工单#%s %s | %s | 处置：%s" % (c["ticket_no"], c["category"],
                                                   c["problem_desc"][:26], c["cause_solution"][:34]))
print("   处置规则 =", [(r["symptom"][:22], r["grade_cn"]) for r in cs["handling_rules"][:3]])

ex = get("/dq/exec_status?domain=%E8%90%A5%E9%94%80")
print("=" * 72)
print("▶ GET /dq/exec_status?domain=营销")
print("   totals =", ex["totals"])
print("   by_status =", [(s["status"], s["count"]) for s in ex["by_status"]])

print("=" * 72)
print("✓ 全部接口自测完成")
