# -*- coding: utf-8 -*-
"""
创建 Dify 工作流应用「元数据补充智能体-POC」：
  开始(catalog_code) → HTTP /api/pack（目录+探查） → 代码节点(整理证据)
  → LLM(生成4项建议, 只依据证据) → 代码节点(解析JSON) → 结束
数由代码算（探查统计服务端 SQL 得出），话由模型说（四项建议由模型成文），
最终落库由人工确认 —— 对应红线「AI 不改数」。
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from dify_api import ensure_auth, req, raw_req  # noqa: E402


def req2(path, method="GET", body=None):
    """同 req，但接受 2xx（建应用/建 Key 返回 201）。"""
    code, obj = raw_req(path, method, body)
    if 200 <= code < 300:
        return obj
    print("HTTP %s %s -> %s" % (code, method, path),
          json.dumps(obj, ensure_ascii=False)[:300])
    return None

API_BASE = "http://host.docker.internal:8092"
HERE = os.path.dirname(os.path.abspath(__file__))

SYSTEM_PROMPT = """你是省烟草公司数据共享中心的「元数据补充智能体」。
任务：依据给定证据，为一张库表起草 4 项业务元数据建议，供业务人员确认后填报。

# 铁律
1. 只依据【证据】作答。证据里没有的推断要标注低置信度；严禁编造具体数字、表名、系统名。
2. 四项建议一律用中文，业务简介 50~150 字；业务口径 50~120 字，说明核心指标/字段的统计口径与范围。
3. 更新频率必须从枚举里选一个：["每日更新","每周更新","每月更新","实时更新","不定期更新"]。
   判定优先级：加工任务调度周期（task_info，最硬证据）＞ 时间类字段跨度分布 ＞ 目录简介推断；
   依据任务调度直接判定时给 high，依据时间字段分布给 mid，仅凭推断给 low。
4. 数据来源系统判定优先级：血缘上游权威源系统（lineage）＞ 目录所属机构与业务推断；
   依据血缘判定给 high，仅凭机构/简介推断给 low 或 mid；无法判断时写"待确认"。
   严禁编造血缘与任务里没有的系统名。
5. 每项都给 confidence（high/mid/low）与 basis（一句话依据，引用证据里的具体统计）。
6. 输出必须是单个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码块包裹：
{
  "business_desc": {"value": "...", "confidence": "high|mid|low", "basis": "..."},
  "business_rule": {"value": "...", "confidence": "...", "basis": "..."},
  "update_freq":   {"value": "每日更新", "confidence": "...", "basis": "..."},
  "source_system": {"value": "...", "confidence": "...", "basis": "..."}
}"""

USER_TMPL = """目录编码：{{#start_main.catalog_code#}}

【证据】（服务端对库表做 limit 100 探查的确定性统计结果）
{{#code_evidence.llm_input#}}

请按系统提示的铁律输出 JSON。"""


def node(nid, ntype, title, x, y, data, h=98):
    d = {"type": ntype, "title": title, "desc": "", "selected": False}
    d.update(data)
    return {"id": nid, "type": "custom",
            "position": {"x": x, "y": y}, "positionAbsolute": {"x": x, "y": y},
            "width": 244, "height": h,
            "sourcePosition": "right", "targetPosition": "left", "data": d}


CODE_EVIDENCE = '''
def main(cat_raw, prof_raw, task_raw, lin_raw):
    """汇总四个分节点（①查目录 ②库表探查 ③任务解析 ④血缘接口）为紧凑证据文本。"""
    import json as _json

    def loads(x):
        if isinstance(x, (bytes, bytearray)):
            x = x.decode("utf-8")
        if isinstance(x, str):
            return _json.loads(x)
        return x or {}

    cat = loads(cat_raw)
    if not cat.get("ok"):
        return {"is_ok": False, "llm_input": "", "catalog_name": ""}
    cat = cat["catalog"]
    prof = loads(prof_raw).get("profile") or {}
    task = (loads(task_raw) or {}).get("task_info")
    lin = loads(lin_raw) or {}
    lines = []
    lines.append("目录名称: %s" % cat["catalog_name"])
    lines.append("目录简介(平台已有): %s" % (cat.get("description") or "无"))
    lines.append("所属机构: %s | 共享类型: %s | 被申请次数: %s" %
                 (cat["org"], cat["share_type"], cat.get("applied_count")))
    lines.append("库表名: %s | 总行数: %s | 本次采样: 前 %s 行" %
                 ("t_" + cat["catalog_code"].split("/")[1], prof.get("row_count"),
                  prof.get("sampled")))
    lines.append("字段清单(%d 个): %s" % (len(cat["fields"]), "、".join(cat["fields"])))
    lines.append("字段探查:")
    for f in prof["fields"]:
        seg = "  - %s [%s] 空值率%s%% 去重%s 样例: %s" % (
            f["name"], f["type"], f["null_pct"], f["distinct"], " | ".join(f["samples"][:3]))
        if "min" in f:
            seg += " (min=%s, max=%s)" % (f["min"], f["max"])
        lines.append(seg)
    ti = task
    if ti:
        lines.append("加工任务解析(DataWorks/Dataphin): 平台=%s | 任务=%s | 调度=%s" %
                     (ti.get("platform"), ti.get("task"), ti.get("schedule")))
    ln = lin.get("lineage")
    if ln:
        lines.append("血缘上游(权威源): 来源系统=%s | 上游表=%s" %
                     (ln.get("source_system"), " -> ".join(ln.get("upstream") or [])))
    sv = lin.get("service_api")
    if sv:
        lines.append("共享中心服务接口: %s | 近30天调用 %s 次" % (sv.get("api"), sv.get("calls_30d")))
    return {"is_ok": True,
            "llm_input": "\\n".join(lines),
            "catalog_name": cat["catalog_name"]}
'''

CODE_PARSE = '''
def main(llm_text):
    """从 LLM 输出中稳健提取 JSON：整体 loads → ```json 围栏 → 贪心正则，
    全部失败时按正则逐键抠 value/confidence/basis，绝不抛异常。"""
    import json as _json, re as _re
    t = (llm_text or "").strip()
    cands = []
    m = _re.search(r"```(?:json)?\\s*([\\s\\S]*?)```", t)
    if m:
        cands.append(m.group(1))
    m2 = _re.search(r"\\{[\\s\\S]*\\}", t)
    if m2:
        cands.append(m2.group(0))
    cands.insert(0, t)
    obj = None
    for c in cands:
        try:
            obj = _json.loads(c)
            break
        except Exception:
            continue

    def g(k, d=""):
        v = (obj or {}).get(k)
        if isinstance(v, dict):
            return str(v.get("value") or d)
        if isinstance(v, str):
            return v
        return d

    def c(k):
        v = (obj or {}).get(k)
        if isinstance(v, dict):
            return str(v.get("confidence") or "low")
        return "mid" if isinstance(v, str) else "low"

    def b(k):
        v = (obj or {}).get(k)
        if isinstance(v, dict):
            return str(v.get("basis") or "")
        return ""

    if obj is None:
        # 回退（常见于输出被 max_tokens 截断）：逐键正则抠 value/confidence/basis
        obj = {}
        pat = r'"%s"\\s*:\\s*\\{\\s*"value"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"'
        for k in ("business_desc", "business_rule", "update_freq", "source_system"):
            mv = _re.search(pat % k, t)
            if not mv:
                continue
            mc = _re.search(r'"%s"\\s*:\\s*\\{[^{}]*?"confidence"\\s*:\\s*"(\\w+)"' % k, t)
            mb = _re.search(r'"%s"\\s*:\\s*\\{[^{}]*?"basis"\\s*:\\s*"((?:[^"\\\\]|\\\\.)*)"' % k, t)
            obj[k] = {"value": mv.group(1),
                      "confidence": mc.group(1) if mc else "mid",
                      "basis": mb.group(1) if mb else ""}
    vals = {k: g(k) for k in ("business_desc", "business_rule", "update_freq", "source_system")}
    cons = {k: c(k) for k in ("business_desc", "business_rule", "update_freq", "source_system")}
    bass = {k: b(k) for k in ("business_desc", "business_rule", "update_freq", "source_system")}

    # 受控词表校验（规范性审核的确定性部分，"数由代码算"）：
    # 更新频率必须落在枚举内，越界时模糊归一，仍不行则强制"待确认"并降置信度
    FREQ_ENUM = ["每日更新", "每周更新", "每月更新", "实时更新", "不定期更新"]
    notes = []
    fq = vals["update_freq"]
    if fq and fq not in FREQ_ENUM:
        hit = None
        for pat, target in (("实时", "实时更新"), ("周", "每周更新"), ("月", "每月更新"),
                            ("每日", "每日更新"), ("每天", "每日更新"), ("日", "每日更新"),
                            ("不定期", "不定期更新"), ("临时", "不定期更新")):
            if pat in fq:
                hit = target
                break
        if hit:
            notes.append("更新频率'%s'归一为'%s'" % (fq, hit))
            bass["update_freq"] = (bass["update_freq"] + "｜" if bass["update_freq"] else "") + "受控词表校验归一"
            vals["update_freq"] = hit
        else:
            notes.append("更新频率'%s'不在受控词表，强制待确认" % fq)
            bass["update_freq"] = (bass["update_freq"] + "｜" if bass["update_freq"] else "") + "不在受控词表，需人工判定"
            vals["update_freq"] = "待确认"
            cons["update_freq"] = "low"
    for k in ("business_desc", "business_rule"):
        if not vals[k]:
            notes.append("%s 为空，置信度置 low" % k)
            cons[k] = "low"
        elif len(vals[k]) < 20:
            notes.append("%s 不足20字，置信度降为 mid" % k)
            if cons[k] == "high":
                cons[k] = "mid"
    for k in ("update_freq", "source_system"):
        if not vals[k]:
            vals[k] = "待确认"
            cons[k] = "low"

    for k in ("business_desc", "business_rule", "update_freq", "source_system"):
        if isinstance((obj or {}).get(k), dict):
            obj[k]["value"] = vals[k]
            obj[k]["confidence"] = cons[k]
    return {"business_desc": vals["business_desc"], "business_rule": vals["business_rule"],
            "update_freq": vals["update_freq"], "source_system": vals["source_system"],
            "conf_desc": cons["business_desc"], "conf_rule": cons["business_rule"],
            "conf_freq": cons["update_freq"], "conf_sys": cons["source_system"],
            "basis_desc": bass["business_desc"], "basis_rule": bass["business_rule"],
            "basis_freq": bass["update_freq"], "basis_sys": bass["source_system"],
            "validate_note": "；".join(notes),
            "fill_json": _json.dumps(obj or {}, ensure_ascii=False)}
'''


def build_graph():
    n_start = {"id": "start_main", "type": "custom",
               "position": {"x": 60, "y": 300}, "positionAbsolute": {"x": 60, "y": 300},
               "width": 244, "height": 116, "sourcePosition": "right",
               "targetPosition": "left",
               "data": {"type": "start", "title": "开始", "desc": "", "selected": False,
                        "variables": [{"variable": "catalog_code", "label": "目录编码",
                                       "type": "text-input", "required": True,
                                       "max_length": 48, "options": []}]}}

    def http_node(nid, title, ep, x, y):
        return node(nid, "http-request", title, x, y, {
            "method": "get",
            "url": "%s%s" % (API_BASE, ep),
            "authorization": {"type": "no-auth", "config": None},
            "headers": "Content-Type:application/json",
            "params": "catalog_code: {{#start_main.catalog_code#}}",
            "body": {"type": "none", "data": []},
            "timeout": {"connect": 10, "read": 120, "write": 60},
            "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 1000},
        }, h=110)

    n_cat  = http_node("http_cat",  "①查目录", "/api/catalog_get", 340, 120)
    n_prof = http_node("http_prof", "②库表探查", "/api/profile", 340, 280)
    n_task = http_node("http_task", "③任务解析", "/api/task_info", 340, 440)
    n_lin  = http_node("http_lin",  "④血缘接口", "/api/lineage", 340, 600)

    n_ev = node("code_evidence", "code", "整理证据", 640, 280, {
        "variables": [
            {"variable": "cat_raw", "value_selector": ["http_cat", "body"]},
            {"variable": "prof_raw", "value_selector": ["http_prof", "body"]},
            {"variable": "task_raw", "value_selector": ["http_task", "body"]},
            {"variable": "lin_raw", "value_selector": ["http_lin", "body"]},
        ],
        "code_language": "python3",
        "code": CODE_EVIDENCE,
        "outputs": {"is_ok": {"type": "boolean", "children": None},
                    "llm_input": {"type": "string", "children": None},
                    "catalog_name": {"type": "string", "children": None}},
    }, h=120)

    n_llm = node("llm_fill", "llm", "生成元数据建议", 940, 300, {
        "model": {
            "provider": "langgenius/openai_api_compatible/openai_api_compatible",
            "name": "MiniMax-M3", "mode": "chat",
            "completion_params": {"temperature": 0.2, "top_p": 1,
                                  "enable_thinking": False, "max_tokens": 4096},
        },
        "prompt_template": [
            {"id": "sys", "role": "system", "text": SYSTEM_PROMPT, "edition_type": "basic"},
            {"id": "usr", "role": "user", "text": USER_TMPL, "edition_type": "basic"},
        ],
        "prompt_config": {},
        "context": {"enabled": False, "variable_selector": []},
        "memory": None,
        "vision": {"enabled": False, "configs": {"variable_selector": []}},
        "tools": [],
        "retry_config": {"retry_enabled": True, "max_retries": 3, "retry_interval": 2000},
    }, h=110)

    n_parse = node("code_parse", "code", "解析建议JSON", 1240, 300, {
        "variables": [{"variable": "llm_text", "value_selector": ["llm_fill", "text"]}],
        "code_language": "python3",
        "code": CODE_PARSE,
        "outputs": {k: {"type": "string", "children": None} for k in
                    ("business_desc", "business_rule", "update_freq", "source_system",
                     "conf_desc", "conf_rule", "conf_freq", "conf_sys",
                     "basis_desc", "basis_rule", "basis_freq", "basis_sys",
                     "validate_note", "fill_json")},
    }, h=140)

    n_end = node("end_main", "end", "结束", 1540, 300, {
        "outputs": [
            {"variable": "business_desc", "value_selector": ["code_parse", "business_desc"]},
            {"variable": "business_rule", "value_selector": ["code_parse", "business_rule"]},
            {"variable": "update_freq", "value_selector": ["code_parse", "update_freq"]},
            {"variable": "source_system", "value_selector": ["code_parse", "source_system"]},
            {"variable": "fill_json", "value_selector": ["code_parse", "fill_json"]},
        ]}, h=120)

    def edge(eid, src, tgt, st, tt):
        return {"id": eid, "type": "custom", "source": src, "sourceHandle": "source",
                "target": tgt, "targetHandle": "target",
                "data": {"sourceType": st, "targetType": tt,
                         "isInIteration": False, "isInLoop": False}, "zIndex": 0}

    return {"nodes": [n_start, n_cat, n_prof, n_task, n_lin, n_ev, n_llm, n_parse, n_end],
            "edges": [edge("e1", "start_main", "http_cat", "start", "http-request"),
                      edge("e2", "http_cat", "http_prof", "http-request", "http-request"),
                      edge("e3", "http_prof", "http_task", "http-request", "http-request"),
                      edge("e4", "http_task", "http_lin", "http-request", "http-request"),
                      edge("e5", "http_lin", "code_evidence", "http-request", "code"),
                      edge("e6", "code_evidence", "llm_fill", "code", "llm"),
                      edge("e7", "llm_fill", "code_parse", "llm", "code"),
                      edge("e8", "code_parse", "end_main", "code", "end")],
            "viewport": {"x": 0, "y": 0, "zoom": 0.6}}


def main():
    if not ensure_auth():
        raise SystemExit("Dify 控制台鉴权失败")
    # 1) 建应用（workflow 模式）
    apps = req("/console/api/apps?page=1&limit=100") or {"data": []}
    app = next((a for a in apps.get("data", [])
                if a.get("name") == "元数据补充智能体-POC"), None)
    if app:
        APP = app["id"]
        print("复用已有应用:", APP)
    else:
        r = req2("/console/api/apps", "POST",
                {"name": "元数据补充智能体-POC", "mode": "workflow",
                 "icon_type": "emoji", "icon": "🧩", "icon_background": "#E4FBCC",
                 "description": "目录编码→库表探查→业务元数据四项建议（人工确认后填报）"})
        if not r:
            raise SystemExit("建应用失败")
        APP = r["id"]
        print("新建应用:", APP)

    # 2) 同步草稿
    cur = req("/console/api/apps/%s/workflows/draft" % APP, quiet=True)
    h = cur.get("hash") if isinstance(cur, dict) and cur.get("hash") else None
    payload = {"graph": build_graph(), "features": {"retriever_resource": {"enabled": False}}}
    if h:
        payload["hash"] = h
    r = req("/console/api/apps/%s/workflows/draft" % APP, "POST", payload)
    print("同步草稿:", json.dumps(r, ensure_ascii=False)[:160] if r else "失败")
    if not r:
        raise SystemExit(1)

    # 3) 发布
    p = req("/console/api/apps/%s/workflows/publish" % APP, "POST",
            {"marked_name": "v1", "marked_comment": "POC: 探查+四项元数据建议"})
    print("发布:", "ok" if p is not None else "失败")

    # 4) 取 API Key
    keys = req("/console/api/apps/%s/api-keys" % APP) or {"data": []}
    if keys.get("data"):
        key = keys["data"][0]["token"]
    else:
        k = req2("/console/api/apps/%s/api-keys" % APP, "POST", {})
        key = (k or {}).get("token", "")
    open(os.path.join(HERE, "meta_dify_key.txt"), "w").write(key)
    open(os.path.join(HERE, "meta_app_id.txt"), "w").write(APP)
    print("app_id:", APP)
    print("api_key:", key[:12] + "...（已写入 meta_dify_key.txt）")


if __name__ == "__main__":
    main()
