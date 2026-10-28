# -*- coding: utf-8 -*-
"""智能寻数智能体 · 统一演示服务（端口 8095）

  GET  /              → 统一演示页 demo.html（选数车打底 + 右侧悬浮对话框）
  GET  /api/params    → Dify 开场白 + 开场建议问题（/v1/parameters 代理）
  GET  /api/catalogs  → 目录名称清单（前端解析「加入选数车」按钮对应的目录用）
  GET  /api/health    → 健康检查
  POST /api/chat      → Dify /chat-messages 流式代理（SSE 原样透传）

启动：python demo_server.py [--port 8095]
前置：Dify(8070) 已启动；血缘服务(8091) 建议一并启动（血缘问法出图）。
"""
import argparse
import http.client
import json
import os
import random
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
DIFY = os.environ.get("DIFY_BASE", "http://127.0.0.1:8070")
KEY_FILE = os.path.join(HERE, "app_key.txt")
CATALOG = json.load(open(os.path.join(HERE, "catalog.json"), encoding="utf-8"))

# 显式禁用代理：本机代理会拦 127.0.0.1 请求
OPENER = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# ── 过载保护 ──────────────────────────────────────────────────────────────
# MiniMax 529 = 服务端集群过载，不是本地限流。正确姿态：
#   1) 指数退避 + 抖动，别定间隔连打（连打只会把它压得更死）
#   2) 限并发，过载时少发而不是多发
#   3) 退避到上限仍未恢复，就把话说明白让用户稍后再试 / 换模型，
#      不要无限重试假装没事
MAX_CONCURRENCY = int(os.environ.get("DEMO_MAX_CONCURRENCY", "2"))
BACKOFF_BASE = float(os.environ.get("DEMO_BACKOFF_BASE", "3"))   # 首次退避秒数
BACKOFF_MAX_TRIES = int(os.environ.get("DEMO_BACKOFF_TRIES", "3"))  # 最多重试几次

_SLOTS = threading.BoundedSemaphore(MAX_CONCURRENCY)

# Dify 的 SSE 是紧凑 JSON（"event":"message"，冒号后无空格），
# 判字符串必须两种写法都覆盖，否则会漏判。
EV_MESSAGE = (b'"event": "message"', b'"event":"message"')
EV_ERROR = (b'"event": "error"', b'"event":"error"')


def backoff_delay(attempt):
    """指数退避 + ±30% 抖动：3s → 7s → 15s 附近，避免多个请求同一刻重试撞车。"""
    raw = BACKOFF_BASE * (2 ** attempt)
    return round(raw * (0.7 + random.random() * 0.6), 2)


def _is_overload(text):
    """判断上游返回是不是"服务端忙"。529 / overloaded / 集群负载高 都算。"""
    if not text:
        return False
    t = text if isinstance(text, str) else text.decode("utf-8", "replace")
    tl = t.lower()
    return ("529" in tl or "overloaded" in tl or "集群负载" in t
            or "server is busy" in tl or "service unavailable" in tl)


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
        elif u.path == "/api/suggest":
            # 下一步问题建议代理：Dify 用 message_id 单独拉取（开启应用功能开关后生效）
            from urllib.parse import parse_qs
            qs = parse_qs(u.query or "")
            mid = (qs.get("message_id") or [""])[0]
            usr = (qs.get("user") or ["demo-user"])[0]
            if not mid:
                self._json({"ok": False, "error": "message_id required"})
                return
            try:
                req = urllib.request.Request(
                    DIFY + "/v1/messages/%s/suggested?user=%s" % (mid, usr),
                    headers={"Authorization": "Bearer " + app_key()})
                obj = json.loads(OPENER.open(req, timeout=60).read().decode("utf-8"))
                self._json({"ok": True, "items": obj.get("data") or []})
            except Exception as e:                     # noqa: BLE001
                self._json({"ok": False, "error": str(e)})
        elif u.path == "/api/catalogs":
            self._json({"ok": True, "items": [
                {"name": x["name"], "code": x["code"], "org": x.get("org", ""),
                 "share": x.get("share", ""), "tags": x.get("tags", ""),
                 "desc": x.get("desc", "")} for x in CATALOG]})
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
        # 并发闸门：过载时少发而不是多发。等不到就明说，不硬塞进队列死等。
        if not _SLOTS.acquire(timeout=20):
            return self._json({"ok": False, "busy": True,
                               "error": "当前问问题的人较多，请稍候几秒再试"}, 429)
        try:
            self._chat(req)
        finally:
            _SLOTS.release()

    def _chat(self, req):
        """带退避重试的对话主流程。

        重试只发生在「正文一个字都还没发给前端」的阶段：此时前端只看到转圈，
        静默重来对用户是无感的；一旦正文开始输出就绝不重试，否则会串内容。
        """
        last = ""
        for attempt in range(BACKOFF_MAX_TRIES + 1):
            try:
                status, headers, body = self._open_dify(req)
            except Exception as e:                     # noqa: BLE001
                last = str(e)
                if attempt < BACKOFF_MAX_TRIES:
                    d = backoff_delay(attempt)
                    print("[8095] 建连失败，%.1fs 后重试（第 %d 次）：%s" % (d, attempt + 1, e), flush=True)
                    time.sleep(d)
                    continue
                return self._json({"ok": False, "error": last}, 502)

            if status != 200:
                err = body if isinstance(body, str) else str(body)
                last = err
                try:
                    body.close()
                except Exception:                      # noqa: BLE001
                    pass
                if _is_overload(err) and attempt < BACKOFF_MAX_TRIES:
                    d = backoff_delay(attempt)
                    print("[8095] 上游过载（%s），%.1fs 后重试（第 %d 次）" % (status, d, attempt + 1),
                          flush=True)
                    time.sleep(d)
                    continue
                if _is_overload(err):
                    return self._json({
                        "ok": False, "overload": True,
                        "error": "模型服务当前过载（529），已退避重试 %d 次仍未恢复。"
                                 "请稍后重试，或在 Dify 里切到备用模型。"
                                 % BACKOFF_MAX_TRIES}, 503)
                return self._json({"ok": False, "error": err[:300] or ("HTTP %s" % status)},
                                  status if status < 500 else 502)

            # 200：先预读，确认不是「开头就报过载」，再决定吐给前端
            outcome = self._stream(body, attempt)
            if outcome == "overload" and attempt < BACKOFF_MAX_TRIES:
                d = backoff_delay(attempt)
                print("[8095] 流内过载（正文未开始），%.1fs 后重试（第 %d 次）" % (d, attempt + 1),
                      flush=True)
                time.sleep(d)
                continue
            return

    def _stream(self, body, attempt):
        """预读一小段 + 直通。返回 'ok' 或 'overload'（仅当正文未开始就失败时）。

        预读窗口只留 1.5 秒：529 基本是请求发出瞬间就回，这么长足够判出来；
        再等下去前端进度条会僵住，得不偿失。窗口内没异常就直接放行。
        """
        head = b""
        committed = False
        try:
            deadline = time.time() + 1.5
            while time.time() < deadline:
                chunk = body.read1(4096)
                if not chunk:
                    break
                head += chunk
                if any(m in head for m in EV_MESSAGE) or b'"message_end"' in head:
                    committed = True
                    break
                if any(m in head for m in EV_ERROR):
                    break                                  # 已明确失败，直接判定
                if len(head) > 262144:                     # 兜底：异常大的头部直接放行
                    committed = True
                    break
        except Exception as e:                             # noqa: BLE001
            print("[8095] 预读异常：", repr(e), flush=True)

        if not committed and _is_overload(head):
            try:
                body.close()
            except Exception:                              # noqa: BLE001
                pass
            return "overload"

        # ── 阶段二：正式回包 + 直通 ──
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "close")            # 明确告知：答完就断
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.end_headers()
        wfile = self.wfile
        stop = threading.Event()
        tail_box = [head]

        def pump():                                        # 读上游 → 写下游
            try:
                while not stop.is_set():
                    chunk = body.read1(4096)
                    if not chunk:
                        break
                    wfile.write(chunk)
                    wfile.flush()
                    tail_box[0] = (tail_box[0] + chunk)[-8192:]
                    t = tail_box[0]
                    # Dify 发完收尾事件后不主动断连接：由我方判定结束，
                    # 两个事件都到齐才算完整（避免截断最后一个事件）
                    if ((b'"message_end"' in t and b'"workflow_finished"' in t)
                            or any(m in t for m in EV_ERROR)):
                        stop.set()
                        break
            except Exception as e:                         # noqa: BLE001
                print("[8095] 转发结束：", repr(e), flush=True)
            finally:
                stop.set()
                try:
                    body.close()
                except Exception:                          # noqa: BLE001
                    pass

        try:
            if head:
                wfile.write(head)
                wfile.flush()
        except Exception:                                  # noqa: BLE001
            pass

        th = threading.Thread(target=pump, daemon=True)
        th.start()
        try:
            while th.is_alive():
                th.join(0.2)
            th.join(3.0)          # 给它 3 秒把尾巴写完
        finally:
            stop.set()
            try:
                body.close()
            except Exception:                              # noqa: BLE001
                pass
            if tail_box[0]:
                print("[8095] 转发结束，累计 %d 字节" % len(tail_box[0]), flush=True)
            self.close_connection = True
            try:
                self.wfile.flush()
            except Exception:                              # noqa: BLE001
                pass
        return "ok"

    def _open_dify(self, req):
        """返回 (status, headers, body)；conversation_id 失效时自动开新会话重试一次。"""
        from urllib.parse import urlparse
        u = urlparse(DIFY)
        conn_cls = (http.client.HTTPSConnection if u.scheme == "https"
                    else http.client.HTTPConnection)
        base = {"inputs": {}, "query": req.get("query") or "",
                "response_mode": "streaming",
                "user": req.get("user") or "demo-user"}
        conv = (req.get("conversation_id") or "").strip()
        for attempt in (0, 1):
            payload = dict(base)
            payload["conversation_id"] = conv if attempt == 0 else ""
            conn = conn_cls(u.hostname, u.port, timeout=300)
            conn.request("POST", "/v1/chat-messages",
                         body=json.dumps(payload).encode("utf-8"),
                         headers={"Authorization": "Bearer " + app_key(),
                                  "Content-Type": "application/json",
                                  "Accept": "text/event-stream"})
            resp = conn.getresponse()
            if resp.status == 200:
                return 200, resp.headers, resp
            err = resp.read().decode("utf-8", "replace")
            conn.close()
            if attempt == 0 and conv and resp.status in (400, 404):
                print("[8095] conversation_id 无效，改开新会话重试：", err[:150])
                conv = ""
                continue
            return resp.status, resp.headers, err
        return 502, {}, "上游无响应"

    def log_message(self, fmt, *args):
        print("[8095]", fmt % args)


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8095)
    a = ap.parse_args()
    print("智能寻数统一演示服务 http://127.0.0.1:%d  (GET /, POST /api/chat)" % a.port)
    ThreadingHTTPServer(("0.0.0.0", a.port), Handler).serve_forever()
