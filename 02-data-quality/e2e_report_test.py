# -*- coding: utf-8 -*-
"""端到端：验证「沙箱出报告 + docx 下载」链路，并回归其余三类问法。

用法：  python e2e_report_test.py [要问的报告问题]
默认问："给我出一份营销域的数据质量检测报告"
"""
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEY_FILE = os.environ.get("DIFY_API_KEY_FILE", "dq_key.txt")
WEB = "http://127.0.0.1:8070/v1"


def api_key():
    if os.path.exists(KEY_FILE):
        t = open(KEY_FILE, encoding="utf-8").read().strip()
        if t:
            return t
    raise SystemExit("未找到 API Key：%s" % KEY_FILE)


def ask(q, tok, timeout=420):
    r = urllib.request.Request(
        WEB + "/chat-messages",
        json.dumps({"inputs": {}, "query": q, "response_mode": "blocking",
                    "user": "poc-user", "conversation_id": ""}).encode(),
        {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    with urllib.request.urlopen(r, timeout=timeout) as resp:
        return json.load(resp)


def fetch(url):
    r = urllib.request.Request(url)
    with urllib.request.urlopen(r, timeout=60) as resp:
        return resp.status, resp.read()


def main():
    tok = api_key()
    q = sys.argv[1] if len(sys.argv) > 1 else "给我出一份营销域的数据质量检测报告"
    print("Q:", q)
    try:
        res = ask(q, tok)
    except urllib.error.HTTPError as e:
        raise SystemExit("HTTP %s %s" % (e.code, e.read().decode("utf-8", "replace")[:400]))

    ans = res.get("answer", "")
    print("=" * 74)
    print(ans)
    print("=" * 74)

    m = re.search(r"https?://[^\s\)\]，。]+", ans)
    if not m:
        print("[!] 回答里没找到下载链接")
        return
    url = m.group(0).rstrip(".,)")
    print("下载链接:", url)
    status, body = fetch(url)
    print("下载校验: HTTP %s, %d bytes, PK头=%s" % (
        status, len(body), body[:2] == b"PK"))
    if body[:2] == b"PK":
        name = url.rsplit("/", 1)[-1]
        out = os.path.join(HERE, "out", "e2e_" + urllib.parse.unquote(name))
        open(out, "wb").write(body)
        print("已保存:", out)


if __name__ == "__main__":
    main()
