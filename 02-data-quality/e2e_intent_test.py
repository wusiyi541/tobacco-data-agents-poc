# -*- coding: utf-8 -*-
"""意图路由端到端回归：逐条问 Dify，检查意图是否正确、有没有误出报告文件。

判定规则（对应本次要修的病根）：
  · 只有 report 意图允许出现下载链接；
  · 其余意图一律不得出现「已生成报告」「下载报告 http」；
  · 兜底意图必须给出能力清单。
用法：python e2e_intent_test.py
"""
import json
import os
import re
import sys
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
DIFY = "http://127.0.0.1:8070"
KEY = open(os.path.join(HERE, "demo_key.txt"), encoding="utf-8").read().strip()
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

CASES = [
    ("报告", "给我出一份营销域的数据质量检测报告", "report"),
    ("报告", "出一份综管域的质检报告", "report"),
    ("追问", "哪些表质量问题最多？", "analysis"),
    ("追问", "各数据域问题数量排名", "analysis"),
    ("追问", "有效性问题主要出在哪些字段", "analysis"),
    ("追问", "营销域目前的数据质量怎么样？", "analysis"),
    ("对比", "9月和8月的数据质量对比怎么样", "compare"),
    ("规则", "联系电话空值，帮我生成一条校验规则", "rule"),
    ("规则", "客户信息表主键重复，帮我生成一条规则", "rule"),
    # 已下线两项：不单独作答、不出现"不提供"类字眼，合流到兜底话术（接住 + 能力清单 + 指路报告）
    ("原下线", "营销域规则执行情况怎么样？", "retired"),
    ("原下线", "本月哪些规则失败", "retired"),
    ("原下线", "规则通过率是多少", "retired"),
    ("原下线", "空值类问题以前是怎么处置的？", "retired"),
    ("兜底", "你好", "fallback"),
    ("兜底", "今天天气怎么样", "fallback"),
    ("兜底", "1+1等于几", "fallback"),
]


def ask(q):
    body = json.dumps({"inputs": {"use_classifier": "off"}, "query": q,
                       "response_mode": "blocking", "user": "e2e-intent",
                       "conversation_id": ""}).encode("utf-8")
    req = urllib.request.Request(DIFY + "/v1/chat-messages", data=body,
                                 headers={"Authorization": "Bearer " + KEY,
                                          "Content-Type": "application/json"})
    with OPENER.open(req, timeout=300) as r:
        return json.loads(r.read().decode("utf-8")).get("answer", "")


# 已下线能力：回答里若把这几个词当成「我能做」列出来，说明能力清单没收敛干净
RETIRED_WORDS = ["规则执行情况", "历史案例与处置", "历史案例", "追溯历史"]
# 「不说自己在哪方面不提供」—— 2026-10-21 用户要求：不许出现「本期暂不提供」这类字眼
BANNED = ["不提供", "不支持", "已下线", "不在服务范围", "尚未开放", "暂未提供"]


def has_cap_list(ans):
    """粗略判断是否给出了能力清单：有编号 + 至少两项能力名。"""
    n = len(re.findall(r"(?m)^\s*[①②③④⑤1-9][\.、）)]", ans))
    named = sum(1 for w in ("质检报告", "质检规则", "校验规则", "质量问题", "数据质量")
                if w in ans)
    return n >= 2 and named >= 2


def judge(ans, expect):
    """返回 (是否通过, 说明)。"""
    has_dl = bool(re.search(r"下载报告|\.docx", ans))
    says_report = "已生成" in ans and "报告" in ans
    leaked = [w for w in RETIRED_WORDS if w in ans]
    banned = [w for w in BANNED if w in ans]
    if expect == "report":
        return (has_dl, "报告意图应有下载链接" if not has_dl else "ok")
    if expect == "retired":
        # 已合流到兜底：不出文件、不出现"不提供"类字眼、给能力清单
        if has_dl or says_report:
            return (False, "问的是已下线能力，却出了报告文件")
        if banned:
            return (False, "出现禁用字眼 %s（用户要求不许说本期暂不提供）" % banned)
        if leaked:
            return (False, "把已下线能力当成'能做'列了出来：%s" % leaked)
        if not has_cap_list(ans):
            return (False, "没给出能力清单，等于把问题撂下了")
        return (True, "ok")
    if expect == "fallback":
        ok = has_cap_list(ans) and not has_dl
        if banned:
            return (False, "出现禁用字眼 %s" % banned)
        if ok and leaked:
            return (False, "兜底清单里仍列出已下线的能力：%s" % leaked)
        return (ok, "兜底应给能力清单且不出文件" if not ok else "ok")
    # 其余四类：不得出文件、不得说自己生成了报告
    if has_dl or says_report:
        return (False, "非报告意图却出了报告文件/声称已生成报告")
    if len(ans.strip()) < 30:
        return (False, "回答过短，可能没取到数")
    return (True, "ok")


def main():
    fails = []
    for tag, q, expect in CASES:
        try:
            ans = ask(q)
        except Exception as e:                                   # noqa: BLE001
            fails.append((tag, q, "调用异常：%s" % e))
            print("[ERR ] %-4s %-28s %s" % (tag, q[:26], e))
            continue
        ok, why = judge(ans, expect)
        print("[%s] %-4s %-28s %s" % ("PASS" if ok else "FAIL", tag, q[:26], why))
        if not ok:
            fails.append((tag, q, why))
            print("       ---- 回答节选 ----")
            print("       " + ans[:300].replace("\n", "\n       "))
    print("\n合计 %d 条，通过 %d 条，失败 %d 条"
          % (len(CASES), len(CASES) - len(fails), len(fails)))
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
