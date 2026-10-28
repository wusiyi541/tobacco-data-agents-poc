# -*- coding: utf-8 -*-
"""
构建/更新 Dify Chatflow：质检报告生成下沉到沙箱代码节点执行。

链路：
    开始 → [取数] HTTP /dq/report_pack → [报告生成] 代码执行(沙箱内跑 py) 
         → [落盘] HTTP /dq/save_report → [成文] LLM → 回答

设计要点：
  · 「数由代码算」：/dq/report_pack 返回的全部数字由服务端 SQL 算出；
  · 「报告由沙箱拼」：代码节点在 Dify 沙箱容器内运行 report_builder.py，
    用纯标准库生成符合客户模板的 .docx（不依赖 python-docx、不联网）；
  · 「话由模型说」：LLM 只依据沙箱摘要与落盘结果组织对话话术，不碰数字。

沙箱说明：当前用 Dify 自带沙箱容器承载；架构位置对应 V3 的 CubeSandbox，
将来替换只需把代码节点换成「调用 CubeSandbox 的 HTTP 节点」，链路其余部分不动。
"""
import json
import os
import re

from dify_api import ensure_auth, req

APP = "6ac068d9-77ea-4f09-95a3-8265b63b9178"
API_BASE = "http://host.docker.internal:8090"
HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = """你是省烟草公司数据共享中心的「数据质检助手」，服务于数据运营组与各业务处室。

# 铁律（违反即为事故）
1. **所有数字、占比、表名、字段名、人名、规则名，只能来自「沙箱执行结果」或「报告文件落盘结果」。**
   这些结果由服务端确定性计算、沙箱确定性拼装得出，**严禁改动、四舍五入、估算或自行补全**。
2. 结果里没有的，就直说「本次未取到该项数据」，不要推断、不要编造。
3. 不得输出 JSON、字段名、变量名等技术细节；不得输出你的思考过程。
4. 不得输出「默认数据域」「以某域为例」之外的自造抬头；若结果里带 assumed 字段，才在开头一句说明。
5. **不得编造故障**：只有当「报告文件落盘结果」里 `ok` 为 false、或出现了 `error` 字段时，
   才可以描述失败。`ok` 为 true 时一律按成功处理、照抄 url，
   严禁写出「连接异常」「服务不可用」「落盘失败」等结果里根本没有的故障描述。

# 按 intent 选择作答模板

**intent = report（质检报告）** —— 沙箱已按客户《智慧营销数据质量检测报告》模板生成完整报告文件。
按以下顺序输出，**不要重复报告全文**：
  ① 一句话说明已生成报告、覆盖范围与期次；
  ② 「本期关键指标」：用 3-5 条要点列出本期问题数、命中强规则数、常态化运行规则数、规则通过率，
     并给出环比变化（全部取自沙箱摘要 digest，一个数字都不要改）。
     **若摘要里本来就没有环比**（说明该期次是现有数据中最早的一期），
     就明写「本期无可比期次」，不要写「未取到该项数据」，那是两种不同的情况，
     混着说会让用户以为是接口故障；
  ③ 下载链接：从「报告文件落盘结果」取 **url** 字段（**不要用 host_url，那是容器内地址，
     用户浏览器打不开**），单独一行输出
     `下载报告：<url>`（若落盘结果 ok=false，则说明文件生成失败并给出原因）；
     同理，回答里不要出现 host.docker.internal 字样，也不要输出 docx_b64 之类的技术字段。
  ④ 结尾一句说明报告构成：含封面、目录、三章正文（概述 / 质量管理实施 / 问题处理与管理成效）
     与附录A 问题清单，已按客户模板编排。
  ⑤ 若摘要带 caveats 口径说明，用一行小字附在最后。

**intent = rule（规则推荐）** —— 输出规则名、所属质量维度、检核对象、判定逻辑、
  建议阈值（注明「N 条同类规则采用」）、建议强弱等级及理由、参考依据。
  末尾声明：本建议仅供确认，落地需业务系统执行。

**intent = case（案例追溯）** —— 先给该维度归纳出的处置规则（现象 → 处置动作 → 分级 → 责任方 → 支撑案例数），
  再列 2-3 条真实台账工单（工单号 / 表 / 问题描述 / 处置过程），最后给一句可复用的处置建议。

**intent = exec（执行情况）** —— 输出执行记录总数、通过率、各状态分布、平均耗时。

# 其他
- 若 intent 不是 report，「报告文件落盘结果」会返回 ok=false，**请直接忽略，不要提及**。
- 若 intent 识别结果与用户问题明显不符，礼貌说明并给出你理解到的正确范围。
- 回答语言为中文，简洁、结论先行；表格列不超过 5 列。"""

COMPOSE_USER = """用户问题：{{#sys.query#}}

【沙箱执行结果】（代码节点在受限沙箱内运行，已完成报告拼装）
{{#code_report.llm_input#}}

【报告文件落盘结果】
{{#http_save.body#}}

请严格依据以上结果作答，一个数字都不要改动或补充。"""


def node(nid, ntype, title, x, y, data, h=98):
    d = {"type": ntype, "title": title, "desc": "", "selected": False}
    d.update(data)
    return {"id": nid, "type": "custom",
            "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
            "width": 244, "height": h,
            "sourcePosition": "right", "targetPosition": "left", "data": d}


def code_node_source():
    """把 report_builder.py 全文作为代码节点的代码（去掉本地 CLI 入口），
    再补一个 Dify 约定的 main(pack_json) 入口 —— 保持单一事实来源。"""
    src = open(os.path.join(HERE, "report_builder.py"), encoding="utf-8").read()
    i = src.find('if __name__ == "__main__":')
    if i > 0:
        src = src[:i].rstrip()

    # ⚠️ 曾经踩过的坑：report_builder 里若还有一个叫 `main` 的函数，我们随后追加的
    # Dify 入口 `main` 会把它覆盖掉，导致 handle_pack 内部调用递归回到自己，
    # 表现为「节点不报错，但 filename / digest / docx_b64 全是空串」。
    # 这里做一次前置断言，避免以后重命名时又踩回去。
    if "def main(" in src:
        raise SystemExit("report_builder.py 里仍存在 def main( —— 会与 Dify 入口重名，"
                         "请改名为 build_report 之类。")
    # ⚠️ Dify 代码节点会校验「返回的键必须与声明的 outputs 完全一致」，
    # 多返回一个未声明的键就报 `Not all output parameters are validated.`
    # → 这里显式收窄为 5 个声明输出，别图省事直接 return handle_pack(...)
    wrapper = '''

# ---------------- Dify 代码节点入口 ----------------
def main(pack_json):
    """入参为 /dq/report_pack 的返回（JSON 字符串或 dict）。

    ⚠️ 只能返回 outputs 里声明过的键，多一个都会触发
    `Not all output parameters are validated.`
    """
    r = handle_pack(pack_json)
    return {
        "is_report": bool(r.get("is_report")),
        "filename": str(r.get("filename") or ""),
        "docx_b64": str(r.get("docx_b64") or ""),
        "digest": str(r.get("digest") or ""),
        "llm_input": str(r.get("llm_input") or ""),
    }
'''
    return src + "\n" + wrapper


def build_graph():
    n_start = {"id": "start_main", "type": "custom",
               "position": {"x": 60, "y": 300}, "positionAbsolute": {"x": 60, "y": 300},
               "width": 244, "height": 90, "sourcePosition": "right", "targetPosition": "left",
               "data": {"type": "start", "title": "开始", "desc": "",
                        "variables": [], "selected": False}}

    n_pack = node("http_pack", "http-request", "质检取数", 340, 300, {
        "method": "get",
        "url": API_BASE + "/dq/report_pack",
        "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json",
        "params": "query: {{#sys.query#}}",
        "body": {"type": "none", "data": []},
        "timeout": {"connect": 10, "read": 120, "write": 60},
        "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 1000},
    }, h=110)

    n_code = node("code_report", "code", "报告生成(沙箱)", 640, 300, {
        "variables": [{"variable": "pack_json",
                       "value_selector": ["http_pack", "body"]}],
        "code_language": "python3",
        "code": code_node_source(),
        "outputs": {
            "is_report": {"type": "boolean", "children": None},
            "filename": {"type": "string", "children": None},
            "docx_b64": {"type": "string", "children": None},
            "digest": {"type": "string", "children": None},
            "llm_input": {"type": "string", "children": None},
        },
    }, h=120)

    n_save = node("http_save", "http-request", "报告落盘", 940, 300, {
        "method": "post",
        "url": API_BASE + "/dq/save_report",
        "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json",
        "params": "",
        # ⚠️ 踩坑（2026-10-08）：`x-www-form-urlencoded` 形态下，Dify 的
        # `_convert_body_kv` 虽然没有报错，但 `{{#node.var#}}` 实际没被替换
        # → 服务端收到的是空 filename / 空 base64，文件落盘 0 字节。
        # 改用 `json` 形态：value 为一整段 JSON 文本，模板在文本里内联替换；
        # 这里之所以安全，是因为 base64 字符集（A-Za-z0-9+/=）不含引号与反斜杠，
        # 不会有 JSON 转义问题。
        "body": {"type": "json", "data": [
            {"id": "json_body", "key": "", "type": "text",
             "value": json.dumps({"filename": "{{#code_report.filename#}}",
                                  "docx_b64": "{{#code_report.docx_b64#}}"},
                                 ensure_ascii=False)},
        ]},
        "timeout": {"connect": 10, "read": 120, "write": 120},
        "retry_config": {"retry_enabled": False, "max_retries": 1, "retry_interval": 1000},
    }, h=110)

    n_llm = node("llm_compose", "llm", "成文", 1240, 300, {
        "model": {
            "provider": "langgenius/openai_api_compatible/openai_api_compatible",
            "name": "MiniMax-M3", "mode": "chat",
            "completion_params": {"temperature": 0.2, "top_p": 1, "enable_thinking": False},
        },
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYSTEM_PROMPT, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": COMPOSE_USER, "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": True, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 5, "retry_interval": 2000},
    }, h=110)

    n_ans = node("answer_main", "answer", "回答", 1540, 300, {
        "answer": "{{#llm_compose.text#}}", "variables": [],
    }, h=90)

    def edge(eid, src, tgt, st, tt):
        return {"id": eid, "type": "custom", "source": src, "sourceHandle": "source",
                "target": tgt, "targetHandle": "target",
                "data": {"sourceType": st, "targetType": tt,
                         "isInIteration": False, "isInLoop": False}, "zIndex": 0}

    return {
        "nodes": [n_start, n_pack, n_code, n_save, n_llm, n_ans],
        "edges": [
            edge("e1", "start_main", "http_pack", "start", "http-request"),
            edge("e2", "http_pack", "code_report", "http-request", "code"),
            edge("e3", "code_report", "http_save", "code", "http-request"),
            edge("e4", "http_save", "llm_compose", "http-request", "llm"),
            edge("e5", "llm_compose", "answer_main", "llm", "answer"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 0.6},
    }


FEATURES = {
    "opening_statement": "您好，我是数据质检助手。可以帮您看质检报告、分析问题影响与处置建议，"
                         "也能根据你的描述推荐质量规则。",
    "suggested_questions": [
        "给我出一份营销域的数据质量检测报告",
        "营销域目前的数据质量怎么样？",
        "客户信息表的主键有重复，帮我生成一条校验规则",
    ],
    "suggested_questions_after_answer": {"enabled": False},
    "retriever_resource": {"enabled": False},
    "sensitive_word_avoidance": {"enabled": False},
    "file_upload": {"enabled": False, "number_limits": 3, "allowed_file_types": [],
                    "allowed_file_extensions": [], "allowed_file_upload_methods": [],
                    "image": {"enabled": False}},
    "speech_to_text": {"enabled": False},
    "text_to_speech": {"enabled": False, "language": "", "voice": ""},
}


def main():
    if not ensure_auth():
        raise SystemExit("Dify 控制台鉴权失败")
    # 先取当前草稿 hash（Dify 用乐观锁，hash 不符会 409）
    cur = req("/console/api/apps/%s/workflows/draft" % APP, quiet=True)
    h = cur.get("hash") if isinstance(cur, dict) and cur.get("hash") else None

    payload = {"graph": build_graph(), "features": FEATURES}
    if h:
        payload["hash"] = h
    r = req("/console/api/apps/%s/workflows/draft" % APP, "POST", payload)
    print("同步草稿:", json.dumps(r, ensure_ascii=False)[:200] if r else "失败")
    if not r:
        return
    p = req("/console/api/apps/%s/workflows/publish" % APP, "POST",
            {"marked_name": "v2", "marked_comment": "报告生成下沉沙箱代码节点"})
    print("发布:", json.dumps(p, ensure_ascii=False)[:160] if p else "失败")


if __name__ == "__main__":
    main()
