# -*- coding: utf-8 -*-
"""生成虚构演示目录数据 catalog.json（200 条，含血缘层核心表名）。

全部数据为脚本虚构，不含任何真实企业/系统数据。
运行：python gen_demo_catalog.py
"""
import io, json, os, random, sys
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
HERE = os.path.dirname(os.path.abspath(__file__))

# 血缘层核心表（与 generate_kb.py 的 LAYERS 对齐，保证演示链路可跑）
CORE_TABLES = [
    '20支零售户终端销售信息明细表', '零售户终端销售信息表', '客户订单月报信息表',
    '卡片库_经济运行_月度统计', '卡片库_经济运行_年度统计', '零售客户基本信息表',
    '零售客户基本信息拉链表', '一户一图-零售户基础信息表', '订单主表', '订单明细表',
    '零售户投放周期及已购数量信息表', '零售户进销存日报信息', '一户一图-零售户价类购进量汇总月表',
    '零售许可证主表', '零售许可证信息日切表', '专卖零售许可证日报表',
    '物流配送交接数据明细表', '物流配送交接数据主表', '省外真烟外流信息表',
    '真烟流出品规分析表', '公司客户档位投放信息表', '投放卷烟组合周期档位投放表',
    '卷烟商品信息拉链表',
]

ORGS = ['信息中心', '营销中心', '物流中心', '专卖监督管理处', '财务处', '人事处', '办公室']
DTYPES = ['系统库表', '系统库表', '系统库表', '统计报表', '外部数据']
SHARES = ['有条件共享', '无条件共享', '有条件共享', '不予共享']
SHARE_DESC = {'有条件共享': '按申请审批后共享', '无条件共享': '可直接调用',
              '不予共享': '仅限授权场景使用'}

# 业务域：前缀 → 字段模板
DOMAINS = [
    ('营销', ['客户编码', '客户名称', '档位', '品规编码', '品规名称', '周期', '投放量', '实购量', '业务日期', '状态']),
    ('销售', ['订单编号', '客户编码', '品规编码', '数量', '金额', '业务日期', '渠道类型', '状态']),
    ('物流', ['配送单号', '客户编码', '线路编码', '送货员', '件数', '交接时间', '签收状态']),
    ('专卖', ['案件编号', '案件类型', '涉案品规', '发生地市', '查处数量', '办案单位', '案件状态', '发生日期']),
    ('财务', ['凭证编号', '科目编码', '科目名称', '借方金额', '贷方金额', '记账日期', '摘要']),
    ('人力', ['员工编号', '姓名', '组织机构', '岗位', '入职日期', '用工性质']),
    ('综合', ['指标编码', '指标名称', '统计周期', '指标值', '单位', '更新时间']),
]

SUFFIX = ['信息表', '明细表', '汇总表', '日切表', '拉链表', '月度统计表', '日报表', '基础信息表']

def gen_fields(domain):
    return '，'.join(DOMAINS[domain][1][:random.randint(6, 10)])

def main():
    random.seed(2026)
    catalog = []
    # ① 23 张血缘核心表：名称对齐 generate_kb.py，字段通用化
    for name in CORE_TABLES:
        dom = next((i for i, (p, _) in enumerate(DOMAINS) if p in name[:6]), 0)
        catalog.append({
            'code': f'POC-DEMO/{len(catalog)+1:03d}',
            'name': name,
            'desc': f'演示目录：{name}（虚构）',
            'org': random.choice(ORGS),
            'dtype': '系统库表',
            'share': '有条件共享',
            'fields': gen_fields(dom),
            'tags': random.choice(['营销域', '专卖域', '物流域', '综合域']),
        })
    # ② 补足 200 条：业务域 × 后缀 组合的虚构表
    seen = {x['name'] for x in catalog}
    while len(catalog) < 200:
        dom = random.randrange(len(DOMAINS))
        prefix, _ = DOMAINS[dom]
        name = f'{prefix}{random.choice(["卷烟", "客户", "订单", "配送", "案件", "费用", "资产", "合同", "证照", "考核"])}' \
               f'{random.choice(SUFFIX)}'
        if name in seen:
            continue
        seen.add(name)
        share = random.choice(SHARES)
        catalog.append({
            'code': f'POC-DEMO/{len(catalog)+1:03d}',
            'name': name,
            'desc': f'演示目录：{prefix}业务{name}（虚构，共享说明：{SHARE_DESC[share]}）',
            'org': random.choice(ORGS),
            'dtype': random.choice(DTYPES),
            'share': share,
            'fields': gen_fields(dom),
            'tags': f'{prefix}域',
        })
    out = os.path.join(HERE, 'catalog.json')
    json.dump(catalog, io.open(out, 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
    print('catalog.json:', len(catalog), '条（全部虚构）')

if __name__ == '__main__':
    main()
