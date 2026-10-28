# -*- coding: utf-8 -*-
"""对话接口回归：把 samples 下每个文件当作上传文件内容送入工作流，打印结论摘要。"""
import json
import os
import sys
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
KEY = open(os.path.join(HERE, "app_key.txt")).read().strip()
BASE = "http://127.0.0.1:8070/v1/chat-messages"
SAMPLES = os.path.join(os.path.dirname(HERE), "samples")


def ask(query, ddl_text="", user="poc-check"):
    body = {"inputs": {"ddl_text": ddl_text}, "query": query,
            "response_mode": "blocking", "user": user,
            "conversation_id": "", "files": []}
    r = urllib.request.Request(BASE, data=json.dumps(body).encode("utf-8"), method="POST")
    r.add_header("Authorization", "Bearer " + KEY)
    r.add_header("Content-Type", "application/json")
    t = time.time()
    try:
        with urllib.request.urlopen(r, timeout=180) as resp:
            obj = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"error": "HTTP %s %s" % (e.code, e.read().decode("utf-8")[:300])}
    obj["_sec"] = round(time.time() - t, 1)
    return obj


def main():
    only = sys.argv[1] if len(sys.argv) > 1 else ""
    files = sorted(f for f in os.listdir(SAMPLES) if f.lower().endswith((".sql", ".csv")))
    for f in files:
        if only and only not in f:
            continue
        text = open(os.path.join(SAMPLES, f), encoding="utf-8").read()
        obj = ask("帮我检查这张表是否符合元数据规范", text)
        if obj.get("error"):
            print("✗ %-34s %s" % (f, obj["error"]))
            continue
        ans = obj.get("answer", "")
        head = ans.split("\n")[0][:110]
        body = ans.split("<!--FINDINGS:")[0]
        print("✓ %-34s %5ss  %s" % (f, obj["_sec"], head))
        print("    字符数 %d，含清单附录：%s" % (len(body), "是" if "<!--FINDINGS:" in ans else "否"))
        print()


if __name__ == "__main__":
    main()
