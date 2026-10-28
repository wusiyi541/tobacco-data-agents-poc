# -*- coding: utf-8 -*-
"""
重建 Dify Chatflow：数据质检助手 —— 按意图路由（v3）。

问题（2026-10-20）：原来只要 start → 取数 → 沙箱出报告 → 成文一条链，
服务端 ask() 的默认意图又是 report，于是「问什么都给一份质检报告」。

新链路（**三项能力**：出报告 / 推荐规则 / 情况追问 + 越界兜底）：
    开始
      ├─(开关关闭)──────────────────────────────┐
      ├─(开关打开) 意图分类器(LLM, 预留) ──┐      │
      │                                    ▼      │
      └──────────────────────────────► 取数/路由 (/dq/report_pack)
                                             │ intent
                      ┌──────────┬───────────┼────────────┐
                    report   rule/analysis/compare   其他（含 retired_*）
                      │          │                      │
                 沙箱出报告   成文(取数类)            成文(兜底)
                      │          │                      │
                 成文(报告类) ◄──┴──────────────────────┘
                      │
                变量聚合(成文分流) → 回答

2026-10-20 按用户要求下线两项：「规则执行情况」「历史案例与处置」。
它们的问法仍会被服务端识别（intent=retired_exec / retired_case），但**不再单独作答、
也不单独说明下线**：2026-10-21 按用户要求删掉「成文·已下线」节点，合并进「成文·兜底」，
回答里不出现「本期暂不提供」这类字眼 —— 服务端 payload 里仍带 retired / fallback，
由兜底提示词自行决定要不要用一句「该信息在报告哪一章」接住话头。

要点：
  · **意图路由的主体在服务端**（规则可审计、零 token）；分类器只是「预留开关」，
    默认 if-else 走 has_hint=false 分支，即完全不调模型分类。
    将来要上 dev 模型/分类器，把 if-else 的第一个条件改成 true 即可，其余不动。
  · 只有 report 意图进沙箱出 docx，其余意图一律不出文件 —— 这是本次要修的病根。
  · 落盘节点只给报告链路，非报告意图不再走「落盘返回 skipped」的老路。
"""
import json
import os

from dify_api import ensure_auth, req

APP = "6ac068d9-77ea-4f09-95a3-8265b63b9178"
API_BASE = "http://host.docker.internal:8090"
HERE = os.path.dirname(os.path.abspath(__file__))

MODEL = {
    "provider": "langgenius/openai_api_compatible/openai_api_compatible",
    "name": "MiniMax-M3", "mode": "chat",
    "completion_params": {"temperature": 0.2, "top_p": 1, "enable_thinking": False},
}

# ============================================================ 提示词：报告链路
SYS_REPORT = """你是省烟草公司数据共享中心的「数据质检助手」，服务于数据运营组与各业务处室。

# 铁律（违反即为事故）
1. **所有数字、占比、表名、字段名、规则名、期次，只能来自「沙箱执行结果」或「报告落盘结果」。**
   这些结果由服务端确定性计算、沙箱确定性拼装得出，**严禁改动、四舍五入、估算或自行补全**。
2. 结果里没有的，就直说「本次未取到该项数据」，不要推断、不要编造。
3. 不得输出 JSON、字段名、变量名等技术细节；不得输出你的思考过程。
4. **不得编造故障**：只有落盘结果里 `ok` 为 false、或出现 `error` 字段时才可描述失败。
   `ok` 为 true 时一律按成功处理、照抄 url，严禁写出「连接异常」「服务不可用」等结果里没有的故障。
5. 不得出现 host.docker.internal 字样，也不要输出 docx_b64 之类技术字段。

# 输出结构（这是「已生成报告」的场景，不要重复报告全文）
① 一句话说明已生成报告、覆盖范围与期次；
② 「本期关键指标」：3-5 条要点列出本期问题数、命中强规则数、常态化运行规则数、规则通过率，
   并给出环比变化（全部取自沙箱摘要 digest，一个数字都不要改）。
   若摘要里本来就没有环比（说明该期次是最早一期），明写「本期无可比期次」——
   不要写成「未取到该项数据」，那是两种不同情况。
③ 下载链接：取落盘结果里的 **url** 字段，单独一行 `下载报告：<url>`；
   若 ok=false，说明文件生成失败并给出原因。
④ 结尾一句说明报告构成：含封面、目录、三章正文（概述 / 质量管理实施 / 问题处理与管理成效）
   与附录A 问题清单，已按客户模板编排。
⑤ 若摘要带 caveats 口径说明，用一行小字附在最后。

# 其他
- 回答语言为中文，简洁、结论先行；表格列不超过 5 列。"""

USER_REPORT = """用户问题：{{#sys.query#}}

【沙箱执行结果】（代码节点在受限沙箱内运行，已完成报告拼装）
{{#code_report.llm_input#}}

【报告文件落盘结果】
{{#http_save.body#}}

请严格依据以上结果作答，一个数字都不要改动或补充。"""


# ============================================================ 提示词：取数类
SYS_DATA = """你是省烟草公司数据共享中心的「数据质检助手」，服务于数据运营组与各业务处室。

# 铁律（违反即为事故）
1. **所有数字、占比、表名、字段名、规则名、期次，只能来自「服务端取数结果」。**
   这些数字由服务端 SQL 确定性计算，你**只负责组织语言**，严禁加减、四舍五入、估算或补全。
2. 结果里没有的，直说「本次未取到该项数据」。不得编造接口故障、连接异常等结果里没有的状态。
3. 不得输出 JSON 字段名、变量名；不得输出思考过程；不得出现 host.docker.internal。
4. 结论先行：先一句话回答用户问的那件事，再给支撑数据。

# 按 intent 组织作答

**intent = analysis（质检情况追问）** —— 用户想知道「哪里问题多 / 什么问题最多 / 集中在哪」。
  · 先用一句话给结论（点出排第一的表或问题及数量）；
  · 再从 payload 里挑最贴题的一张表呈现：问「哪些表」用 top_tables，
    问「哪些字段」用 top_issues，问「各域」用 by_domain，问「什么维度」用 by_dimension；
  · 最后一句提示可继续追问（如按域下钻、要看完整报告请说「出一份XX域的报告」）。
  · **不要生成报告、不要说"已生成报告"**，这是问答不是出报告。

**intent = compare（跨期对比）** —— 用一张两列（本期 / 上期）小表给问题数、强规则命中数、通过率，
  再一句话说变化方向。**通过率按 payload 重算，绝不对两期通过率取平均**；
  若 compare_available 为 false，说明只有一期数据、暂无可比期次。

**intent = rule（规则推荐）** —— 输出规则名、所属质量维度、检核对象（表 · 字段）、判定逻辑、
  建议阈值（注明「N 条同类规则采用」，N 取自 payload）、建议强弱等级及理由、参考依据。
  末尾必须声明：本建议仅供确认，落地需质检人员在工具中按流程配置，智能体不直接写入。

# 其他
- 若识别到的范围与用户问题明显不符（payload 里有 assumed 字段），先用一行说明你按什么范围作答。
- 回答语言为中文，表格列不超过 5 列。"""

USER_DATA = """用户问题：{{#sys.query#}}

【服务端取数结果】（意图 {{#http_pack.body#}} 由服务端判定；全部数字已由 SQL 算出）
{{#http_pack.body#}}

请严格依据以上结果作答，一个数字都不要改动或补充；不要生成报告。"""


# ============================================================ 提示词：兜底（含「问的是别的功能」）
# 2026-10-21 按用户要求：原「成文·已下线」节点删除，与兜底合并。
# 两类场景（越界闲聊 / 问的是不在本期范围内的质检功能）走同一节点，
# 由 payload 里有没有 retired 字段自行区分，**不出现「本期暂不提供」这类字眼**。
SYS_FALLBACK = """你是省烟草公司数据共享中心的「数据质检助手」。
用户这句话不在本助手当前能回答的范围内。请**不要生成报告、不要编造任何数字**。

# 先判断是哪一类（看下面的服务端返回）
· 若返回里**有** retired 字段：用户问的是一个具体功能，而这项信息不在问答范围内。
· 若返回里**没有** retired 字段：用户这句话与数据质量检核无关，或没说清想问什么。
两类都走同一套结构，**不要解释为什么不提供，不要编造排期、资源、技术原因**。

# 输出结构（严格两段，不要引言、不要客套）
第一段：一句话把话头接住，再说明你能做什么。
  · 有 retired 时，可用 payload 里的 fallback 原意说明该信息在报告哪一章可以看到
    （用自己的话压缩成一句，不要照抄换行）；
  · 没有 retired 时，用「我是数据质检助手，目前能帮您做这几件事：」起头。
第二段：用编号列出下面 capabilities 里的能力，每条后面用括号给出示例问题
  （照抄名称与示例，不要增删、不要改写）。

# 能力清单（照此输出；服务端返回里的 capabilities 即全部能力）
{{#http_pack.body#}}

# 收尾
最后一句：「您可以直接点下面的示例问题，或用自己的话描述想了解的情况。」

# 铁律
- 不要写「不提供」「不支持」「已下线」「不在服务范围」「尚未开放」这类否定字眼，
  也不要把「规则执行情况」「历史案例」「追溯历史」当作功能名去罗列。
- 只讲**能做什么**，不讲不能做什么；用户问的那件事如果报告里有，就一句话把他引到报告。
- **绝不编造**执行数据、失败规则清单、问题清单、工单号、处置人姓名。
- 不生成报告、不说「已生成报告」、不给下载链接。
- 不出现 JSON 字段名、不出现 host.docker.internal、不输出思考过程。
- 不回答与本助手无关的问题（天气、算术、闲聊等）。"""


USER_FALLBACK = """用户问题：{{#sys.query#}}

【服务端返回】
{{#http_pack.body#}}

请按系统提示的两段式作答，第一段先接住用户的话头再说能力清单。"""


def node(nid, ntype, title, x, y, data, h=98, w=244):
    d = {"type": ntype, "title": title, "desc": "", "selected": False}
    d.update(data)
    return {"id": nid, "type": "custom",
            "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
            "width": w, "height": h,
            "sourcePosition": "right", "targetPosition": "left", "data": d}


def code_node_source():
    """把 report_builder.py 全文作为代码节点代码，再补 Dify 约定入口 main。

    单一事实来源：改报告只有 report_builder.py 一处。
    """
    src = open(os.path.join(HERE, "report_builder.py"), encoding="utf-8").read()
    i = src.find('if __name__ == "__main__":')
    if i > 0:
        src = src[:i].rstrip()
    if "def main(" in src:
        raise SystemExit("report_builder.py 里仍有 def main( —— 会与 Dify 入口重名。")
    wrapper = '''

# ---------------- Dify 代码节点入口 ----------------
def main(pack_json):
    """入参为 /dq/report_pack 的返回。只能返回 outputs 里声明过的键。"""
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


def edge(eid, src, tgt, st, tt, sh="source"):
    return {"id": eid, "type": "custom", "source": src, "sourceHandle": sh,
            "target": tgt, "targetHandle": "target",
            "data": {"sourceType": st, "targetType": tt,
                     "isInIteration": False, "isInLoop": False}, "zIndex": 0}


def build_graph():
    Y = 300
    # start 里放一个「分类器开关」：默认关 → 完全走服务端规则路由（零 extra token、
    # 结果可复现）。改成 on 即切到 LLM 分类器，用来对比两种路由效果。
    n_start = node("start_main", "start", "开始", 40, Y, {
        "variables": [{
            "variable": "use_classifier",
            "label": "启用意图分类器(预留)",
            "type": "select",
            "required": False,
            "default": "off",
            "options": ["off", "on"],
        }],
    }, h=120)

    # ---- 分类器开关（预留）：默认关闭，条件恒 false → 直接进服务端路由
    n_switch = node("if_hint", "if-else", "分类器开关(预留·默认关)",
                    300, Y, {
                        "cases": [{
                            "case_id": "true",
                            "logical_operator": "and",
                            "conditions": [{
                                "variable_selector": ["start_main", "use_classifier"],
                                "comparison_operator": "is",
                                "value": "on",
                            }],
                        }],
                        "logical_operator": "and",
                    }, h=120, w=250)

    n_cls = node("cls_intent", "question-classifier", "意图分类器(预留)", 300, 560, {
        "query_variable_selector": ["sys", "query"],
        "model": MODEL,
        "classes": [
            {"id": "1", "name": "生成质检报告", "label": "出报告"},
            {"id": "2", "name": "规则推荐", "label": "规则推荐"},
            {"id": "3", "name": "质检情况追问", "label": "情况追问"},
            {"id": "4", "name": "其他或不确定", "label": "其他（兜底）"},
        ],
        "instruction": (
            "判断用户这句话属于哪一类，只依据问题本身。\n\n"
            "1 = 生成质检报告：明确要报告/检测报告，或「出一份/生成/导出/下载」报告。\n"
            "2 = 规则推荐：想新配一条检核规则，或问某类问题该配什么规则、阈值怎么定。\n"
            "3 = 质检情况追问：问哪些表/字段/域问题多、分布、占比、数量、跨期对比（**只是问数，不是要报告**）。\n"
            "4 = 其他：闲聊、天气、算术、问执行情况或历史案例，以及拿不准的。"
            "**拿不准一律归 4**（会走服务端兜底，不会答错）。\n\n"
            "关键区分：**问「怎么样/哪些/多少」是 5，要「报告/出报告」才是 1。"
        ),
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": False, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "retry_config": {"retry_enabled": True, "max_retries": 2, "retry_interval": 1000},
    }, h=150, w=250)

    # ---- 取数/路由：唯一入口，带 intent_hint
    n_pack = node("http_pack", "http-request", "取数·意图路由", 620, Y, {
        "method": "get",
        "url": API_BASE + "/dq/report_pack",
        "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json",
        # intent_hint 留空 = 由服务端规则判定意图；接入分类器后改为 {{#cls_intent.class_name#}}
        # 映射值。注意不能直接塞 class_name（中是中文），需在分类器 instruction 里
        # 让模型直接输出英文枚举，或加一个代码节点做映射 —— 见 README 的「切换步骤」。
        "params": "query: {{#sys.query#}}\nintent_hint: ",
        "body": {"type": "none", "data": []},
        "timeout": {"connect": 10, "read": 120, "write": 60},
        "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 1000},
    }, h=130)

    # ---- 按 intent 分流（服务端返回的 intent 是固定枚举，用多分支 if-else 判）
    n_route = node("if_route", "if-else", "按意图分流", 920, Y, {
        "cases": [
            {"case_id": "report", "logical_operator": "and",
             "conditions": [{"variable_selector": ["http_pack", "body"],
                             "comparison_operator": "contains",
                             "value": '"intent": "report"'}]},
            {"case_id": "data", "logical_operator": "and",
             "conditions": [{"variable_selector": ["http_pack", "body"],
                             "comparison_operator": "contains",
                             "value": '"intent": "analysis"'}]},
            {"case_id": "data2", "logical_operator": "or",
             "conditions": [

                 {"variable_selector": ["http_pack", "body"],
                  "comparison_operator": "contains", "value": '"intent": "compare"'},
                 {"variable_selector": ["http_pack", "body"],
                  "comparison_operator": "contains", "value": '"intent": "rule"'},
             ]},
        ],
        "logical_operator": "and",
    }, h=190, w=230)

    # ---- 报告链：沙箱出 docx → 落盘
    n_code = node("code_report", "code", "报告生成(沙箱)", 1220, 100, {
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

    n_save = node("http_save", "http-request", "报告落盘", 1520, 100, {
        "method": "post",
        "url": API_BASE + "/dq/save_report",
        "authorization": {"type": "no-auth", "config": None},
        "headers": "Content-Type:application/json",
        "params": "",
        # 踩坑：x-www-form-urlencoded 下 {{#node.var#}} 不替换 → 必须用 json body；
        # base64 字符集不含引号/反斜杠，内联进 JSON 安全。
        "body": {"type": "json", "data": [
            {"id": "json_body", "key": "", "type": "text",
             "value": json.dumps({"filename": "{{#code_report.filename#}}",
                                  "docx_b64": "{{#code_report.docx_b64#}}"},
                                 ensure_ascii=False)},
        ]},
        "timeout": {"connect": 10, "read": 120, "write": 120},
        "retry_config": {"retry_enabled": False, "max_retries": 1, "retry_interval": 1000},
    }, h=110)

    # ---- 成文分流：报告 / 取数 / 兜底，三个 LLM 各自一组提示词，避免「一句话套所有意图」
    n_llm_r = node("llm_report", "llm", "成文·报告", 1820, 100, {
        "model": MODEL,
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYS_REPORT, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": USER_REPORT, "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": True, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 5, "retry_interval": 2000},
    }, h=110)

    n_llm_d = node("llm_data", "llm", "成文·取数", 1820, 340, {
        "model": MODEL,
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYS_DATA, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": USER_DATA, "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": True, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 5, "retry_interval": 2000},
    }, h=110)

    n_llm_f = node("llm_fallback", "llm", "成文·兜底", 1820, 580, {
        "model": MODEL,
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYS_FALLBACK, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": USER_FALLBACK, "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": {"role_prefix": {"user": "", "assistant": ""},
                   "window": {"enabled": True, "size": 10}},
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 5, "retry_interval": 2000},
    }, h=110)

    n_agg = node("var_agg", "variable-aggregator", "成文分流", 2120, Y, {
        "output_type": "string",
        "variables": [["llm_report", "text"], ["llm_data", "text"],
                      ["llm_fallback", "text"]],
        "advanced_settings": {"group_enabled": False, "groups": []},
    }, h=110)

    n_ans = node("answer_main", "answer", "回答", 2400, Y, {
        "answer": "{{#var_agg.output#}}", "variables": [],
    }, h=90)

    return {
        "nodes": [n_start, n_switch, n_cls, n_pack, n_route, n_code, n_save,
                  n_llm_r, n_llm_d, n_llm_f, n_agg, n_ans],
        "edges": [
            edge("e1", "start_main", "if_hint", "start", "if-else"),
            # 开关关闭（默认）→ 服务端规则路由
            edge("e2", "if_hint", "http_pack", "if-else", "http-request", "false"),
            # 开关打开 → 分类器（预留路径，默认不走；接通方式见 README）
            edge("e3", "if_hint", "cls_intent", "if-else", "question-classifier", "true"),
            # 取数后按 intent 分流（case_id 即 handle）
            edge("e10", "http_pack", "if_route", "http-request", "if-else"),
            edge("e11", "if_route", "code_report", "if-else", "code", "report"),
            edge("e12", "if_route", "llm_data", "if-else", "llm", "data"),
            edge("e13", "if_route", "llm_data", "if-else", "llm", "data2"),
            edge("e14", "if_route", "llm_fallback", "if-else", "llm", "false"),
            edge("e15", "code_report", "http_save", "code", "http-request"),
            edge("e16", "http_save", "llm_report", "http-request", "llm"),
            edge("e17", "llm_report", "var_agg", "llm", "variable-aggregator"),
            edge("e18", "llm_data", "var_agg", "llm", "variable-aggregator"),
            edge("e19", "llm_fallback", "var_agg", "llm", "variable-aggregator"),
            edge("e20", "var_agg", "answer_main", "variable-aggregator", "answer"),
        ],
        "viewport": {"x": 0, "y": 0, "zoom": 0.55},
    }


FEATURES = {
    "opening_statement": "您好，我是数据质检助手。可以帮您：① 出质检报告；"
                         "② 推荐质检规则；③ 回答质检情况（哪里问题多、集中在哪些表/字段）。",
    "suggested_questions": [
        "给我出一份营销域的数据质量检测报告",
        "哪些表质量问题最多？",
        "各数据域问题数量排名",
        "联系电话空值，帮我生成一条校验规则",
        "有效性问题主要出在哪些字段？",
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
    cur = req("/console/api/apps/%s/workflows/draft" % APP, quiet=True)
    h = cur.get("hash") if isinstance(cur, dict) and cur.get("hash") else None
    payload = {"graph": build_graph(), "features": FEATURES}
    if h:
        payload["hash"] = h
    r = req("/console/api/apps/%s/workflows/draft" % APP, "POST", payload)
    print("同步草稿:", json.dumps(r, ensure_ascii=False)[:160] if r else "失败")
    if not r:
        return
    p = req("/console/api/apps/%s/workflows/publish" % APP, "POST",
            {"marked_name": "v3-intent-route",
             "marked_comment": "删掉「成文·已下线」节点，合并进兜底；不再出现「本期暂不提供」"})
    print("发布:", json.dumps(p, ensure_ascii=False)[:160] if p else "失败")


if __name__ == "__main__":
    main()
