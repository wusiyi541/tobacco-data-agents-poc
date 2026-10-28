# -*- coding: utf-8 -*-
"""把 rules_engine.RULES 导出为《元数据检查规则表》CSV。

规则表恒由引擎导出，不手工维护，保证「规则表 = 引擎」同源。
新增列时只改本脚本与 RULES 元组，CSV 自动跟随。

用法：python export_rules.py [输出路径]
"""
import csv
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rules_engine import RULES, SEV_WEIGHT

HEADER = [
    "规则编号", "检查项类型", "对应元模型属性", "级别", "扣分", "判据",
    "依据条款", "依据强度", "原文出处与关键句",
]


def main(out_path=None):
    if not out_path:
        out_path = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                "元数据检查规则表.csv")
    rows = [HEADER]
    for r in RULES:
        rid, rtype, attr, sev, criterion, basis, strength, quote = r
        rows.append([rid, rtype, attr, sev, SEV_WEIGHT[sev], criterion,
                     basis, strength, quote])
    with open(out_path, "w", newline="", encoding="utf-8-sig") as f:
        csv.writer(f).writerows(rows)
    print("已导出 %d 条规则 -> %s" % (len(RULES), out_path))
    # 强度分布
    from collections import Counter
    c = Counter(r[6] for r in RULES)
    for k in ("原文直判", "属性推导", "技术约定"):
        print("  %s：%d 条" % (k, c.get(k, 0)))
    return out_path


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else None)
