# -*- coding: utf-8 -*-
"""
需求状态追踪 POC · 构建并发布 Dify Chatflow

两种编排，同一套取数服务与提示词：
  直连版（默认）「需求状态追踪POC」
      开始(含 user_name) → HTTP /req/ask → LLM 成文 → 回答
      · 意图路由在服务端完成，Dify 侧无分支节点，链路最短、最好维护。

  路由版（--router）「需求状态追踪POC-路由版」
      开始(含 user_name) → 问题分类器 ├─ 1 单/多单进度 → HTTP /req/lookup  ┐
                                     ├─ 2 统计与在办 → HTTP /req/overview ┤→ 变量聚合 → LLM 成文 → 回答
                                     ├─ 3 流程口径   → HTTP /req/flow    ┘
                                     └─ 4 无关问题   → 固定话术回答
      · 意图判断变成 workflow 里看得见的分支，每类可接不同接口/工具；
      · 代价：多一次分类模型调用（延迟 + token），且分类错会走错分支。

权限（两版一致）：
  · 开始节点 user_name 由门户登录态透传（POC 页面手输/下拉），
    每一路 HTTP 都带 user 参数，服务端按「本人申请 + 本人参与审核/处理」收窄；
  · 模型只拿到收窄后的事实，denied/need_user 时事实里只有拒绝理由，无任何单据字段。

运行：
    python build_chatflow.py            # 直连版
    python build_chatflow.py --router   # 路由版
"""
import argparse
import json
import os

from dify_api import ensure_auth, req, raw_req

API_BASE = "http://host.docker.internal:8094"
HERE = os.path.dirname(os.path.abspath(__file__))

APP_DIRECT = "需求状态追踪POC"
APP_ROUTER = "需求状态追踪POC-路由版"

MODEL = {
    "provider": "langgenius/openai_api_compatible/openai_api_compatible",
    "name": "MiniMax-M3", "mode": "chat",
    "completion_params": {"temperature": 0.2, "top_p": 1, "enable_thinking": False},
}

SYSTEM_PROMPT = """你是省烟草公司数据共享中心的「需求状态追踪助手」，面向需求申请人，
回答需求单所处环节、当前审核人与已等待时长（V5 口径）。

# 铁律（违反即为事故）
1. 所有需求单号、目录名、人名、状态、时间、等待时长，只能来自「取数结果」，严禁改动、估算或补充。
1.1 **状态与环节原文照抄**：原样复制，括号必须是**英文半角**——
    「已签收(待评论)」不得写成「已签收（待评价）」，也不得写成「已签收（待评论）」；
    「实施完成(待签收)」不得写成「已完成待签收」。用词、括号、顺序都照原文。
2. 结果里没有的直接说「未取到该项数据」，不推断、不编造。
3. **不做催办**：可以告知当前审核人与已等待时长，但不得承诺加急、催促或给出处理时限。
4. **不预测审批结果**：不得说"应该快通过了"之类的话。
5. 不得编造故障：仅当结果中 ok=false 或出现 error 字段时才可描述失败；ok=true 一律按成功处理。
6. 不得输出 JSON、字段名、变量名等技术细节，不输出思考过程。
7. 若"状态为推导值"为 true，需说明该状态是根据流转轨迹推导、建议以系统页面为准。
8. **权限（最高优先级）**：
   8.1 取数结果已按当前用户收窄，只能使用结果中出现的单据；不得凭记忆或推测补充其他单据。
   8.2 intent = denied 时，**严禁**说明该单据是否存在、单号、申请人、目录、状态或任何字段，
       只回复：该需求单不在您的查询范围（仅可查询本人申请或本人参与审核/处理的需求单），
       如需查询请联系数据共享中心。
   8.3 intent = need_user 时，提示用户先在页面顶部选择或输入本人姓名，不回答任何单据信息。
   8.4 intent = empty 时，说明其名下暂无需求单，建议核对账号；不得推测"可能有过单但已删除"。
   8.5 统计类回答必须写明"统计范围为与您相关的 N 张单"，**不得说成全省/全量**。

# 按 intent 选择作答模板

**intent = detail（单单进度查询）** —— 结论先行：
  ① 一句话给结论：该单当前处于〈进度条当前环节〉，最新状态〈最新状态〉；
  ② 用一行"进度条"呈现七阶段：发起→申请→审核→实施→签收→评价→办结，
     已完成的打 ✔，当前的用【当前】标出，未到的如实列出；若某环节"未通过/终止"要明确标注；
  ③ 若是审核中：给出审核环节（如主管部门审核）、当前审核人、已等待时长；
     若不在审核环节：给最近一次流转时间与距今时长，以及签收状态/评价状态（若有），
     **不要输出「当前审核人：未取到」这类空字段行**；
  ④ 若该单下有多个实施单且状态不一，简要列出（编号可截尾显示前 6 位+…）；
  ⑤ 引用 2-3 条最近的流转轨迹（时间 + 环节 + 处理人）作为依据。
  需求项很多时只汇总条数与目录名，不全量罗列。

**intent = list（命中多张单 / 我的单据清单）** —— 逐单一行：单号（可截尾）｜目录｜最新状态｜
  当前环节｜等待时长，结尾提示用户报出具体单号或目录名可看详情。

**intent = overview（整体统计 / 在办情况）** —— ① 说明统计范围为当前用户相关的 N 张单；
  ② 状态分布；③ 各环节分布一句话概括；
  ④ **审核中在办**逐单列：单号｜审核环节｜当前审核人｜已等待时长（本助手核心价值，必须列出）；
  ⑤ 一句话说明数据截至时间。**不催办、不建议加急。**
  若用户问"我是谁 / 我能查哪些单"，依据结果里的查询范围（用户、角色、单数）回答。

**intent = flow（流程与口径说明）** —— 按取数结果里的进度条与四环节口径作答，
  说明各环节的状态分类，并声明：当前审核人与已等待时长由流程数据计算，不由模型估计。

**intent = unknown** —— 礼貌引导：可报需求单号 / 目录名称查进度，或问「我的需求单整体情况」
  「用数单有哪些环节」。

# 其他
- 回答语言中文，简洁、结论先行；表格列不超过 5 列。
- 直接说事，**不要以用户姓名或「您好」开头**，也不要输出半截加粗符号。
- 不得自造"办结率""超期"等结果里没有的指标。"""

COMPOSE_USER = """用户问题：{{#sys.query#}}

【取数结果】（服务端已完成权限收窄与全部计算）
%s

请严格依据以上结果作答，一个数字、一个人名、一个时间都不要改动或补充。"""

ROUTER_INSTRUCTION = """判断用户这句话属于哪一类。只依据问题本身，不要臆测用户身份。

1 = 查询需求单进度：用户明确指向某一张（或某几张）具体需求单，想知道它现在到哪一步、
    卡在哪个环节、审核人是谁、等了多久；包括「我的需求单到哪一步了」这类要看单据明细的问题。
    特征：句子里有单号、目录名、表名，或特指某张单。
2 = 统计与在办情况：用户要的是数量、分布、汇总、清单层面的结论，没有特指某一张单。
    典型：「我有几张单」「整体情况怎么样」「现在有哪些需求单在审核」「有哪些单在等审批」
    「哪些单卡住了」「审核人是谁、等了多久」（复数、清单语气）。
3 = 流程与口径：用户问用数单的整体流程、要经过哪些审核环节、某个状态是什么意思，
    不涉及任何具体单据的当前进度。
4 = 其他：与需求单进度、统计、流程都无关的问题（闲聊、天气、写代码等）；
    **拿不准时也归到 4**（该类会走服务端兜底路由，不会答错）。

注意 1 与 2 的区别：**特指一张单 → 1；问有哪些/多少/整体 → 2**。
"现在有哪些需求单在审核，审核人是谁，等了多久" 属于 2，不属于 1。"""


def node(nid, ntype, title, x, y, data, h=98, w=244):
    d = {"type": ntype, "title": title, "desc": "", "selected": False}
    d.update(data)
    return {"id": nid, "type": "custom", "position": {"x": x, "y": y},
            "positionAbsolute": {"x": x, "y": y}, "width": w, "height": h,
            "sourcePosition": "right", "targetPosition": "left", "data": d}


def edge(eid, src, tgt, st, tt, handle="source"):
    return {"id": eid, "type": "custom", "source": src, "sourceHandle": handle,
            "target": tgt, "targetHandle": "target",
            "data": {"sourceType": st, "targetType": tt,
                     "isInIteration": False, "isInLoop": False}, "zIndex": 0}


def n_start():
    """开始节点：user_name 由门户登录态透传（POC 页面手选/手输）。"""
    return node("start_main", "start", "开始", 60, 300, {"variables": [{
        "variable": "user_name", "label": "当前用户姓名", "type": "text-input",
        "required": True, "max_length": 48, "options": [],
        "placeholder": "请填写当前用户姓名（生产由门户登录态传入）",
    }]}, h=110)


def n_http(nid, title, path, x, y, query_param=True):
    params = ["user: {{#start_main.user_name#}}"]
    if query_param:
        params.insert(0, "query: {{#sys.query#}}")
    return node(nid, "http-request", title, x, y, {
        "method": "get", "url": API_BASE + path,
        "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json",
        "params": "\n".join(params),
        "body": {"type": "none", "data": []},
        "timeout": {"connect": 10, "read": 120, "write": 60},
        "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 1000},
    }, h=118)


def n_llm(x, y, fact_var):
    return node("llm_compose", "llm", "成文", x, y, {
        "model": MODEL,
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYSTEM_PROMPT, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": COMPOSE_USER % fact_var,
             "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": True, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 5, "retry_interval": 2000},
    }, h=110)


def n_answer(nid, title, text, x, y):
    return node(nid, "answer", title, x, y, {"answer": text, "variables": []}, h=90)


def graph_direct():
    return {"nodes": [n_start(),
                      n_http("http_ask", "追踪取数", "/req/ask", 340, 300),
                      n_llm(640, 300, "{{#http_ask.body#}}"),
                      n_answer("answer_main", "回答", "{{#llm_compose.text#}}", 940, 300)],
            "edges": [edge("e1", "start_main", "http_ask", "start", "http-request"),
                      edge("e2", "http_ask", "llm_compose", "http-request", "llm"),
                      edge("e3", "llm_compose", "answer_main", "llm", "answer")],
            "viewport": {"x": 0, "y": 0, "zoom": 0.7}}


CLASSES = [
    {"id": "1", "name": "查询需求单进度", "label": "单/多单进度"},
    {"id": "2", "name": "统计与在办情况", "label": "统计与在办"},
    {"id": "3", "name": "流程与口径", "label": "流程口径"},
    {"id": "4", "name": "其他或不确定", "label": "其他（兜底）"},
]


UNKNOWN_ANSWER = ("这个问题不在需求状态追踪的范围内。我可以帮您：\n"
                  "· 查某张需求单的进度（报需求单号或目录名称）\n"
                  "· 看您相关需求单的整体情况与在办环节\n"
                  "· 说明用数单的流程与状态口径")


def graph_router():
    n_cls = node("cls_intent", "question-classifier", "意图分类", 330, 320, {
        "query_variable_selector": ["sys", "query"],
        "model": MODEL,
        "classes": CLASSES,
        "instruction": ROUTER_INSTRUCTION,
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": False, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        # 分类器靠模型输出 JSON 判定，弱模型偶发不合法输出 → 重试兜底
        "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 1000},
    }, h=150)
    n_agg = node("var_agg", "variable-aggregator", "事实汇总", 900, 300, {
        "output_type": "string",
        "variables": [["http_lookup", "body"], ["http_overview", "body"],
                      ["http_flow", "body"], ["http_fallback", "body"]],
        "advanced_settings": {"group_enabled": False, "groups": []},
    }, h=100)
    return {"nodes": [
        n_start(), n_cls,
        n_http("http_lookup", "查单据", "/req/lookup", 620, 110),
        n_http("http_overview", "查统计", "/req/overview", 620, 300, query_param=False),
        n_http("http_flow", "查流程", "/req/flow", 620, 490, query_param=False),
        # 第 4 类不写死话术：走服务端兜底路由，分类器拿不准时也不会答错
        n_http("http_fallback", "兜底取数", "/req/ask", 620, 690),
        n_agg,
        n_llm(1160, 300, "{{#var_agg.output#}}"),
        n_answer("answer_main", "回答", "{{#llm_compose.text#}}", 1440, 300),
    ], "edges": [
        edge("e1", "start_main", "cls_intent", "start", "question-classifier"),
        edge("e2", "cls_intent", "http_lookup", "question-classifier", "http-request", "1"),
        edge("e3", "cls_intent", "http_overview", "question-classifier", "http-request", "2"),
        edge("e4", "cls_intent", "http_flow", "question-classifier", "http-request", "3"),
        edge("e5", "cls_intent", "http_fallback", "question-classifier", "http-request", "4"),
        edge("e6", "http_lookup", "var_agg", "http-request", "variable-aggregator"),
        edge("e7", "http_overview", "var_agg", "http-request", "variable-aggregator"),
        edge("e8", "http_flow", "var_agg", "http-request", "variable-aggregator"),
        edge("e11", "http_fallback", "var_agg", "http-request", "variable-aggregator"),
        edge("e9", "var_agg", "llm_compose", "variable-aggregator", "llm"),
        edge("e10", "llm_compose", "answer_main", "llm", "answer"),
    ], "viewport": {"x": 0, "y": 0, "zoom": 0.62}}



def features(router):
    qs = ["我的需求单到哪一步了",
          "综管个人积分统计表的进度",
          "现在有哪些需求单在审核，审核人是谁，等了多久",
          "用数单要经过哪些审核环节"]
    opening = ("您好，我是需求状态追踪助手。请先在页面顶部选择本人姓名，"
               "我会在您的权限范围内查询需求单进度、当前审核人与已等待时长。")
    if router:
        opening += "（路由版：意图分类在 workflow 内完成）"
    return {
        "opening_statement": opening,
        "suggested_questions": qs[:3],
        "suggested_questions_after_answer": {"enabled": False},
        "retriever_resource": {"enabled": False},
        "sensitive_word_avoidance": {"enabled": False},
        "file_upload": {"enabled": False, "number_limits": 3, "allowed_file_types": [],
                        "allowed_file_extensions": [], "allowed_file_upload_methods": [],
                        "image": {"enabled": False}},
        "speech_to_text": {"enabled": False},
        "text_to_speech": {"enabled": False, "language": "", "voice": ""},
    }


def find_or_create_app(name, desc):
    apps = req("/console/api/apps?page=1&limit=100") or {"data": []}
    for a in apps.get("data", []):
        if a.get("name") == name:
            print("找到已有应用:", a["id"])
            return a["id"]
    code, obj = raw_req("/console/api/apps", "POST", {
        "name": name, "mode": "advanced-chat", "icon_type": "emoji",
        "icon": "📋", "icon_background": "#FFEAD5", "description": desc})
    if code not in (200, 201) or not obj.get("id"):
        raise SystemExit("创建应用失败: %s %s" % (code, obj))
    print("创建应用:", obj["id"])
    return obj["id"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--router", action="store_true", help="构建问题分类器路由版")
    args = ap.parse_args()

    if not ensure_auth():
        raise SystemExit("Dify 控制台鉴权失败")

    router = args.router
    name = APP_ROUTER if router else APP_DIRECT
    desc = ("需求状态追踪 POC（路由版）：问题分类器分流 → 单单进度 / 统计 / 流程 / 无关，"
            "按 user_name 收窄权限" if router else
            "需求状态追踪 POC：单单进度 / 审核人 / 等待时长 / 统计 / 流程口径，按 user_name 收窄权限")
    app_id = find_or_create_app(name, desc)

    cur = req("/console/api/apps/%s/workflows/draft" % app_id, quiet=True)
    h = cur.get("hash") if isinstance(cur, dict) and cur.get("hash") else None
    payload = {"graph": graph_router() if router else graph_direct(),
               "features": features(router)}
    if h:
        payload["hash"] = h
    r = req("/console/api/apps/%s/workflows/draft" % app_id, "POST", payload)
    print("同步草稿:", "ok" if r else "失败")
    if not r:
        return
    p = req("/console/api/apps/%s/workflows/publish" % app_id, "POST",
            {"marked_name": "v2", "marked_comment":
             "v2：开始节点增加 user_name（权限透传）" +
             ("；意图改为问题分类器路由" if router else "")})
    print("发布:", "ok" if p else "失败")

    code, obj = raw_req("/console/api/apps/%s/api-keys" % app_id, "POST", {})
    if code in (200, 201) and isinstance(obj, dict) and obj.get("token"):
        key = obj["token"]
    else:
        code2, obj2 = raw_req("/console/api/apps/%s/api-keys" % app_id)
        data = (obj2 or {}).get("data", []) if code2 == 200 else []
        keys = [d["token"] for d in data if d.get("token")]
        key = keys[-1] if keys else ""
    id_file = "app_id_router.txt" if router else "app_id.txt"
    key_file = "app_key_router.txt" if router else "app_key.txt"
    with open(os.path.join(HERE, id_file), "w") as f:
        f.write(app_id)
    with open(os.path.join(HERE, key_file), "w") as f:
        f.write(key)
    print("app_id =", app_id, "| api key =", (key[:8] + "...") if key else "获取失败")


if __name__ == "__main__":
    main()
