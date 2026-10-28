# -*- coding: utf-8 -*-
"""
需求状态追踪 POC · 端到端测试（含权限用例）
经 Dify /v1/chat-messages 打完整链路：身份 + 用户话术 → Chatflow 取数 → LLM 成文。

运行：
    python e2e_test.py            # 直连版
    python e2e_test.py --router   # 路由版
    python e2e_test.py -u 张枫 -q "我的需求单到哪一步了"
"""
import argparse
import json
import os
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
URL = "http://127.0.0.1:8070/v1/chat-messages"

# (用户, 问题, 期望要点)
CASES = [
    ("张枫", "我的需求单到哪一步了", "只列本人 1 张单"),
    ("张枫", "综管个人积分统计表的进度", "本人单据详情"),
    ("张枫", "20261012124425015264 到哪一步了", "越权 → 拒绝，且不泄露字段"),
    ("张枫", "陈汝斌的需求单什么状态", "越权 → 拒绝"),
    ("张鹏宇", "现在有哪些需求单在审核，审核人是谁，等了多久", "处理人视角，范围收窄"),
    ("张鹏宇", "我的需求单整体情况", "统计仅限本人相关"),
    ("陈汝斌", "用数单要经过哪些审核环节", "流程口径（无需单据）"),
    ("张枫", "今天天气怎么样", "无关问题 → 引导"),
]


def key_of(router):
    p = os.path.join(HERE, "app_key_router.txt" if router else "app_key.txt")
    with open(p) as f:
        return f.read().strip()


def chat(query, user, router=False, inputs=True):
    body = {"inputs": {"user_name": user} if inputs else {},
            "query": query, "response_mode": "blocking", "user": "poc-e2e"}
    r = urllib.request.Request(URL, data=json.dumps(body).encode("utf-8"), method="POST")
    r.add_header("Content-Type", "application/json")
    r.add_header("Authorization", "Bearer " + key_of(router))
    with urllib.request.urlopen(r, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--router", action="store_true")
    ap.add_argument("-u", "--user")
    ap.add_argument("-q", "--query")
    a = ap.parse_args()

    cases = [(a.user, a.query, "")] if (a.user and a.query) else CASES
    print("编排 =", "路由版" if a.router else "直连版")
    for user, q, expect in cases:
        print("=" * 30)
        print("[%s] %s%s" % (user, q, ("   （期望：%s）" % expect) if expect else ""))
        try:
            print(chat(q, user, a.router).get("answer", "(空)").strip()[:900])
        except Exception as e:                      # noqa: BLE001
            print("失败:", e)
        print()
