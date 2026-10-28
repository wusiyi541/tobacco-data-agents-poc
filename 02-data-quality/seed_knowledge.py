# -*- coding: utf-8 -*-
"""
数据质检 POC · 三项缺失素材沉淀脚本
=================================================
V3 第 03 章里标「待沉淀 / 待确认」的三项素材，在此从现有数据一次性沉淀成
结构化表，供报告与规则推荐使用：

  ① handling_rule    问题处理规则表（问题类型 → 处置方式 → 责任方 → 建议分级）
  ② feature_rule_map 数据特性 → 规则模板映射
  ③ caliber          统计口径定义

口径来源说明：
  · handling_rule 的处置方式逐条对照《YC用数需求和数据质量问题台账》真实工单
    归纳（脚本用关键字回算 evidence_count，数字可核）。
  · feature_rule_map 的占比/阈值由 quality_rule 表实算得出。
  · caliber 为人工定义，代码固化执行。

可重复执行：每次先清空这三张表再写入。
"""
import os
import sqlite3

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "dq_poc.db")

DDL = """
CREATE TABLE IF NOT EXISTS handling_rule (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  dimension TEXT, symptom TEXT, handling TEXT, grade TEXT,
  grade_cn TEXT, owner_role TEXT, evidence_count INTEGER, note TEXT
);
CREATE TABLE IF NOT EXISTS feature_rule_map (
  id INTEGER PRIMARY KEY AUTOINCREMENT,
  data_feature TEXT, dimension TEXT, template_name TEXT,
  rule_share REAL, strong_share REAL, suggested_threshold TEXT,
  sample_rules TEXT, note TEXT
);
CREATE TABLE IF NOT EXISTS caliber (
  key TEXT PRIMARY KEY, name TEXT, definition TEXT, formula TEXT, note TEXT
);
"""

# 建议分级（对应 V3「当前建议」章节的三档）
GRADE = {
    "A": "可直接执行",
    "B": "疑似待判",
    "C": "现象上报",
}

# ① 问题处理规则表：(维度, 现象特征, 处置方式, 分级, 责任方, 台账回算关键字, 备注)
HANDLING = [
    # ---------------- 及时性问题 ----------------
    ("及时性", "分区数据量为 0 或数据未及时更新，上游为 HANA 类数据源",
     "重启上游服务器后重新抽取数据", "A", "数据运维", ["重启"], "高频；多表同时发生"),
    ("及时性", "数据未及时更新，入湖作业报错",
     "定位报错原因后重新抽取数据", "A", "数据运维", ["入湖作业报错"], ""),
    ("及时性", "数据未及时更新，上游作业当日未发布",
     "发布上游作业后重跑下游", "A", "数据开发", ["未发布"], "上游未发布导致下游无数据"),
    ("及时性", "分区数据量为 0，调度区间条件写错",
     "修正区间条件后重跑数据", "A", "数据开发", ["区间条件"], "重复出现多张表"),
    ("及时性", "对账数据不平，同步作业耗时超过对账窗口",
     "调整对账时间或延后/提前作业启动时间", "B", "数据开发", ["对账", "作业启动时间"], ""),
    ("及时性", "入湖作业失败，进程 OOM 异常",
     "调小 batchSize（如 256）后重跑", "A", "数据开发", ["batchSize", "OOM"], "参数类问题"),
    ("及时性", "下游报表与上游日报累计值不一致（下游未及时更新）",
     "重跑下游报表作业", "A", "数据开发", ["未及时更新"], ""),
    ("及时性", "业务系统有数据但底座数据严重缺失",
     "修复程序逻辑 bug 后重新同步数据", "A", "数据开发", ["逻辑bug", "程序逻辑"], ""),
    ("及时性", "分区数据量为 0，源端确实无数据",
     "与数源确认后标记为正常（非数据问题）", "C", "业务/数源", ["数源数据为0", "数源反馈正常"], ""),
    ("及时性", "数据未及时更新，作业运行时间过长被系统 kill",
     "优化作业后重跑，必要时拆分任务", "B", "数据开发", ["kill"], ""),

    # ---------------- 唯一性问题 ----------------
    ("唯一性", "主键重复，开链日切表取数逻辑有误",
     "修正逻辑为 start_date<=biz_date and end_date>biz_date", "A", "数据开发",
     ["开链日切表的逻辑有误"], "反复出现，属典型逻辑缺陷"),
    ("唯一性", "主键重复，源表存在逻辑删除数据未剔除",
     "修改处理逻辑，仅取最新日期记录", "A", "数据开发", ["获取最新日期记录"], ""),
    ("唯一性", "主键重复，拉链表重跑时同时间跑多个日期",
     "按日期先后顺序重跑数据", "A", "数据开发", ["按日期前后顺序"], ""),
    ("唯一性", "主键重复，分库键与主键不一致导致数据重复",
     "将分库键调整为唯一性最高的字段（主键或联合主键之一）", "A", "数据开发",
     ["分库键"], "分库键选型问题"),
    ("唯一性", "主键重复，源系统数据本身重复",
     "与源系统确认后由源端整改，再重抽数据", "B", "业务+开发", ["来源表就存在"], ""),
    ("唯一性", "主键重复，主表与明细表对应关系未确认",
     "确认表间关系后修正脚本", "B", "业务确认", ["1对1", "1对多"], ""),
    ("唯一性", "主键重复，重复的原因为新记录覆盖老记录逻辑缺失",
     "调整逻辑：存在新记录时不再取用老记录，并重跑", "A", "数据开发",
     ["如果有新记录"], ""),
    ("唯一性", "主键重复，历史数据存在同日多档位",
     "重跑历史数据后恢复正常", "A", "数据开发", ["重跑历史数据"], ""),
    ("唯一性", "主键重复，修复程序未发布导致问题复现",
     "确认程序发布后重跑数据", "A", "数据开发", ["未发布，已完成发布"], ""),

    # ---------------- 完整性问题 ----------------
    ("完整性", "字段空值，订单明细等关联数据缺失",
     "由数源补录缺失的明细数据", "A", "业务/数源", ["补录"], ""),
    ("完整性", "字段空值，新增字段未纳入数据汇聚作业",
     "重新同步历史数据并更新数据汇聚作业", "A", "数据开发", ["后期在表中增加"], ""),
    ("完整性", "字段空值，上游父节点任务未完成即触发下游",
     "重跑上游来源表，并将其配置为父节点依赖", "A", "数据开发", ["父节点"], ""),
    ("完整性", "字段空值，日期格式不一致导致关联失败",
     "统一 biz_date 格式为 yyyy-mm-dd", "A", "数据开发", ["格式"], "公共层格式统一"),
    ("完整性", "数据量偏少，初次创建调度作业未全量同步历史数据",
     "重新抽取历史数据补齐", "A", "数据开发", ["初次创建调度作业"], "多表共性"),
    ("完整性", "底座缺少某时间段历史数据，源库保留完整",
     "从源库重新抽取缺失区间数据", "A", "数据开发", ["mc缺少", "重抽数据即可"], "多表共性"),
    ("完整性", "机构名称/编码为空，来源维度表未就绪",
     "重跑来源维度表后重跑目标表", "A", "数据开发", ["机构"], ""),
    ("完整性", "字段为空字符串，来源表本身存在空串",
     "数据修复后重新跑数", "A", "数据开发", ["重新跑数"], ""),
    ("完整性", "数据量为 0，业务逻辑或关联条件错误",
     "修改业务逻辑与关联条件后重跑", "A", "数据开发", ["业务逻辑和关联条件"], ""),
    ("完整性", "空值率上升或持续为空，业务侧可能属正常现象",
     "上报业务确认是否为正常业务现象", "C", "业务", ["待整合", "待确认", "正常"], ""),

    # ---------------- 一致性问题 ----------------
    ("一致性", "多源数据量不一致，因保留期不同导致核对区间不一致",
     "调整核对时间或修改对账规则，保持相同时间区间", "A", "数据开发",
     ["核对时间", "对账规则"], "高频；读库仅保留近 N 天"),
    ("一致性", "数据不一致，增量策略为 insert ignore 导致更新丢失",
     "策略改为 replace 并补录已更新数据", "A", "数据开发", ["insert ignore", "ignore"], ""),
    ("一致性", "数据不一致，源端历史数据发生更新未同步",
     "重新同步/补录变化数据", "A", "数据开发", ["源数据有更改", "补入"], ""),
    ("一致性", "对账不平，质检任务运行时同步作业尚未结束",
     "调整作业启动时间或延后对账窗口", "A", "数据开发", ["未执行完成", "作业未跑完"], ""),
    ("一致性", "数据量与源库不一致，两者不同源",
     "改为同源核对（业务库对业务库）", "B", "业务确认", ["同源"], ""),
    ("一致性", "同一机构 UUID 对应多个名称",
     "统一从结构维度表获取机构名称后重跑", "A", "数据开发", ["机构名称", "机构名"], ""),
    ("一致性", "主表与明细表关联缺失，历史数据不完整",
     "确认历史数据补录方案后修复", "B", "业务+开发", ["历史数据补录方案"], ""),
    ("一致性", "累计值不一致，时间区间或口径不一致",
     "统一取数区间后重跑", "A", "数据开发", ["累计值", "区间"], ""),
    ("一致性", "累计销量/销售额不一致，上游主键重复引发笛卡尔积",
     "先修复上游主键重复，再全量重跑下游", "A", "数据开发", ["笛卡尔积"], ""),
    ("一致性", "数源侧尚未完成数据更新导致的不一致",
     "待源端更新完成后重跑（跟踪源端进度）", "C", "业务", ["营销补偿机制", "源端"], ""),

    # ---------------- 准确性问题 ----------------
    ("准确性", "数值精度丢失（如 170.82 变成 171）",
     "比对源表与底座字段类型长度，整改后重跑", "A", "数据开发", ["精度", "字段类型"], ""),
    ("准确性", "统计值异常偏高，源端记录重复出现",
     "对源表去重后重跑", "A", "数据开发", ["重复出现"], ""),
    ("准确性", "未剔除逻辑删除数据导致统计偏大",
     "在脚本中增加 sysisdelete='0' 等过滤条件", "A", "数据开发",
     ["sysisdelete", "逻辑删"], ""),
    ("准确性", "字段值与业务系统不一致，成员表结构修改有误",
     "对源表与目标表结构整体比对并整改相关作业", "A", "数据开发", ["表结构"], ""),
    ("准确性", "数据重复出现，主表与明细表关系尚未确认",
     "先由业务确认表间关系，再修正脚本", "B", "业务确认", ["1对1", "1对多"], ""),
    ("准确性", "业务标识异常（如商家 id 全为 1），字典表缺失",
     "协调数源方确认并补全字典表", "C", "业务/数源", ["数源", "字典表"], ""),
    ("准确性", "统计口径与设计不符（聚合粒度写错）",
     "按设计文档修正聚合逻辑后重跑", "B", "业务确认", ["实际脚本"], ""),
    ("准确性", "底座存在无可溯源记录的脏数据",
     "重建表清除错误数据后重新导入", "A", "数据开发", ["重建表"], ""),
    ("准确性", "分区值不符合规范（如 202390）",
     "清空异常分区后重跑", "A", "数据开发", ["异常分区"], ""),

    # ---------------- 有效性问题 ----------------
    ("有效性", "日期格式不统一（yyyy-mm-dd 与 yyyymmdd 混用）",
     "统一日期格式后重跑", "A", "数据开发", ["格式不统一", "格式统一"], ""),
    ("有效性", "分区值错误导致数据落错分区",
     "清空异常分区并重跑", "A", "数据开发", ["清空异常分区"], ""),
    ("有效性", "编码类字段出现未登记值",
     "补充字典表并增加枚举值校验规则", "B", "业务", ["字典"], ""),
    ("有效性", "字段取值超出业务允许范围",
     "上报业务确认取值边界，确认后新增有效性规则", "C", "业务", ["待确认"], ""),
    ("有效性", "字段值不符合业务格式（如手机号位数不对）",
     "按标准格式回填，并配置格式校验规则", "A", "数据开发", ["格式"], ""),

    # ---------------- 规范性问题 ----------------
    ("规范性", "字段注释缺失，无法识别数据内容",
     "补全字段注释（可与元数据规范审核联动）", "A", "数据开发", ["注释"], ""),
    ("规范性", "字段名不可识别（如 db_filed+数字）",
     "重命名字段并补充注释", "A", "数据开发", ["db_filed"], ""),
    ("规范性", "字段名与数据模型设计不一致",
     "更新数据模型使其与实际表结构一致", "A", "数据开发", ["模型", "不一致"], ""),
    ("规范性", "字段与下发标准接口不一致",
     "核对标准接口文件，确认差异来源后再整改", "B", "业务+开发", ["标准"], ""),
    ("规范性", "业务标签/命名不符合规范",
     "按命名规范整改", "A", "数据开发", ["规范化"], ""),
]

# ② 数据特性 → 规则模板映射
FEATURE = [
    ("字段空值率高（NULL 占比偏高）", "完整性", "字段空值校验", 0.390, 0.003,
     "错误率 ERROR_RATE < 10%（即字段非空率 >= 90%）",
     "is_actual_oper_字段空值校验、xxx_字段空值校验",
     "规则库中占比最高的模板，是缺失类问题的首选校验方式"),
    ("字段为空字符串（空白而非 NULL）", "完整性", "字段空字符串校验", 0.265, 0.0,
     "错误率 ERROR_RATE < 10%；或正常率 NORMAL_RATE >= 95%",
     "xxx_字段空字符串校验",
     "空值与空串是两种问题，需分别配置规则"),
    ("编码/枚举类字段出现未登记值、格式不符", "有效性", "字段有效性校验", 0.134, 0.0,
     "错误率 ERROR_RATE < 10%；或 NORMAL_RATE >= 80%",
     "xxx_字段有效性校验",
     "适用于状态码、类型码、日期格式等取值域受限字段"),
    ("主键或业务唯一键出现重复", "唯一性", "字段唯一性校验", 0.074, 0.959,
     "正常率 NORMAL_RATE >= 95%；强规则多用 ERROR_NUMBER = 0",
     "dim_org_info_id_unique_xxx、xxx_unique_xxx",
     "唯一样本中 96% 被配置为强规则，属高优先级问题"),
    ("跨表同一指标对不上（如销售流水 vs 支付流水）", "一致性", "多表一致性校验", 0.073, 0.795,
     "正常率 NORMAL_RATE >= 95%",
     "销售流水表和支付流水表数据保持一致",
     "79% 为强规则，一旦命中通常影响多个下游报表"),
    ("表/分区数据量异常波动或突然为 0", "准确性", "表稳定性校验", 0.053, 0.0,
     "按日环比波动阈值判定（需结合历史基线）",
     "xxx_表稳定性校验",
     "技术口径原名 STABILITY(稳定性)，按客户报告模板统一呈现为「准确性」；属趋势类校验，需多期数据"),
    ("时间字段先后顺序不合理（如结束早于开始）", "及时性", "单表时间字段比较", 0.011, 0.0,
     "错误条数 ERROR_NUMBER = 0",
     "xxx_单表时间字段比较",
     "样本较少，属长尾模板"),
]

# ③ 统计口径定义
CALIBER = [
    ("dimension_mapping", "质量维度口径",
     "问题按质量维度归类：本期问题以 problem.dimension（取自 last_problem_types）为准；"
     "规则以 quality_rule.dimension（取自 rule_catalogs）为准。两者共用同一套英文枚举。",
     "英文枚举 ↔ 中文名 见 dimension_map 表",
     "【已对齐客户模板】技术规则库原用 STABILITY(稳定性)，业务台账与客户"
     "《数据质量检测报告》模板均称「准确性」，现统一呈现为「准确性」；"
     "技术原值保留在 dimension(EN) 列可回溯。台账另有「规范性」一类，单列标注"),
    ("problem_count", "问题数口径",
     "统计范围内 problem 表的记录数（一条 = 一个问题对象 × 一条规则的命中）。",
     "COUNT(problem)", "问题对象可为表或字段，字段级占比更高"),
    ("problem_share", "占比口径",
     "某维度问题数 / 同范围内问题总数。",
     "某维度问题数 ÷ 范围内问题总数 × 100%",
     "分母随筛选范围（数据域/表/期次）变化，报告中须写明范围"),
    ("strong_rule", "强/弱规则口径",
     "quality_rule.strength = STRONG 为强规则，命中后影响面较大，建议优先处置。",
     "SUM(rule_strength='STRONG')", "唯一样本中强规则占比最高（约 96%）"),
    ("pass_rate", "规则通过率口径",
     "执行记录中校验通过（is_pass='Y'）的规则数 / 有效执行规则数（task_status='SUCCESS'）。",
     "SUM(is_pass='Y') ÷ SUM(task_status='SUCCESS')",
     "WAITING/RUNNING/FAILED 状态不计入分母"),
    ("exec_status", "执行状态口径",
     "SUCCESS 成功、FAILED 失败、RUNNING 运行中、WAITING 等待调度。",
     "GROUP BY task_status", ""),
    ("mttr", "问题处置时长口径",
     "台账（已闭环工单）中「实际完成时间 − 提出日期」的天数。",
     "AVG(lead_time_days) WHERE status='已解决'",
     "仅台账工单有该字段；当期系统检出的问题清单无处置时长"),
    ("ring_ratio", "环比口径【已启用】",
     "本期问题数相对上一期的问题数变化率；期次以 problem.ds / rule_exec.ds 划分。",
     "(本期问题数 − 上期问题数) ÷ 上期问题数 × 100%",
     "现已有两期数据：2026-08（上期，ds=20260822）与 2026-09（本期，ds=20260922），可直接计算"),
    ("period", "期次口径",
     "一个问题/执行记录属于哪一期，由 ds 前 6 位（YYYYMM）决定；本期 is_current=1。",
     "见 period_dim 表", "报告默认出本期，环比对比上一期"),
    ("data_domain", "数据域口径",
     "按表描述与表名关键词打标：一号工程（下行/HANA）> 物流 > 专卖（含许可证/案件）> "
     "营销 > 综管，先匹配者优先。",
     "见 build_base.py 的 DOMAIN_RULES", "规则表与问题清单共用同一套打标逻辑"),
    ("advice_grade", "建议分级口径",
     "A 可直接执行（规则可判定、动作明确）；B 疑似待判（需业务确认后再动）；"
     "C 现象上报（仅提示现象，不给出具体更正动作）。",
     "见 handling_rule.grade", "对应 V3「AI 不改数」原则：所有建议均需人工确认后由业务系统执行"),
]


def seed():
    conn = sqlite3.connect(DB)
    c = conn.cursor()
    c.executescript(DDL)
    for t in ("handling_rule", "feature_rule_map", "caliber"):
        c.execute("DELETE FROM %s" % t)

    # ① handling_rule（evidence_count 从台账实算）
    n_ev = 0
    for dim, symptom, handling, grade, owner, kws, note in HANDLING:
        cond = " OR ".join(["cause_solution LIKE ?"] * len(kws))
        args = ["%%%s%%" % k for k in kws]
        cnt = c.execute("SELECT COUNT(*) FROM problem_ticket WHERE " + cond, args).fetchone()[0]
        c.execute("INSERT INTO handling_rule "
                  "(dimension,symptom,handling,grade,grade_cn,owner_role,evidence_count,note) "
                  "VALUES (?,?,?,?,?,?,?,?)",
                  (dim, symptom, handling, grade, GRADE[grade], owner, cnt, note))
        n_ev += cnt

    # ② feature_rule_map（占比/强弱从规则表实算校验）
    for feat, dim, tpl, share, strong, thr, samples, note in FEATURE:
        row = c.execute("""SELECT COUNT(*), SUM(strength='STRONG') FROM quality_rule
                           WHERE template_name=?""", (tpl,)).fetchone()
        real_n, real_s = row[0], (row[1] or 0)
        real_share = real_n / 1000.0
        real_strong = (real_s / real_n) if real_n else 0.0
        c.execute("INSERT INTO feature_rule_map "
                  "(data_feature,dimension,template_name,rule_share,strong_share,"
                  "suggested_threshold,sample_rules,note) VALUES (?,?,?,?,?,?,?,?)",
                  (feat, dim, tpl, round(real_share, 4), round(real_strong, 4),
                   thr, samples, note))

    # ③ caliber
    for k, name, d, f, note in CALIBER:
        c.execute("INSERT INTO caliber VALUES (?,?,?,?,?)", (k, name, d, f, note))

    conn.commit()

    print("=" * 70)
    print("三项素材沉淀完成")
    print("  ① handling_rule      %d 条（台账合计回算支撑案例 %d 例次）"
          % (c.execute("SELECT COUNT(*) FROM handling_rule").fetchone()[0], n_ev))
    print("  ② feature_rule_map   %d 条"
          % c.execute("SELECT COUNT(*) FROM feature_rule_map").fetchone()[0])
    print("  ③ caliber            %d 条"
          % c.execute("SELECT COUNT(*) FROM caliber").fetchone()[0])
    print()
    print("--- 处理规则 · 维度 × 分级 分布 ---")
    for r in c.execute("""SELECT dimension, grade_cn, COUNT(*) FROM handling_rule
                          GROUP BY dimension, grade ORDER BY dimension, grade"""):
        print("  %-6s %-8s %s" % r)
    print()
    print("--- 处理规则 · 有台账案例支撑的条目（Top8）---")
    for r in c.execute("""SELECT dimension, substr(symptom,1,26), decision_grade, evidence_count
                          FROM (SELECT dimension, symptom, grade_cn decision_grade, evidence_count
                                FROM handling_rule) WHERE evidence_count>0
                          ORDER BY evidence_count DESC LIMIT 8"""):
        print("  %-6s %-28s %-8s %s例" % r)
    print()
    print("--- 数据特性 → 规则模板（实算校验）---")
    for r in c.execute("""SELECT dimension, template_name, rule_share, strong_share
                          FROM feature_rule_map ORDER BY rule_share DESC"""):
        print("  %-6s %-14s 占比%.1f%%  强规则%.0f%%"
              % (r[0], r[1], r[2] * 100, r[3] * 100))
    conn.close()


if __name__ == "__main__":
    seed()
