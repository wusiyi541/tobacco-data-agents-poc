# -*- coding: utf-8 -*-
"""端到端：为已发布 Chatflow 建 API Key（或复用），调 /v1/chat-messages 三问。"""
import json, urllib.request, urllib.error
from dify_api import req  # 复用控制台鉴权（cookie/CSRF）

APP = "6ac068d9-77ea-4f09-95a3-8265b63b9178"
WEB = "http://localhost:8070/v1"

def console(path, method="GET", body=None):
    return req(path, method, body) if body is not None else req(path, quiet=True)

# 1) 找现成 key
keys = req("/console/api/apps/%s/api-keys" % APP, quiet=True) or {}
items = keys.get("data") or keys.get("items") or []
if isinstance(keys, list):
    items = keys
tok = items[0]["token"] if items else None
if not tok:
    r = req("/console/api/apps/%s/api-keys" % APP, "POST", {"name": "poc-e2e"})
    tok = (r or {}).get("token")
    print("新建 API Key:", "OK" if tok else r)
else:
    print("复用已有 API Key:", tok[:8] + "..." if tok else None)
if not tok:
    raise SystemExit("无可用 API Key")

# 2) 三问
QS = ["营销域目前的数据质量怎么样？",
      "客户信息表的主键有重复，帮我生成一条校验规则",
      "主键重复这类问题以前是怎么处理的？"]
for q in QS:
    reqo = urllib.request.Request(
        WEB + "/chat-messages",
        json.dumps({"inputs": {}, "query": q, "response_mode": "blocking",
                    "user": "poc-user", "conversation_id": ""}).encode(),
        {"Authorization": "Bearer " + tok, "Content-Type": "application/json"})
    try:
        r = json.load(urllib.request.urlopen(reqo, timeout=300))
        ans = r.get("answer", "")
        print("\n" + "=" * 72)
        print("Q:", q)
        print("-" * 72)
        print(ans[:1400])
    except urllib.error.HTTPError as e:
        print("\nQ:", q, "-> HTTP", e.code, e.read().decode("utf-8", "replace")[:300])
