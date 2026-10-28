# -*- coding: utf-8 -*-
"""为沉淀素材做统计：规则模板×维度、台账处置模式、台账分类空缺。"""
import sqlite3, os, collections

DB = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dq_poc.db")
conn = sqlite3.connect(DB)
c = conn.cursor()

print("=" * 74)
print("【1】规则模板 × 质量维度（用于「数据特性→规则模板」映射）")
rows = c.execute("""SELECT dimension_cn, template_name, COUNT(*),
                            SUM(strength='STRONG'), SUM(validate_object_type='COLUMN')
                     FROM quality_rule GROUP BY dimension_cn, template_name
                     ORDER BY 3 DESC""").fetchall()
tot = sum(r[2] for r in rows)
for r in rows:
    print("  %-8s %-16s 规则 %-4d(%4.1f%%) 强规则%-4s 字段级%-4s"
          % (r[0] or "-", r[1] or "-", r[2], 100.0 * r[2] / tot, r[3], r[4]))

print("\n【2】维度 → 阈值口径（ERROR_RATE 等）")
for r in c.execute("""SELECT dimension_cn, threshold_metric, threshold_expr, COUNT(*)
                      FROM quality_rule WHERE threshold_expr IS NOT NULL
                      GROUP BY 1,2,3 ORDER BY 4 DESC LIMIT 12"""):
    print("  %-8s %-14s %-8s %s" % r)

print("\n【3】台账处置模式：按分类取高频处置关键字")
for cat in ["一致性问题", "完整性问题", "及时性问题", "唯一性问题", "准确性问题", "有效性问题", "规范性问题"]:
    sols = [r[0] for r in c.execute(
        "SELECT cause_solution FROM problem_ticket WHERE category=? AND cause_solution<>''", (cat,))]
    kw = collections.Counter()
    PAT = ["重跑", "补录", "补数", "重抽", "去重", "修改程序", "修改处理逻辑",
           "更新策略", "修改策略", "核对", "确认", "全删全插", "重建表", "剔除",
           "过滤", "调整", "重启", "重新同步", "补全", "统一", "删除", "清空",
           "按日期", "历史数据", "配置", "报错", "数源", "待", "已解决"]
    for s in sols:
        for p in PAT:
            if p in s:
                kw[p] += 1
    print("  【%s】样本 %d 条 -> %s" % (cat, len(sols),
          "; ".join("%s(%d)" % (k, v) for k, v in kw.most_common(9))))

print("\n【4】台账工单 分类为空的行")
for r in c.execute("SELECT ticket_no, table_en, problem_desc, cause_solution, status FROM problem_ticket WHERE category='' OR category IS NULL"):
    print("  #%s %s | %s | %s | %s" % (r[0], str(r[1])[:40], str(r[2])[:40], str(r[3])[:40], r[4]))

print("\n【5】台账 处理人 / 域 分布（用于建议里的责任方）")
for r in c.execute("SELECT owner, COUNT(*) FROM problem_ticket WHERE owner<>'' GROUP BY owner ORDER BY 2 DESC LIMIT 12"):
    print("  %-16s %s" % r)
print()
for r in c.execute("SELECT domain_l2, COUNT(*) FROM problem_ticket WHERE domain_l2<>'' GROUP BY domain_l2 ORDER BY 2 DESC LIMIT 12"):
    print("  %-24s %s" % r)

print("\n【6】营销域当期问题：Top 表 / Top 规则 / 状态")
print("  -- Top 表 --")
for r in c.execute("""SELECT table_name, table_desc, COUNT(*) FROM problem
                      WHERE data_domain='营销' GROUP BY table_name ORDER BY 3 DESC LIMIT 8"""):
    print("    %-52s %-20s %s" % (str(r[0])[:52], str(r[1])[:20], r[2]))
print("  -- Top 规则 --")
for r in c.execute("""SELECT rule_name, COUNT(*) FROM problem
                      WHERE data_domain='营销' GROUP BY rule_name ORDER BY 2 DESC LIMIT 8"""):
    print("    %-48s %s" % (str(r[0])[:48], r[1]))
print("  -- 状态 --")
for r in c.execute("""SELECT problem_status, COUNT(*) FROM problem
                      WHERE data_domain='营销' GROUP BY 1 ORDER BY 2 DESC"""):
    print("    %-10s %s" % r)
print("  -- 负责人 Top --")
for r in c.execute("""SELECT owner_names, COUNT(*) FROM problem
                      WHERE data_domain='营销' AND owner_names<>'' GROUP BY 1 ORDER BY 2 DESC LIMIT 6"""):
    print("    %-24s %s" % (str(r[0])[:24], r[1]))
conn.close()
