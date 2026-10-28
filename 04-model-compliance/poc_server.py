# -*- coding: utf-8 -*-
"""
数据模型符合性检查智能体 · POC 本地服务（端口 8093）

职责：
  1) 托管对话页面（按 V5 屏 5 复刻）；
  2) 提供样例清单与样例正文，便于演示「直接输入 / 上传文件」两条路径；
  3) /api/chat 代理到 Dify，隐藏 API Key、规避浏览器跨域。

启动：.venv\\Scripts\\python.exe poc_server.py
"""
import http.server
import json
import os
import re
import socketserver
import sys
import urllib.error
import urllib.request
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")
DIFY = os.environ.get("DIFY_BASE", "http://127.0.0.1:8070")
PORT = int(os.environ.get("POC_PORT", "8093"))
KEY_FILE = os.path.join(HERE, "app_key.txt")

# 显式禁用代理：本机代理会拦 127.0.0.1 请求（见项目记忆）
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# 规则引擎（页面嵌入形态直接本地出结果，不经大模型）
sys.path.insert(0, HERE)
import rules_engine as R  # noqa: E402


def app_key():
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


def sample_label(name):
    path = os.path.join(SAMPLES, name)
    try:
        text = open(path, encoding="utf-8").read()
    except OSError:
        return name
    m = re.search(r"样例\s*\d+\s*（([^）]*)）：([^\n]+)", text)
    if m:
        tail = re.split(r"[—\-]{2,}", m.group(2).strip())[0].strip()
        return "%s · %s" % (m.group(1), tail)
    # CSV：取表中文名 / 表英文名
    rows = [r for r in text.replace("\ufeff", "").splitlines() if r.strip()]
    if len(rows) > 1:
        head = rows[0].split(",")
        for r in rows[1:]:
            cells = r.split(",")
            if len(cells) >= 2:
                if len(cells) > 1 and cells[1].strip():
                    return "表结构清单 · %s" % cells[1].strip()
                if cells[0].strip():
                    return "表结构清单 · %s" % cells[0].strip()
    return name


def list_samples():
    out = []
    for f in sorted(os.listdir(SAMPLES)):
        if f.lower().endswith((".sql", ".csv")):
            out.append({"name": f, "label": sample_label(f),
                        "size": os.path.getsize(os.path.join(SAMPLES, f))})
    return out


def read_sample(name):
    if not re.match(r"^[\w\-.]+\.(sql|csv)$", name):
        raise ValueError("非法文件名")
    path = os.path.join(SAMPLES, name)
    if not os.path.exists(path):
        raise ValueError("样例不存在")
    raw = open(path, "rb").read()
    for enc in ("utf-8-sig", "utf-8", "gbk"):
        try:
            return raw.decode(enc)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", "replace")


def sample_fields(name):
    """解析样例的字段清单，供页面嵌入形态的「数据项」表格展示。"""
    text = read_sample(name)
    r = R.check_text(text, name)
    if not r["ok"] or not r["tables"]:
        raise ValueError(r.get("error") or "样例解析失败")
    t = r["tables"][0]
    fields = [c for c in t["columns"] if not c.get("partition")]
    return {"ok": True, "name": name, "table": t["name"],
            "cn": t.get("comment", ""), "dialect": r.get("dialect", ""),
            "fields": fields}


def check_sample(name):
    """页面嵌入形态的一键检查：本地规则引擎直接出结果，不经大模型。"""
    text = read_sample(name)
    r = R.check_text(text, name)
    if not r["ok"]:
        raise ValueError(r.get("error") or "样例解析失败")
    payload = R.findings_payload(r)
    t = r["tables"][0]
    return {"ok": True, "name": name, "table": t["name"], "dialect": r.get("dialect", ""),
            "payload": payload}


def call_dify(query, ddl_text, file_name=""):
    body = {"inputs": {"ddl_text": ddl_text}, "query": query,
            "response_mode": "blocking", "user": "poc-portal",
            "conversation_id": "", "files": []}
    req = urllib.request.Request(DIFY + "/v1/chat-messages",
                                 data=json.dumps(body).encode("utf-8"), method="POST")
    req.add_header("Authorization", "Bearer " + app_key())
    req.add_header("Content-Type", "application/json")
    with OPENER.open(req, timeout=240) as resp:
        return json.loads(resp.read().decode("utf-8"))


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, fmt, *args):
        sys.stderr.write("%s - %s\n" % (self.address_string(), fmt % args))

    def _send(self, code, payload, ctype="application/json; charset=utf-8"):
        data = payload if isinstance(payload, bytes) else json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def do_GET(self):
        u = urlparse(self.path)
        if u.path in ("/", "/index.html"):
            with open(os.path.join(HERE, "index.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path in ("/demo", "/demo.html"):
            with open(os.path.join(HERE, "demo.html"), "rb") as f:
                return self._send(200, f.read(), "text/html; charset=utf-8")
        if u.path == "/api/params":
            # Dify 应用参数（开场白 + 开场建议问题），供统一演示页拉取
            try:
                req = urllib.request.Request(
                    DIFY + "/v1/parameters", headers={"Authorization": "Bearer " + app_key()})
                obj = json.loads(OPENER.open(req, timeout=8).read().decode("utf-8"))
                return self._send(200, {"ok": True,
                                        "opening_statement": obj.get("opening_statement") or "",
                                        "suggested_questions": obj.get("suggested_questions") or []})
            except Exception as e:                     # noqa: BLE001
                return self._send(200, {"ok": False, "error": str(e)})
        if u.path == "/api/samples":
            return self._send(200, {"ok": True, "items": list_samples()})
        if u.path == "/api/sample":
            name = (parse_qs(u.query).get("name") or [""])[0]
            try:
                return self._send(200, {"ok": True, "name": name, "text": read_sample(name)})
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
        if u.path == "/api/sample_fields":
            name = (parse_qs(u.query).get("name") or [""])[0]
            try:
                return self._send(200, sample_fields(name))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:                     # noqa: BLE001
                return self._send(500, {"ok": False, "error": "解析失败：%s" % e})
        if u.path == "/api/health":
            return self._send(200, {"ok": bool(app_key()), "key_len": len(app_key()),
                                    "dify": DIFY})
        return self._send(404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = urlparse(self.path).path
        if path == "/api/check":
            # 页面嵌入形态：本地规则引擎一键检查（不经大模型）
            n = int(self.headers.get("Content-Length") or 0)
            try:
                req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
            except ValueError:
                return self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
            name = (req.get("sample") or "").strip()
            if not name:
                return self._send(400, {"ok": False, "error": "请先在「库表名称」中选择要检查的表"})
            try:
                return self._send(200, check_sample(name))
            except ValueError as e:
                return self._send(400, {"ok": False, "error": str(e)})
            except Exception as e:                     # noqa: BLE001
                return self._send(500, {"ok": False, "error": "检查失败：%s" % e})
        if path != "/api/chat":
            return self._send(404, {"ok": False, "error": "not found"})
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return self._send(400, {"ok": False, "error": "请求体不是合法 JSON"})
        query = (req.get("query") or "").strip()
        ddl_text = req.get("ddl_text") or ""
        if not query and not ddl_text:
            return self._send(400, {"ok": False, "error": "请输入要检查的语句，或上传文件"})
        if not app_key():
            return self._send(500, {"ok": False, "error": "未找到 app_key.txt，请先运行 build_workflow.py"})
        try:
            obj = call_dify(query or "帮我检查这张表是否符合元数据规范", ddl_text,
                            req.get("file_name", ""))
        except urllib.error.HTTPError as e:
            detail = e.read().decode("utf-8", "replace")[:400]
            return self._send(502, {"ok": False, "error": "Dify 返回 %s：%s" % (e.code, detail)})
        except Exception as e:  # noqa: BLE001
            return self._send(502, {"ok": False, "error": "调用 Dify 失败：%s" % e})
        answer = obj.get("answer", "")
        return self._send(200, {"ok": True, "answer": answer,
                                "message_id": obj.get("message_id", ""),
                                "usage": obj.get("metadata", {}).get("usage", {})})


class Server(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


if __name__ == "__main__":
    os.chdir(HERE)
    print("数据模型符合性检查智能体 POC：http://127.0.0.1:%d/" % PORT)
    print("样例目录：%s" % SAMPLES)
    Server(("127.0.0.1", PORT), Handler).serve_forever()
