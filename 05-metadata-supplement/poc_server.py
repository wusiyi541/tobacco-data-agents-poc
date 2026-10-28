# -*- coding: utf-8 -*-
"""
元数据补充智能体 POC 本地服务（单进程，端口 8092）
  · GET  /                → 填报页面 index.html
  · GET  /api/catalog     → 目录清单（页面下拉）
  · GET  /api/catalog_get?catalog_code= → ① 查目录（Dify 分节点）
  · GET  /api/profile?catalog_code=     → ② 库表探查 limit 100（Dify 分节点）
  · GET  /api/task_info?catalog_code=   → ③ 加工任务解析 mock（Dify 分节点）
  · GET  /api/lineage?catalog_code=     → ④ 血缘上游+服务接口 mock（Dify 分节点）
  · GET  /api/pack?catalog_code=        → 四件事整包（调试用）
  · POST /api/ai_fill     → 代理调用 Dify 工作流（绕浏览器跨域）
"""
import json
import os
import sqlite3
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import unquote

BASE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(BASE, "meta_poc.db")
CATALOG = json.load(open(os.path.join(BASE, "catalog_5.json"), encoding="utf-8"))
CAT_MAP = {x["catalog_code"]: x for x in CATALOG}

DIFY_KEY_FILE = os.path.join(BASE, "meta_dify_key.txt")
DIFY_BASE = "http://127.0.0.1:8070/v1"

def table_of(code):
    return "t_" + code.split("/")[1]

# ------------------------------------------- mock：加工任务解析与血缘（对齐规范"自动采集技术元数据"四渠道）
# 规范要求技术元数据来自：行业中台库表信息、DataWorks/Dataphin 加工任务解析、共享中心数据服务接口。
# POC 用确定性 mock 模拟后两类，验证 LLM 依据"任务调度+血缘上游"时置信度是否提升。
TASK_MAP = {  # DataWorks / Dataphin 任务解析（模拟）
    "POC-DEMO/001": {"platform": "DataWorks", "task": "零售户基础信息日增量同步",
                             "schedule": "每日 01:30 调度", "freq_hint": "每日更新"},
    "POC-DEMO/002": {"platform": "DataWorks", "task": "全省零售户终端销售汇聚加工",
                             "schedule": "每日 03:00 调度（含各市局增量接入）", "freq_hint": "每日更新"},
    "POC-DEMO/003": {"platform": "Dataphin", "task": "专卖案件数据集成",
                             "schedule": "每日 04:00 调度", "freq_hint": "每日更新"},
    "POC-DEMO/004": {"platform": "DataWorks", "task": "真烟流入品规月度分析加工",
                             "schedule": "每月 1 日 05:00 调度", "freq_hint": "每月更新"},
    "POC-DEMO/005": {"platform": "Dataphin", "task": "综管技术社区指标加工",
                             "schedule": "每日 05:00 调度", "freq_hint": "每日更新"},
}
LINEAGE_MAP = {  # 表级血缘上游（模拟血缘接口返回）
    "POC-DEMO/001": {"source_system": "营销系统（客户档案）",
                             "upstream": ["CRM_零售客户档案表"]},
    "POC-DEMO/002": {"source_system": "营销系统（终端销售）",
                             "upstream": ["各市公司零售户终端销售流水", "零售户终端销售信息表(权威源)"]},
    "POC-DEMO/003": {"source_system": "专卖监管系统",
                             "upstream": ["专卖案件库_案件主表"]},
    "POC-DEMO/004": {"source_system": "专卖监管系统（案件+物流）",
                             "upstream": ["案件基本信息表", "物流配送明细表"]},
    "POC-DEMO/005": {"source_system": "综合管理平台",
                             "upstream": ["综管平台指标结果表"]},
}
SVC_MAP = {  # 共享中心数据服务接口（模拟）
    "POC-DEMO/001": {"api": "/ds/retail/basic/v1", "calls_30d": 328},
    "POC-DEMO/002": {"api": "/ds/retail/sales/v2", "calls_30d": 1024},
    "POC-DEMO/003": {"api": "/ds/enforce/case/v1", "calls_30d": 156},
    "POC-DEMO/004": {"api": "/ds/enforce/pinpei/v1", "calls_30d": 42},
    "POC-DEMO/005": {"api": "/ds/admin/metrics/v1", "calls_30d": 87},
}

# ---------------------------------------------------------------- 探查
def profile_table(code):
    x = CAT_MAP.get(code)
    if not x:
        return None
    tname = table_of(code)
    conn = sqlite3.connect(DB)
    conn.row_factory = sqlite3.Row
    cur = conn.cursor()
    total = cur.execute(f'SELECT COUNT(*) FROM "{tname}"').fetchone()[0]
    rows = cur.execute(f'SELECT * FROM "{tname}" LIMIT 100').fetchall()
    cols = [d[0] for d in cur.execute(f'SELECT * FROM "{tname}" LIMIT 1').description]
    fields = []
    for c in cols:
        vals = [r[c] for r in rows]
        nn = [v for v in vals if v is not None]
        null_pct = round(100 * (len(vals) - len(nn)) / max(len(vals), 1), 1)
        distinct = len(set(map(str, nn)))
        numeric = [v for v in nn if isinstance(v, (int, float))]
        f = {"name": c, "null_pct": null_pct, "distinct": distinct,
             "samples": [str(v) for v in nn[:3]]}
        if numeric and len(numeric) >= len(nn) * 0.8 and nn:
            f["type"] = "数值"
            f["min"] = min(numeric); f["max"] = max(numeric)
        else:
            f["type"] = "文本"
        fields.append(f)
    conn.close()
    return {"row_count": total, "sampled": len(rows), "limit": 100, "fields": fields}

def cat_of(code):
    """① 查目录：按编码定位目录元信息。"""
    x = CAT_MAP.get(code)
    if not x:
        return None
    return {k: x[k] for k in ("catalog_code", "catalog_name", "description",
                              "org", "data_type", "share_type", "fields",
                              "applied_count")}

def pack_of(code):
    """整包（兼容 /api/pack 调试）：四件事合一。"""
    cat = cat_of(code)
    if cat is None:
        return None
    return {"catalog": cat, "table_name": table_of(code),
            "profile": profile_table(code),
            "task_info": TASK_MAP.get(code),
            "lineage": LINEAGE_MAP.get(code),
            "service_api": SVC_MAP.get(code)}

# ---------------------------------------------------------------- Dify 代理
def dify_run(catalog_code):
    key = open(DIFY_KEY_FILE, encoding="utf-8").read().strip()
    body = json.dumps({"inputs": {"catalog_code": catalog_code},
                       "response_mode": "blocking", "user": "poc-user"}).encode()
    req = urllib.request.Request(DIFY_BASE + "/workflows/run", data=body, method="POST")
    req.add_header("Authorization", "Bearer " + key)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=180) as resp:
        obj = json.loads(resp.read().decode("utf-8"))
    outs = obj.get("data", {}).get("outputs", {})
    try:
        detail = json.loads(outs.get("fill_json") or "{}")
    except Exception:
        detail = {}
    fill = {}
    for k in ("business_desc", "business_rule", "update_freq", "source_system"):
        d = detail.get(k)
        if isinstance(d, dict):
            # 优先用 LLM 原始结构化建议（含 confidence / basis）
            fill[k] = {"value": str(d.get("value") or outs.get(k) or ""),
                       "confidence": d.get("confidence") or "mid",
                       "basis": d.get("basis") or ""}
        else:
            fill[k] = {"value": str(outs.get(k) or ""),
                       "confidence": "mid", "basis": ""}
    return {"workflow_id": obj.get("data", {}).get("workflow_id"),
            "elapsed": obj.get("data", {}).get("elapsed"),
            "total_tokens": obj.get("data", {}).get("total_tokens"),
            "fill": fill}

# ---------------------------------------------------------------- HTTP
class H(BaseHTTPRequestHandler):
    def _send(self, code, ctype, body):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj, code=200):
        self._send(code, "application/json; charset=utf-8",
                   json.dumps(obj, ensure_ascii=False).encode("utf-8"))

    def do_GET(self):
        path, _, qs = self.path.partition("?")
        if path in ("/", "/index.html"):
            body = open(os.path.join(BASE, "index.html"), "rb").read()
            self._send(200, "text/html; charset=utf-8", body)
        elif path == "/api/catalog":
            self._json([{k: x[k] for k in ("catalog_code", "catalog_name", "org",
                                           "share_type")} for x in CATALOG])
        elif path == "/api/pack":
            r = pack_of(unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                                .get("catalog_code", "")))
            if r is None:
                self._json({"ok": False, "error": "目录不存在"}, 404)
            else:
                r["ok"] = True
                self._json(r)
        elif path == "/api/catalog_get":          # ① 查目录
            code = unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                           .get("catalog_code", ""))
            cat = cat_of(code)
            if cat is None:
                self._json({"ok": False, "error": "目录不存在: " + code}, 404)
            else:
                self._json({"ok": True, "catalog": cat})
        elif path == "/api/profile":              # ② 读库表探查
            code = unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                           .get("catalog_code", ""))
            if code not in CAT_MAP:
                self._json({"ok": False, "error": "目录不存在: " + code}, 404)
            else:
                self._json({"ok": True, "table_name": table_of(code),
                            "profile": profile_table(code)})
        elif path == "/api/task_info":            # ③ 加工任务解析（mock）
            code = unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                           .get("catalog_code", ""))
            self._json({"ok": True, "task_info": TASK_MAP.get(code)})
        elif path == "/api/lineage":              # ④ 血缘上游 + 服务接口（mock）
            code = unquote(dict(p.split("=", 1) for p in qs.split("&") if "=" in p)
                           .get("catalog_code", ""))
            self._json({"ok": True, "lineage": LINEAGE_MAP.get(code),
                        "service_api": SVC_MAP.get(code)})
        elif path == "/api/ping":
            self._json({"ok": True})
        else:
            self._json({"error": "not found"}, 404)

    def do_POST(self):
        if self.path == "/api/ai_fill":
            n = int(self.headers.get("Content-Length", 0))
            try:
                arg = json.loads(self.rfile.read(n).decode("utf-8"))
                code = arg["catalog_code"]
            except Exception:
                return self._json({"ok": False, "error": "bad request"}, 400)
            if code not in CAT_MAP:
                return self._json({"ok": False, "error": "目录不存在"}, 404)
            try:
                self._json({"ok": True, **dify_run(code)})
            except Exception as e:
                self._json({"ok": False, "error": str(e)}, 502)
        else:
            self._json({"error": "not found"}, 404)

    def log_message(self, fmt, *args):
        print("[8092]", self.address_string(), fmt % args)

if __name__ == "__main__":
    print("元数据补充智能体 POC 服务: http://127.0.0.1:8092/")
    ThreadingHTTPServer(("0.0.0.0", 8092), H).serve_forever()
