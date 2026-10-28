# -*- coding: utf-8 -*-
"""数据质检智能体 · 统一演示服务（端口 8096）

  GET  /              → 统一演示页 demo.html（共享平台打底 + 右侧悬浮对话框）
  GET  /api/params    → Dify 开场白 + 开场建议问题（/v1/parameters 代理）
  GET  /api/health    → 健康检查
  POST /api/chat      → Dify /chat-messages 流式代理（SSE 原样透传）

启动：python demo_server.py [--port 8096]
前置：Dify(8070) 已启动；质检 Mock API(8090) 建议一并启动（报告取数落盘）。
"""
import argparse
import json
import os
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DIFY = os.environ.get("DIFY_BASE", "http://127.0.0.1:8070")
KEY_FILE = os.path.join(HERE, "demo_key.txt")

OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))


def app_key():
    try:
        return open(KEY_FILE, encoding="utf-8").read().strip()
    except OSError:
        return ""


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path in ("/", "/index.html", "/demo", "/demo.html"):
            with open(os.path.join(HERE, "demo.html"), "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        elif u.path == "/api/params":
            try:
                req = urllib.request.Request(
                    DIFY + "/v1/parameters", headers={"Authorization": "Bearer " + app_key()})
                obj = json.loads(OPENER.open(req, timeout=8).read().decode("utf-8"))
                self._json({"ok": True,
                            "opening_statement": obj.get("opening_statement") or "",
                            "suggested_questions": obj.get("suggested_questions") or []})
            except Exception as e:                     # noqa: BLE001
                self._json({"ok": False, "error": str(e)})
        elif u.path == "/api/health":
            self._json({"ok": True, "dify": DIFY, "key_len": len(app_key())})
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        from urllib.parse import urlparse
        u = urlparse(self.path)
        if u.path != "/api/chat":
            return self._json({"ok": False, "error": "not found"}, 404)
        n = int(self.headers.get("Content-Length") or 0)
        try:
            req = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except ValueError:
            return self._json({"ok": False, "error": "请求体不是合法 JSON"}, 400)
        payload = {"inputs": {}, "query": req.get("query") or "",
                   "response_mode": "streaming",
                   "conversation_id": req.get("conversation_id") or "",
                   "user": req.get("user") or "demo-user"}
        try:
            r = urllib.request.Request(
                DIFY + "/v1/chat-messages",
                data=json.dumps(payload).encode("utf-8"),
                headers={"Authorization": "Bearer " + app_key(),
                         "Content-Type": "application/json"})
            up = OPENER.open(r, timeout=300)
        except Exception as e:                         # noqa: BLE001
            return self._json({"ok": False, "error": str(e)}, 502)
        self.send_response(200)
        self.send_header("Content-Type", up.headers.get("Content-Type",
                                                        "text/event-stream; charset=utf-8"))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Access-Control-Allow-Origin", "*")
        # 关键：SSE 流式响应没有 Content-Length，浏览器只能靠「连接关闭」判断流结束。
        # HTTP/1.1 keep-alive 下若不显式 close，写完后连接挂着，
        # 前端 reader.read() 永远等不到 done → state.busy 卡死 → 第二条消息发不出去。
        self.send_header("Connection", "close")
        self.close_connection = True
        self.end_headers()
        try:
            while True:
                chunk = up.read(1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (BrokenPipeError, ConnectionAbortedError):
            pass
        finally:
            up.close()

    def log_message(self, fmt, *args):
        print("[8096]", fmt % args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8096)
    a = ap.parse_args()
    print("数据质检统一演示服务 http://127.0.0.1:%d  (GET /, POST /api/chat)" % a.port)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
