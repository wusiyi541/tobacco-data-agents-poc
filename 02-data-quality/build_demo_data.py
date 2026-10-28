# -*- coding: utf-8 -*-
"""数据质检 POC · 演示数据底座构建脚本（全虚构）。

生成 dq_poc.db（SQLite），供 mock_api.py 读取。全部数据为脚本虚构，
不含任何真实企业、真实人员、真实系统数据。

运行：python build_demo_data.py
"""
import datetime
import os
import random
import sqlite3
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dq_poc.db")

# 质量六维（技术规则库标准维度）
DIMS = [("COMPLETENESS", "完整性问题"), ("UNIQUENESS", "唯一性问题"),
        ("VALIDITY", "有效性问题"), ("ACCURACY", "准确性问题"),
        ("CONSISTENCY", "一致性问题"), ("TIMELINESS", "及时性问题")]
# 业务域 → 演示表清单（与 01 寻数 POC 的目录词表同源，全部虚构）
DOMAIN_TABLES = {
    "营销": [("零售客户基本信息表", "dwd_demo.cst_customer_profile"),
             ("零售户终端销售信息表", "dwd_demo.cst_terminal_sales_dtl"),
             ("客户订单月报信息表", "dws_demo.ord_customer_monthly"),
             ("公司客户档位投放信息表", "dwd_demo.cst_gear_delivery"),
             ("卷烟商品信息拉链表", "ods_demo.prod_cig_info_zipper")],
    "专卖": [("案件基本信息表", "ods_demo.enf_case_base"),
             ("真烟流出品规分析表", "ads_demo.enf_outflow_analyze"),
             ("双随机检查计划信息表", "ods_demo.enf_random_plan")],
    "综管": [("综管平台指标结果表", "dws_demo.mgmt_metric_result"),
             ("考核指标月度汇总表", "ads_demo.mgmt_kpi_monthly")],
    "未分类": [("外部接口日志表", "ods_demo.ext_api_log")],
}
TEMPLATES = ["字段空值校验", "字段空字符串校验", "字段唯一性校验",
             "单表时间字段比较", "字段有效性校验", "多表一致性校验", "表稳定性校验"]
TEMPLATE_TYPE = dict(zip(TEMPLATES, ["FIELD_NULL_VALUE_VALIDATE", "FIELD_EMPTY_STRING_VALIDATE",
                                     "FIELD_UNIQUE_VALIDATE", "SINGLE_TABLE_TIME_COMPARE",
                                     "FIELD_VALIDITY_VALIDATE", "MULTI_TABLE_CONSISTENCY",
                                     "TABLE_STABILITY_VALIDATE"]))
STATUSES_P = ["FINISHED", "NEW", "VERIFY", "IGNORE"]
TASK_STATUS = ["SUCCESS", "SUCCESS", "SUCCESS", "FAILED", "RUNNING", "WAITING", ""]
SURNAMES = "赵钱孙李周吴郑王冯陈褚卫蒋沈韩杨朱秦许何吕施张孔曹严金魏陶姜"

random.seed(2026)


def person():
    return random.choice(SURNAMES) + "".join(random.choice("文华明芳军丽伟静涛燕") for _ in range(random.randint(1, 2)))


def rand_dt(day_from=datetime.date(2026, 8, 1)):
    d = day_from + datetime.timedelta(days=random.randint(0, 60))
    return d.strftime("%Y-%m-%d") + " %02d:%02d:%02d" % (random.randint(8, 20), random.randint(0, 59), random.randint(0, 59))


def main():
    if os.path.exists(DB):
        os.remove(DB)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    tables_sql = open(os.path.join(HERE, "schema_demo.sql"), encoding="utf-8").read()
    cur.executescript(tables_sql)

    # ---------- dimension_map ----------
    dim_defs = {"COMPLETENESS": "数据缺失或为空", "UNIQUENESS": "主键/业务键重复",
                "VALIDITY": "取值超出枚举或值域", "ACCURACY": "数值与事实不符",
                "CONSISTENCY": "多表同对象数据不一致", "TIMELINESS": "数据未按预期频率更新"}
    for en, cn in DIMS:
        cur.execute("INSERT INTO dimension_map VALUES (?,?,?,?,?)",
                    (en, cn, cn[:-2], dim_defs[en], "技术规则库"))

    # ---------- period_dim ----------
    cur.execute("INSERT INTO period_dim VALUES ('202608','2026-08','2026 年 8 月','20260818','20260822','0')")
    cur.execute("INSERT INTO period_dim VALUES ('202609','2026-09','2026 年 9 月','20260915','20260919','1')")

    # ---------- meta / caliber ----------
    cur.execute("INSERT INTO meta VALUES ('build_time', ?)", (datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"),))
    caliber = [
        ("dimension_mapping", "质量维度口径", "问题按质量维度归类：problem.dimension（取自技术规则库）与台账 category（人工标注）双向映射。",
         "英文枚举 ↔ 中文名 见 dimension_map 表", "【演示数据】口径映射为演示用简化版"),
        ("period_definition", "统计期口径", "本期问题/规则执行按 period_dim 区间取 ds 统计。", "ds/100 = period_id", "虚构成分"),
        ("strong_weak", "强弱规则口径", "STRONG=强规则（阻断级），WEAK=弱规则（提示级）。", "quality_rule.strength", "演示口径"),
        ("grading_advice", "分级建议口径", "处理建议分三级：可直接执行 / 疑似待判 / 现象上报。", "handling_rule.grade_cn", "演示口径"),
    ]
    for row in caliber:
        cur.execute("INSERT INTO caliber VALUES (?,?,?,?,?)", row)

    # ---------- feature_rule_map ----------
    feats = [
        ("字段空值率高（NULL 占比偏高）", "完整性", "字段空值校验", 0.39, 0.0026, "错误率 ERROR_RATE < 10%", "缺失类问题的首选校验方式"),
        ("字符串长度为 0 但非 NULL", "完整性", "字段空字符串校验", 0.12, 0.01, "空串占比 < 5%", "常与空值校验组合使用"),
        ("主键/业务键出现重复", "唯一性", "字段唯一性校验", 0.08, 0.35, "重复行数 = 0", "强规则占比高"),
        ("时间字段晚于业务截止时间", "及时性", "单表时间字段比较", 0.07, 0.05, "延迟行数 = 0", "用于监控数据到达时间"),
        ("枚举字段出现越界值", "有效性", "字段有效性校验", 0.15, 0.08, "越界行数 = 0", "值域类问题主入口"),
        ("多表同对象字段值不一致", "一致性", "多表一致性校验", 0.11, 0.3, "不一致行数 = 0", "跨系统核对"),
        ("表行数日环比波动超阈值", "及时性", "表稳定性校验", 0.08, 0.2, "波动幅度 < 30%", "整表级稳定性监控"),
    ]
    for i, (f_, dim, tpl, rs, ss, th, note) in enumerate(feats, 1):
        cur.execute("INSERT INTO feature_rule_map VALUES (?,?,?,?,?,?,?,?,'')",
                    (i, f_, dim, tpl, rs, ss, th, note))

    # ---------- handling_rule（分级建议，虚构 40 条） ----------
    SYMPTOMS = [
        ("完整性", "字段大面积为空，上游源系统未采集该字段", "与数源部门确认采集必要性后补充采集逻辑", "A", "可直接执行", "数据开发"),
        ("完整性", "分区数据为空，上游任务未产出", "重跑上游任务并补数据", "A", "可直接执行", "数据运维"),
        ("完整性", "历史存量字段为空且无法回补", "在元数据中标注该字段适用起始日期", "C", "现象上报", "业务确认"),
        ("唯一性", "业务键重复，多源合并未去重", "加工层增加 ROW_NUMBER 去重逻辑", "A", "可直接执行", "数据开发"),
        ("唯一性", "主键重复但业务语义允许多版本", "确认保留最新版本口径后加时间戳排序", "B", "疑似待判", "业务+开发"),
        ("有效性", "枚举字段出现新值，码表未同步", "码表补录新枚举值", "A", "可直接执行", "业务/数源"),
        ("有效性", "数值字段出现负值", "核实是否冲正业务；是则白名单放行，否则修正", "B", "疑似待判", "业务确认"),
        ("准确性", "汇总值与明细加总不一致", "重算汇总任务并核对口径差异", "A", "可直接执行", "数据开发"),
        ("准确性", "与外部权威源存在固定偏差", "差异在阈值内时登记偏差说明", "C", "现象上报", "业务确认"),
        ("一致性", "两系统同对象状态不一致", "以权威源为准修正非权威侧数据", "A", "可直接执行", "数据开发"),
        ("一致性", "编码规则两套体系不一致", "建立编码映射表并按映射改写", "B", "疑似待判", "业务+开发"),
        ("及时性", "数据未按调度时间到达", "检查上游调度依赖并重跑", "A", "可直接执行", "数据运维"),
        ("及时性", "分区数据量为 0 或未更新，上游为外部接口", "联系接口提供方确认后重新抽取", "A", "可直接执行", "数据运维"),
    ]
    for i in range(1, 41):
        sym = SYMPTOMS[(i - 1) % len(SYMPTOMS)]
        cur.execute("INSERT INTO handling_rule VALUES (?,?,?,?,?,?,?,?,'演示规则（虚构）')",
                    (i, sym[0], sym[1], sym[2], sym[3], sym[4], sym[5], random.randint(2, 30)))

    # ---------- quality_rule（虚构 600 条） ----------
    rules = []
    rid = 8800000
    for tname, ten in [(tn, te) for tabs in DOMAIN_TABLES.values() for tn, te in tabs]:
        n = random.randint(60, 110)
        all_cols = ["id", "code", "name", "amount", "status", "created_at", "updated_at", "org_code", "city_code", "remark"]
        for _ in range(n):
            rid += 1
            tpl = random.choice(TEMPLATES)
            dim = {"字段空值校验": DIMS[0], "字段空字符串校验": DIMS[0], "字段唯一性校验": DIMS[1],
                   "单表时间字段比较": DIMS[5], "字段有效性校验": DIMS[2],
                   "多表一致性校验": DIMS[4], "表稳定性校验": DIMS[5]}[tpl]
            strength = random.choice(["STRONG", "WEAK", "WEAK", "WEAK"])
            domain = next(d for d, tabs in DOMAIN_TABLES.items() if (tname, ten) in tabs)
            col = random.choice(all_cols)
            rules.append({
                "rule_id": str(rid), "rule_name": f"{col}_{tpl}_{rid}",
                "rule_desc": f"对表 {ten} 的字段 {col} 执行{tpl}（演示规则）",
                "strength": strength, "strength_cn": "强规则" if strength == "STRONG" else "弱规则",
                "dimension": dim[0], "dimension_cn": dim[1],
                "template_id": str(100 + TEMPLATES.index(tpl)), "template_name": tpl,
                "template_type": TEMPLATE_TYPE[tpl], "table_id": f"odps.100001.demo.{ten.split('.')[-1]}",
                "table_name": ten, "table_description": tname, "data_domain": domain,
                "biz_unit_name": random.choice(["信息中心", "营销中心", "专卖监督管理处"]),
                "project_name": "企业数据中台建设项目（示例）",
                "validate_object_type": random.choice(["COLUMN", "TABLE"]),
                "validate_object_name": col, "validate_field": col,
                "threshold_metric": "ERROR_RATE", "threshold_expr": "ERROR_RATE < 0.1",
                "rule_status": "ACTIVE", "creator_name": person(),
                "gmt_create": rand_dt(datetime.date(2026, 1, 1)),
                "gmt_modified": rand_dt(datetime.date(2026, 6, 1)),
            })
    for r in rules:
        cur.execute(f"INSERT INTO quality_rule VALUES ({','.join('?' * 25)})",
                    [r[c] for c in ["rule_id", "rule_name", "rule_desc", "strength", "strength_cn",
                                    "dimension", "dimension_cn", "template_id", "template_name",
                                    "template_type", "table_id", "table_name", "table_description",
                                    "data_domain", "biz_unit_name", "project_name",
                                    "validate_object_type", "validate_object_name", "validate_field",
                                    "threshold_metric", "threshold_expr", "rule_status",
                                    "creator_name", "gmt_create", "gmt_modified"]])

    # ---------- problem + rule_exec（由规则派生，两个统计期） ----------
    periods = [("202608", 20260818, 20260822), ("202609", 20260915, 20260919)]
    pid = 700000
    for rule in rules:
        for period_id, d1, d2 in periods:
            if random.random() < 0.45:
                continue  # 并非每规则每期都有执行
            ds_day = random.randint(d1, d2)
            ds = int(f"{period_id[:4]}{str(ds_day)[-4:]}"[:8]) if False else int(f"{period_id[:4]}{str(ds_day)[-4:]}")
            ds = ds_day  # ds=yyyymmdd
            status = random.choice(TASK_STATUS)
            is_pass = "N" if status == "FAILED" else (random.choice(["Y", "N", ""]) if status == "SUCCESS" else "")
            dur = random.randint(5, 180)
            start = rand_dt(datetime.date(ds_day // 10000, ds_day // 100 % 100, ds_day % 100))
            end = start[:11] + "%02d" % (int(start[11:13]) + 1) + start[13:]
            cur.execute("INSERT INTO rule_exec VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                        (cur.execute("SELECT COALESCE(MAX(exec_id),0)+1 FROM rule_exec").fetchone()[0],
                         rule["rule_id"], rule["rule_name"],
                         f"{ds_day // 10000}-{ds_day // 100 % 100:02d}-{ds_day % 100:02d}", str(ds),
                         status, is_pass, start, end, str(dur),
                         rule["dimension"], rule["dimension_cn"],
                         rule["table_name"], rule["table_description"], rule["data_domain"]))
            # 有执行且不通过的，按概率生成问题
            if is_pass == "N" and random.random() < 0.5:
                pid += 1
                cur.execute("INSERT INTO problem VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                            (str(pid), rule["validate_object_type"],
                             rule["table_name"] + "_" + rule["validate_object_name"],
                             rule["validate_object_name"], "odps.100001.demo",
                             rule["table_name"], rule["table_description"], rule["data_domain"],
                             rule["rule_id"], rule["rule_name"], rule["strength"],
                             rule["dimension"], rule["dimension_cn"],
                             random.choice(STATUSES_P),
                             f"{rule['template_name']}触发：{rule['validate_object_name']} 未达阈值（演示）",
                             rand_dt(), person(),
                             person() if random.random() < .5 else "",
                             "全表扫描", f"错误率 {round(random.uniform(1, 30), 1)}%", ds_day))

    # ---------- problem_ticket（虚构 120 条） ----------
    DOMAINS_T = ["DWD_智慧营销", "DWS_智慧营销", "ODS_智慧专卖", "外部数据", "营销", "综管"]
    DESCS = ["地市数据缺失，仅有部分地市有数据", "字段值全部为空", "状态字段出现未知枚举值",
             "日增量数据未按时到达", "汇总表与明细表数值不一致", "主键出现重复记录",
             "金额字段出现异常大值，疑似单位错误", "时间字段格式不统一"]
    SOLUTIONS = ["已反馈数源部门补采", "已修正加工逻辑并重跑", "码表已补录", "调度依赖已修复",
                 "以权威源口径重算", "已增加去重逻辑", "已与业务确认口径并登记"]
    for i in range(1, 121):
        d = datetime.date(2026, 3, 1) + datetime.timedelta(days=random.randint(0, 150))
        lead = random.randint(1, 30)
        plan = d + datetime.timedelta(days=lead)
        cur.execute("INSERT INTO problem_ticket VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, random.choice(DOMAINS_T), random.choice(DOMAINS_T),
                     random.choice([x[0] for tabs in DOMAIN_TABLES.values() for x in tabs]),
                     random.choice([x[1] for tabs in DOMAIN_TABLES.values() for x in tabs]),
                     random.choice(DESCS), person(), d.strftime("%Y-%m-%d"),
                     random.choice(["已解决", "已解决", "处理中", "待处理", "无需处理", ""]),
                     person(), random.choice(["完整性问题", "准确性问题", "及时性问题",
                                              "唯一性问题", "有效性问题", "一致性问题"]),
                     "", random.choice(SOLUTIONS), plan.strftime("%Y-%m-%d"),
                     (plan + datetime.timedelta(days=random.randint(-3, 5))).strftime("%Y-%m-%d"),
                     lead, random.choice(["", "", "高"]), "演示工单（虚构）"))

    # ---------- requirement（虚构 80 条） ----------
    REQ_DESCS = ["补充客户维度月度销售汇总数据", "新增外部经济指标入湖", "增加案件数据的质检规则",
                 "整合多源客户主数据", "补充分部机构维表", "提供终端销售日粒度接口",
                 "补充品规主数据标准编码", "增加指标口径说明文档"]
    for i in range(1, 81):
        d = datetime.date(2026, 2, 1) + datetime.timedelta(days=random.randint(0, 180))
        lead = random.randint(2, 45)
        cur.execute("INSERT INTO requirement VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (i, random.choice(["入湖需求", "整合需求", "质检需求"]),
                     random.choice(DOMAINS_T), random.choice(REQ_DESCS),
                     "按业务口径提供近三年月度数据（演示）", "按技术口径入湖至 dwd_demo 层（演示）",
                     random.choice(["高", "中", "低"]), (d + datetime.timedelta(days=random.randint(5, 30))).strftime("%Y-%m-%d"),
                     person(), d.strftime("%Y-%m-%d"), random.choice(["", "已沟通"]),
                     random.choice(["已解决", "处理中", "无需处理", "行政协调中", "挂起"]),
                     person(), "演示需求（虚构）", "", "", "", lead, ""))

    conn.commit()
    conn.close()
    print(f"dq_poc.db 生成完毕（全虚构）：quality_rule={len(rules)} 等多表")


if __name__ == "__main__":
    main()
