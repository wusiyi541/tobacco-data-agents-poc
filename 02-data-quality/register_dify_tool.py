# -*- coding: utf-8 -*-
"""把数据质检 Mock API 注册为 Dify 工作区自定义工具（幂等：已存在则更新）。"""
import json
import os
import sys
import urllib.request

DIFY = "http://127.0.0.1:8070"
HERE = os.path.dirname(os.path.abspath(__file__))
CJ = os.environ.get("DIFY_COOKIE_FILE", ".dify_cookie")
PROVIDER = "dq_compute"
SCHEMA_URL = "http://127.0.0.1:8090/openapi.json"


def cookies():
    """解析 Netscape cookie jar；注意 access_token 那几行带 '#HttpOnly_' 前缀。"""
    m = {}
    with open(CJ, encoding="utf-8") as f:
        for line in f:
            line = line.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#HttpOnly_"):
                line = line[len("#HttpOnly_"):]
            elif line.startswith("#"):
                continue
            p = line.split("\t")
            if len(p) >= 7:
                m[p[5]] = p[6]
    return m


CK = cookies()
CSRF = CK.get("csrf_token", "")


def req(path, method="GET", body=None, raw=False):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(DIFY + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    r.add_header("Cookie", "; ".join("%s=%s" % (k, v) for k, v in CK.items()))
    r.add_header("X-CSRF-Token", CSRF)          # GET 也要带，否则 401
    try:
        with urllib.request.urlopen(r, timeout=60) as resp:
            txt = resp.read().decode("utf-8")
            return json.loads(txt) if not raw else txt
    except urllib.error.HTTPError as e:
        print("HTTP %s: %s" % (e.code, e.read().decode("utf-8")[:400]))
        return None


# 取最新 schema
with urllib.request.urlopen(SCHEMA_URL, timeout=15) as f:
    schema = json.loads(f.read().decode("utf-8"))
schema["servers"] = [{"url": "http://host.docker.internal:8090"}]

payload = {
    "provider": PROVIDER,
    "icon": {"background": "#E4FBCC", "content": "🔍"},
    "credentials": {"auth_type": "none", "api_key_header": "api_key", "api_key_value": ""},
    "schema_type": "openapi",
    "schema": json.dumps(schema, ensure_ascii=False),
    "privacy_policy": "",
    "custom_disclaimer": "",
    "labels": [],
}

# 先看是否已存在
exist = req("/console/api/workspaces/current/tool-provider/api/get?provider=%s" % PROVIDER)
if exist and exist.get("provider") == PROVIDER:
    print("已存在，执行更新 ...")
    payload["original_provider"] = PROVIDER
    r = req("/console/api/workspaces/current/tool-provider/api/update", "POST", payload)
else:
    print("注册新工具供应商 ...")
    r = req("/console/api/workspaces/current/tool-provider/api/add", "POST", payload)

print("结果:", json.dumps(r, ensure_ascii=False)[:300])

tools = req("/console/api/workspaces/current/tool-provider/api/tools?provider=%s" % PROVIDER)
if tools:
    for t in tools.get("tools", []):
        print("  · %-18s %s" % (t.get("name"), t.get("description", "")[:52]))
