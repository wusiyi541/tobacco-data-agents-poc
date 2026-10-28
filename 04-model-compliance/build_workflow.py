# -*- coding: utf-8 -*-
"""
创建 Dify Chatflow「数据模型符合性检查智能体-POC」。

链路（数由代码算，话由模型说）：
  开始（ddl_text 文件内容 / sys.query 直接输入）
    → 代码节点「解析与规则检查」：内嵌 rules_engine，输出确定性不符合项 + 评分
    → 条件分支
         ├ 识别到表结构 → LLM 成文 → 回答（附带机器可读清单，供前端导出）
         └ 未识别到表结构 → 回答（引导正确用法）

运行：python build_workflow.py   （需 Dify 控制台可用）
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dify_api import ensure_auth, req, raw_req  # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "数据模型符合性检查智能体-POC"
MODEL = {"provider": "langgenius/openai_api_compatible/openai_api_compatible",
         "name": "MiniMax-M3", "mode": "chat",
         "completion_params": {"temperature": 0.2, "top_p": 1,
                               "enable_thinking": False, "max_tokens": 2000}}


def req2(path, method="GET", body=None):
    """同 req，但接受 2xx（建应用 / 建 Key 返回 201）。"""
    code, obj = raw_req(path, method, body)
    if 200 <= code < 300:
        return obj
    print("HTTP %s %s %s -> %s" % (code, method, path,
                                   json.dumps(obj, ensure_ascii=False)[:300]))
    return None


# ---------------------------------------------------------------- 代码节点
WRAPPER = '''

# ---------------------------------------------------------------- Dify 入口
def main(query, ddl_text):
    """query: 对话输入；ddl_text: 前端上传文件后读到的文本（可为空）。"""
    src = (ddl_text or "").strip()
    if not src:
        src = (query or "").strip()
    res = check_text(src, "对话输入")
    if not res.get("ok"):
        return {"ok": "no", "error_msg": res.get("error", ""), "llm_input": "",
                "findings_json": "{}", "table_name": "", "total": 0, "score": 0}
    names = "、".join(t["name"] for t in res["tables"])
    payload = findings_payload(res)
    payload["pending"] = [p["attr"] for p in res["pending"]]
    return {"ok": "yes", "error_msg": "", "llm_input": render_evidence(res),
            "findings_json": json.dumps(payload, ensure_ascii=False, separators=(",", ":")),
            "table_name": names, "total": int(res["summary"]["total"]),
            "score": int(res["score"])}
'''


def load_engine_code():
    src = open(os.path.join(HERE, "rules_engine.py"), encoding="utf-8").read()
    cut = src.find("# ================================================================ CLI")
    if cut > 0:
        src = src[:cut]
    return src.rstrip() + WRAPPER


SYSTEM = """你是省烟草公司数据共享中心的「数据模型符合性检查智能体」，服务对象是数据运营组与各业务处室的建模人员。
你会拿到一段【确定性检查证据】：它由服务端规则引擎依据《烟草商业系统元数据管理规范（示例稿）》的元模型（5.2 数据库表元模型、5.3 数据库表字段元模型）与 7.2 规范性审核要求，对建表语句或表结构清单逐条判定后生成。

# 铁律
1. 只能使用证据里出现的事实：表名、字段名、数据类型、不符合项条数、规则编号、判据、建议改法、评分。严禁新增、合并、升级或改写任何检查结论。
2. 证据里没有的字段名、类型、条款一律不写。严禁编造字段或"规范原文"。
3. 不符合项为 0 时，直接说"全部通过"，不要为了凑内容臆造问题；只提示仍需人工填报的业务属性。
4. 表名、字段名、类型、数字必须原样引用（大小写、下划线都不要动）。
5. 只输出面向用户的答复正文，不输出 JSON、代码块、推理过程、免责套话。

# 输出格式（markdown，严格按此结构）
已解析 **<表名>**（<字段数> 个字段），依据《元数据管理规范》元模型与规范性审核要求逐项核对，共 **<X> 项不符合**（违反 a 项 / 风险 b 项 / 建议 c 项），合规评分 **<s>** 分：

**① <问题标题，如：字段命名不符合大写下划线规范>**（<k> 个字段 / 表级）
- 涉及：`字段1`、`字段2`
- 依据：<证据中的条款>
- 建议：<证据中的建议改法，逐条给出 旧 → 新>

**② ……**（每条不符合项一段，标题前用 ①②③ 递增；表级问题写「表级」，不写「1 个」）

结尾必须原样输出下面这个引用块（不加任何标题、不复述本条要求）：
> 以上为检查建议，字段重命名与类型调整需数据负责人确认后在编目页执行，助手不改动任何数据。

# 其他要求
- 字段多时，涉及字段最多列 8 个，其余写"等 k 个字段"。
- 单张表时首句已含表名，不要再加表名小标题；识别到多张表时才按表分节（每张表一个 `###` 小标题）。
- 若无不符合项：先给结论句，再用一句话说明"表级/字段级技术元数据齐全"，最后用列表给出需人工确认的 6 项业务与管理元数据（来自证据）。
- 语言简洁、面向建模人员，不要重复条款原文超过一句。"""

USER = """【确定性检查证据】
{{#code_check.llm_input#}}

请严格按系统提示的格式输出检查结论。"""


def node(nid, ntype, title, x, y, data, h=98):
    d = {"type": ntype, "title": title, "desc": "", "selected": False}
    d.update(data)
    return {"id": nid, "type": "custom", "position": {"x": x, "y": y},
            "positionAbsolute": {"x": x, "y": y}, "width": 244, "height": h,
            "sourcePosition": "right", "targetPosition": "left", "data": d}


def build_graph():
    start = {"id": "start_main", "type": "custom",
             "position": {"x": 60, "y": 300}, "positionAbsolute": {"x": 60, "y": 300},
             "width": 244, "height": 130, "sourcePosition": "right",
             "targetPosition": "left",
             "data": {"type": "start", "title": "开始", "desc": "", "selected": False,
                      "variables": [{
                          "variable": "ddl_text", "label": "建表语句文本（上传文件时由前端读入）",
                          "type": "paragraph", "required": False, "max_length": 20000,
                          "options": []}]}}

    code = node("code_check", "code", "解析与规则检查", 340, 300, {
        "variables": [{"variable": "query", "value_selector": ["sys", "query"]},
                      {"variable": "ddl_text", "value_selector": ["start_main", "ddl_text"]}],
        "code_language": "python3",
        "code": load_engine_code(),
        "outputs": {"ok": {"type": "string", "children": None},
                    "error_msg": {"type": "string", "children": None},
                    "llm_input": {"type": "string", "children": None},
                    "findings_json": {"type": "string", "children": None},
                    "table_name": {"type": "string", "children": None},
                    "total": {"type": "number", "children": None},
                    "score": {"type": "number", "children": None}},
    }, h=160)

    ifelse = node("if_ok", "if-else", "是否识别到表结构", 640, 300, {
        "logical_operator": "and",
        "conditions": [{"id": "c1", "variable_selector": ["code_check", "ok"],
                        "comparison_operator": "is", "value": "yes"}],
    }, h=120)

    llm = node("llm_compose", "llm", "成文检查结论", 900, 200, {
        "model": MODEL,
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYSTEM, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": USER, "edition_type": "basic"}],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": None,
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 2000},
    }, h=120)

    ans_ok = node("answer_ok", "answer", "回答-检查结论", 1180, 200, {
        "answer": "{{#llm_compose.text#}}\n\n<!--FINDINGS:{{#code_check.findings_json#}}-->",
        "variables": [],
    }, h=120)

    ans_fail = node("answer_fail", "answer", "回答-用法引导", 900, 440, {
        "answer": "{{#code_check.error_msg#}}",
        "variables": [],
    }, h=120)

    end = None  # advanced-chat 用 answer 节点收尾

    def edge(eid, src, tgt, st, tt, handle="source"):
        return {"id": eid, "type": "custom", "source": src, "sourceHandle": handle,
                "target": tgt, "targetHandle": "target",
                "data": {"sourceType": st, "targetType": tt,
                         "isInIteration": False, "isInLoop": False}, "zIndex": 0}

    return {"nodes": [start, code, ifelse, llm, ans_ok, ans_fail],
            "edges": [edge("e1", "start_main", "code_check", "start", "code"),
                      edge("e2", "code_check", "if_ok", "code", "if-else"),
                      edge("e3", "if_ok", "llm_compose", "if-else", "llm", "true"),
                      edge("e4", "llm_compose", "answer_ok", "llm", "answer"),
                      edge("e5", "if_ok", "answer_fail", "if-else", "answer", "false")],
            "viewport": {"x": 0, "y": 0, "zoom": 0.7}}


FEATURES = {
    "opening_statement": ("我是数据模型符合性检查助手。把建表语句（CREATE TABLE …）粘贴到对话框，"
                          "或用「＋」上传 .sql / 表结构 .csv 文件，我会依据《元数据管理规范》逐项核对，"
                          "给出不符合项、位置与建议改法。"),
    "suggested_questions": ["帮我检查 dwd_retail_cust_info 这张表是否符合规范",
                            "字段命名和类型有问题吗？",
                            "这张表能不能直接编目上架？"],
    "suggested_questions_after_answer": {"enabled": True},
    "retriever_resource": {"enabled": True},
    "text_to_speech": {"enabled": False, "voice": "", "language": ""},
}


def main():
    if not ensure_auth():
        raise SystemExit("Dify 控制台鉴权失败")

    apps = req("/console/api/apps?page=1&limit=100") or {"data": []}
    app = next((a for a in apps.get("data", []) if a.get("name") == APP_NAME), None)
    if app:
        app_id = app["id"]
        print("复用已有应用:", app_id)
    else:
        r = req2("/console/api/apps", "POST",
                 {"name": APP_NAME, "mode": "advanced-chat",
                  "icon_type": "emoji", "icon": "🧱", "icon_background": "#E0EAFB",
                  "description": "建表语句 / 表结构清单 → 按元数据规范逐项检查，输出不符合项与建议改法"})
        if not r:
            raise SystemExit("建应用失败")
        app_id = r["id"]
        print("新建应用:", app_id)

    cur = req("/console/api/apps/%s/workflows/draft" % app_id, quiet=True) or {}
    payload = {"graph": build_graph(), "features": FEATURES}
    if cur.get("hash"):
        payload["hash"] = cur["hash"]
    r = req("/console/api/apps/%s/workflows/draft" % app_id, "POST", payload)
    print("同步草稿:", "ok" if r is not None else "失败")
    if r is None:
        raise SystemExit(1)

    p = req("/console/api/apps/%s/workflows/publish" % app_id, "POST",
            {"marked_name": "v1", "marked_comment": "POC：解析+规则检查+成文"})
    print("发布:", "ok" if p is not None else "失败")

    k = req2("/console/api/apps/%s/api-keys" % app_id, "POST", {})
    key = (k or {}).get("token", "")
    open(os.path.join(HERE, "app_key.txt"), "w").write(key)
    open(os.path.join(HERE, "app_id.txt"), "w").write(app_id)
    print("app_id:", app_id)
    print("api_key:", key[:10] + "...（已写入 app_key.txt）")


if __name__ == "__main__":
    main()
