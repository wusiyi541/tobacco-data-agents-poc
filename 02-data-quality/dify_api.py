# -*- coding: utf-8 -*-
"""Dify 控制台 API 通用辅助（本机 8070）。

踩坑记录（2026-10-08，重要）：
  `login()` 用 `resp.headers.get_all("Set-Cookie")` 拿到的原始头，形如
      `csrf_token=xxx; Expires=...; Path=/; SameSite=Lax`
  里面用 **分号** 分隔属性；而 Netscape cookie jar 用 **制表符** 分隔 7 个字段。
  早期版本只按制表符解析 → 解析结果为空 dict → 请求不带 Cookie 与 CSRF
  → 一律 401 `Invalid Authorization token.`（表面像"令牌过期"，实为解析失败）。
  现在 `_cookies()` 同时兼容两种格式，并对过期 cookie 直接剔除 + 告警。
"""
import base64
import json
import os
import time
import urllib.error
import urllib.request

DIFY = os.environ.get("DIFY_BASE", "http://127.0.0.1:8070")
CJ = os.environ.get("DIFY_COOKIE_FILE", ".dify_cookie")


# ---------------------------------------------------------------- cookie jar
def _cookies(strict_expiry=True):
    """兼容解析 Netscape TSV 与原始 Set-Cookie 两种格式。"""
    m = {}
    if not os.path.exists(CJ):
        return m
    now = time.time()
    with open(CJ, encoding="utf-8") as f:
        for raw in f:
            line = raw.rstrip("\n")
            if not line.strip():
                continue
            if line.startswith("#HttpOnly_"):
                line = line[10:]
            elif line.startswith("#"):
                continue

            parts = line.split("\t")
            if len(parts) >= 7:                       # Netscape TSV
                exp, name, val = parts[4], parts[5], parts[6]
                try:
                    if strict_expiry and exp not in ("0", "") and float(exp) < now:
                        print("[cj] 跳过已过期 cookie: %s" % name)
                        continue
                except ValueError:
                    pass
                m[name] = val
            else:                                     # 原始 Set-Cookie
                first = line.split(";")[0].strip()
                if "=" in first:
                    k, v = first.split("=", 1)
                    m[k.strip()] = v.strip()
    return m


def _write_netscape(jar_text):
    """把原始 Set-Cookie 文本转成 Netscape TSV 落盘（带过期时间）。"""
    now = int(time.time())
    rows = []
    for c in jar_text.splitlines():
        c = c.strip()
        if not c:
            continue
        chunks = [x.strip() for x in c.split(";")]
        first = chunks[0]
        if "=" not in first:
            continue
        name, val = first.split("=", 1)
        exp = 0
        for a in chunks[1:]:
            low = a.lower()
            if low.startswith("max-age="):
                try:
                    exp = now + int(a.split("=", 1)[1])
                except ValueError:
                    pass
        rows.append("\t".join(["127.0.0.1", "TRUE", "/", "FALSE", str(exp),
                               name.strip(), val.strip()]))
    with open(CJ, "w", encoding="utf-8") as f:
        f.write("\n".join(rows) + "\n")
    return rows


CK = _cookies()
CSRF = CK.get("csrf_token", "")


# ---------------------------------------------------------------- request
def raw_req(path, method="GET", body=None, timeout=90, cookies=None, csrf=None):
    data = json.dumps(body).encode("utf-8") if body is not None else None
    r = urllib.request.Request(DIFY + path, data=data, method=method)
    r.add_header("Content-Type", "application/json")
    ck = CK if cookies is None else cookies
    r.add_header("Cookie", "; ".join("%s=%s" % (k, v) for k, v in ck.items()))
    r.add_header("X-CSRF-Token", CSRF if csrf is None else csrf)   # GET 也带
    try:
        with urllib.request.urlopen(r, timeout=timeout) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as e:
        try:
            return e.code, json.loads(e.read().decode("utf-8") or "{}")
        except Exception:
            return e.code, {}


def req(path, method="GET", body=None, quiet=False, timeout=90):
    code, obj = raw_req(path, method, body, timeout)
    if code != 200 and not quiet:
        print("HTTP %s %s %s -> %s" % (code, method, path,
                                       json.dumps(obj, ensure_ascii=False)[:400]))
        return None
    return obj if code == 200 else None


# ---------------------------------------------------------------- login
def login(password="DifyPoc2026", email="admin@dify.poc"):
    """重新登录并把 cookie 转成 Netscape 格式落盘。返回新的 CK dict。"""
    global CK, CSRF
    b64 = base64.b64encode(password.encode()).decode()
    r = urllib.request.Request(
        DIFY + "/console/api/login",
        data=json.dumps({"email": email, "password": b64,
                         "language": "zh-Hans"}).encode("utf-8"),
        method="POST")
    r.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(r, timeout=30) as resp:
        hdr = resp.headers.get_all("Set-Cookie") or []
    _write_netscape("\n".join(hdr))
    CK = _cookies()                      # 刷新模块级缓存
    CSRF = CK.get("csrf_token", "")
    return CK


def ensure_auth():
    """探测一次；401/403 就重登一次（幂等，可放心在脚本开头调用）。"""
    global CK, CSRF
    code, _ = raw_req("/console/api/apps?page=1&limit=1")
    if code in (401, 403):
        print("[auth] 令牌失效（HTTP %s），重新登录…" % code)
        login()
        code, _ = raw_req("/console/api/apps?page=1&limit=1")
    print("[auth] 控制台可用 = %s" % (code == 200))
    return code == 200


if __name__ == "__main__":
    if not ensure_auth():
        raise SystemExit("登录失败")
    print("cookie 项:", sorted(CK.keys()))
