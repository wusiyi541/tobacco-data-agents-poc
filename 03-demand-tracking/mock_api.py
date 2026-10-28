# -*- coding: utf-8 -*-
"""
需求状态追踪 POC · Mock API 服务
=================================================
零依赖（仅标准库），从 tracking_poc.db 读取，端口 8094。

设计原则 —— 延续「数由代码算，话由模型说」：
  · 意图路由在服务端完成（单单查询 / 状态统计 / 流程说明），
    Dify 侧只有一个 HTTP 节点，不做模型判断分支；
  · 所有数字（等待时长、单数、环节分布）由本服务用 SQL/Python 确定性计算，
    模型只负责把结构化事实组织成话术；
  · 当前审核人与已等待时长**由数据计算**，不靠模型估计（V5 口径）；
  · 只展示状态，不做催办、不预测审批结果（V4 口径）。

状态口径（客户 2026-10-20 提供的四环节分类）：
  1. 用数单申请&审核环节：地市信息中心审核、数源部门审核、主管部门审核
     （清单原始字段为「用数部门审核」，对客口径映射为「地市信息中心审核」）
  2. 用数单实施环节：未签收、已签收、已完成、撤销、已终止
  3. 签收环节：未签收、已签收
  4. 评价环节：未评价、已评价
申请单进度条（真实环境截图口径）：
  发起 → 申请 → 审核 → 实施 → 签收 → 评价 → 办结

权限（POC 2026-10-20 追加）：
  · 所有查询都必须带 user（当前用户姓名），由门户登录态透传；POC 阶段手输/下拉选择；
  · 可见范围 = 本人作为**申请人**的单 ∪ 本人作为**审核人/处理人**的单，其余一律不可见；
  · 越权访问（问到别人的单）返回 denied，且**不回传该单任何字段**（连单号也不回显）；
  · 未带 user 时，除流程口径外一律拒绝（need_user）。

启动：
    python mock_api.py [--port 8094]
接口：
    GET /                      对话页（选人 + 问答，UI）
    GET /api                   接口清单（JSON）
    GET /openapi.json          OpenAPI 3.0（可导入 Dify 自定义工具）
    GET /req/users             可选用户列表（姓名 / 角色 / 可见单数）
    GET /req/me?user=          当前用户的身份与可见范围
    GET /req/ask?query=&user=  统一问答入口（服务端意图路由 → 结构化事实 + llm_input）
    GET /req/lookup?query=&user=  仅「定位单据」（detail/list/denied/unknown），供路由版使用
    GET /req/detail?req_no=&user= 单单详情（进度条 + 环节 + 审核人 + 等待时长 + 流转日志）
    GET /req/search?keyword=&user= 按关键词（单号/目录/申请人/单位）筛单
    GET /req/overview?user=    范围内统计（状态分布、各环节在办、平均等待）
    GET /req/flow              流程与状态口径说明（无需身份）
    POST /chat                 对话代理 → Dify（前端不持有 API Key）
"""
import argparse
import datetime as dt
import json
import os
import re
import sqlite3
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

HERE = os.path.dirname(os.path.abspath(__file__))
DB = os.path.join(HERE, "tracking_poc.db")

COLS = ["需求单号", "需求项编号", "目录编码", "目录名称", "申请人", "申请部门",
        "申请单位", "申请时间", "数源单位", "用数单流转环节", "审批类型",
        "环节创建时间", "审批人或处理人", "需求审核状态", "审批环节开始时间",
        "审批环节结束时间", "需求单最新状态", "实施单编号", "实施单状态",
        "实施单签收时间", "实施完成时间"]

# ---------------------------------------------------------------- 状态口径
STAGES7 = ["发起", "申请", "审核", "实施", "签收", "评价", "办结"]

# 审批类型（原始值）→ 对客审核环节名
AUDIT_CN = {
    "用数部门审核": "地市信息中心审核",
    "数源部门审核": "数源部门审核",
    "主管部门审核": "主管部门审核",
}
# 流转环节（原始值）→ 大环节
LINK_STAGE = {
    "提交订单": "申请",
    "派发用数部门审核": "审核", "用数部门审核": "审核",
    "派发数源部门审核": "审核", "数源部门审核": "审核",
    "派发主管部门审核": "审核", "主管部门审核": "审核",
    "派发共享实施": "实施", "共享实施": "实施",
    "数据签收": "签收",
    "数据评价": "评价",
}
# 需求单最新状态 → (当前大环节, 环节内状态)
STATUS_STAGE = {
    "待用数部门审核": ("审核", "审核中"),
    "待数源部门审核": ("审核", "审核中"),
    "待主管部门审核": ("审核", "审核中"),
    "需求拒绝": ("审核", "未通过"),
    "待实施": ("实施", "待实施"),
    "实施完成(待签收)": ("签收", "待签收"),
    "已签收(待评论)": ("评价", "待评价"),
}
# 签收环节 / 评价环节 状态由实施单数据推导
SIGNS = {"已签收": "已签收", "未签收": "未签收"}
EVALS = {"已评价": "已评价", "未评价": "未评价"}

FLOW_INFO = {
    "进度条": "发起 → 申请 → 审核 → 实施 → 签收 → 评价 → 办结",
    "四环节": [
        {"环节": "用数单申请&审核", "状态": ["地市信息中心审核", "数源部门审核", "主管部门审核"]},
        {"环节": "用数单实施", "状态": ["未签收", "已签收", "已完成", "撤销", "已终止"]},
        {"环节": "签收", "状态": ["未签收", "已签收"]},
        {"环节": "评价", "状态": ["未评价", "已评价"]},
    ],
    "规则": [
        "审核顺序：地市信息中心审核 → 数源部门审核 → 主管部门审核（按单实际流转）",
        "当前审核人与已等待时长由流程数据计算，不由模型估计",
        "本助手只展示状态与轨迹，不做催办、不预测审批结果；落库与执行由业务系统完成",
        "需求被拒绝即流程终止（进度停于审核环节）；拒绝原因见流转日志",
    ],
}


# ---------------------------------------------------------------- 基础
def q(sql, args=()):
    conn = sqlite3.connect(DB)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def all_rows():
    return [dict(zip(COLS, r)) for r in
            q("SELECT * FROM flow_rows ORDER BY c7, c11")]


def pt(s):
    """'20261012 12:44:25' → datetime；空/异常返回 None。"""
    s = (s or "").strip()
    if not s:
        return None
    for f in ("%Y%m%d %H:%M:%S", "%Y-%m-%d %H:%M:%S"):
        try:
            return dt.datetime.strptime(s, f)
        except ValueError:
            pass
    return None


def fmt_t(s):
    d = pt(s)
    return d.strftime("%Y-%m-%d %H:%M:%S") if d else (s or "")


def dur_cn(seconds):
    if seconds is None or seconds < 0:
        return ""
    m = int(seconds // 60)
    if m < 60:
        return "%d 分钟" % max(m, 1)
    h = m // 60
    if h < 48:
        return "%d 小时 %d 分" % (h, m % 60)
    return "%d 天 %d 小时" % (h // 24, h % 24)


def wait_since(t_str, now):
    d = pt(t_str)
    return dur_cn((now - d).total_seconds()) if d else ""


# ---------------------------------------------------------------- 单据视图
def order_view(rows, now):
    """把一张单的全部行 → 结构化视图。rows 需已按时间排序。"""
    o = rows[0]
    req_no = o["需求单号"]
    # 单级状态：取**最后一条非空**的需求单最新状态（首行是「提交订单」行，状态为空）；
    # 全空（如 20250408102510019211）则按流转轨迹/实施单推导，并如实标注。
    status = ""
    for r in rows:
        if r["需求单最新状态"]:
            status = r["需求单最新状态"]
    status_inferred = False
    if not status:
        impl_states = {r["实施单状态"] for r in rows if r["实施单状态"]}
        if "已终止" in impl_states:
            status = "实施单已终止"
        elif impl_states:
            status = "实施中"
        else:
            status = "流转中"
        status_inferred = True
    # 单级状态 → 大环节
    if status_inferred:
        # 推导状态：**流转轨迹优先**（如实施单已终止但后续有签收/评价事件）
        last_link = next((r["用数单流转环节"] for r in reversed(rows)
                          if r["用数单流转环节"]), "")
        ls = LINK_STAGE.get(last_link)
        if ls:
            stage, sub = ls, {"签收": "已签收", "评价": "已评价"}.get(ls, "进行中")
        else:
            stage, sub = "申请", status
    else:
        stage, sub = STATUS_STAGE.get(status, ("申请", status or "进行中"))

    # ---- 目录（需求项）维度：逐行覆盖，取该需求项**最后**的流转状态
    items, impls = {}, {}
    for r in rows:
        item_id = r["需求项编号"]
        if item_id:
            it = items.setdefault(item_id, {"需求项编号": item_id})
            it["目录编码"] = r["目录编码"] or it.get("目录编码", "")
            it["目录名称"] = r["目录名称"] or it.get("目录名称", "")
            it["流转环节"] = r["用数单流转环节"] or it.get("流转环节", "")
            it["当前环节"] = LINK_STAGE.get(r["用数单流转环节"], it.get("当前环节", ""))
            if r["审批类型"]:
                it["审核环节"] = AUDIT_CN.get(r["审批类型"], r["审批类型"])
            elif "审核环节" not in it:
                it["审核环节"] = AUDIT_CN.get(
                    str(r["用数单流转环节"]).replace("派发", ""), "") \
                    if "审核" in str(r["用数单流转环节"]) else ""
            if r["需求审核状态"]:
                it["审核状态"] = r["需求审核状态"]
            elif "审核状态" not in it:
                it["审核状态"] = ""
        imp = r["实施单编号"]
        if imp and imp not in impls:
            impls[imp] = {"实施单编号": imp, "实施单状态": r["实施单状态"] or "进行中",
                          "签收状态": "已签收" if r["实施单签收时间"] else "未签收",
                          "签收时间": fmt_t(r["实施单签收时间"]),
                          "完成时间": fmt_t(r["实施完成时间"])}

    # ---- 当前审核人 / 等待时长（审核中才有意义）
    auditor, wait, wait_base, pend_link = "", "", "", ""
    if stage == "审核" and sub == "审核中":
        pend = [r for r in rows
                if LINK_STAGE.get(r["用数单流转环节"]) == "审核"
                and not r["审批环节结束时间"]
                and not r["需求审核状态"]]
        if pend:
            cur = pend[-1]
            auditor = cur["审批人或处理人"]
            pend_link = AUDIT_CN.get(cur["审批类型"]) or \
                AUDIT_CN.get(str(cur["用数单流转环节"]).replace("派发", ""), "")
            wait_base = cur["审批环节开始时间"] or cur["环节创建时间"]
            wait = wait_since(wait_base, now)
    else:
        last_ev = max((pt(r["环节创建时间"]) for r in rows if r["环节创建时间"]), default=None)
        if last_ev:
            wait_base = last_ev.strftime("%Y%m%d %H:%M:%S")
            wait = "最近流转 " + wait_since(wait_base, now) + "前"

    # ---- 七阶段进度条
    idx = STAGES7.index(stage) if stage in STAGES7 else 1
    if sub in ("未通过", "已终止"):   # 拒绝/终止：流程停于当前环节
        progress = [{"stage": s, "state": "done" if i < idx else
                     ("failed" if i == idx else "todo")}
                    for i, s in enumerate(STAGES7)]
    else:
        progress = [{"stage": s,
                     "state": "done" if i < idx else ("active" if i == idx else "todo")}
                    for i, s in enumerate(STAGES7)]
        # 全部走完 → 办结
        if status in ("已评价", "已办结", "已完成"):
            progress = [{"stage": s, "state": "done"} for s in STAGES7]

    # ---- 流转日志（去重、按时间）
    log, seen = [], set()
    for r in rows:
        key = (r["环节创建时间"], r["用数单流转环节"], r["审批人或处理人"])
        if key in seen or not r["环节创建时间"]:
            continue
        seen.add(key)
        log.append({"时间": fmt_t(r["环节创建时间"]),
                    "环节": r["用数单流转环节"],
                    "处理人": r["审批人或处理人"],
                    "审核结论": r["需求审核状态"]})

    # ---- 签收 / 评价汇总（以**单级状态**为准；实施单签收时间与单级状态
    #      存在口径矛盾（如 20261012124425015264），记入 POC 待确认）
    if status == "已签收(待评论)":
        sign = "已签收"
    elif impls and status in ("实施完成(待签收)", "实施单已终止", "实施中"):
        sign = "未签收" if status == "实施完成(待签收)" else ""
    elif not impls:
        sign = ""
    else:
        sign = "已签收" if any(v["签收状态"] == "已签收" for v in impls.values()) else "未签收"
    evaluated = any(r["用数单流转环节"] == "数据评价" for r in rows)
    evalu = ("已评价" if evaluated else
             ("待评价" if status == "已签收(待评论)" else ""))

    return {
        "需求单号": req_no,
        "申请人": o["申请人"], "申请部门": o["申请部门"],
        "申请单位": o["申请单位"], "申请时间": fmt_t(o["申请时间"]),
        "数源单位": o["数源单位"],
        "最新状态": status or "进行中",
        "状态为推导值": status_inferred,
        "进度条": progress,
        "当前大环节": stage, "环节内状态": sub,
        "当前审核人": auditor, "待审环节": pend_link, "已等待时长": wait,
        "签收状态": sign, "评价状态": evalu,
        "需求项": list(items.values()),
        "实施单": list(impls.values()),
        "流转日志": log,
    }


def orders_index(now):
    """按单聚合 + 索引（供检索与统计）。"""
    rows = all_rows()
    by, order_seq = {}, []
    for r in rows:
        by.setdefault(r["需求单号"], []).append(r)
    idx = {}
    for no, rs in by.items():
        v = order_view(rs, now)
        v["行数"] = len(rs)
        idx[no] = v
        order_seq.append(no)
    return idx, order_seq


# ---------------------------------------------------------------- 权限（身份与可见范围）
ROLE_APPLICANT = "申请人"
ROLE_HANDLER = "审核/处理人"
# 非自然人账号：不作为可登录身份出现在用户列表
NON_PERSON = {"系统自动处理", "省烟草数据共享中心管理员"}


def people_of(v):
    """一张单涉及的自然人 → {姓名: {角色}}。"""
    p = {}
    if v["申请人"]:
        p.setdefault(v["申请人"], set()).add(ROLE_APPLICANT)
    for lg in v["流转日志"]:
        n = (lg.get("处理人") or "").strip()
        if n and n not in NON_PERSON:
            p.setdefault(n, set()).add(ROLE_HANDLER)
    return p


def related(v, user):
    """当前用户与该单的关系：None=无关，否则返回角色列表（申请人优先）。"""
    if not user:
        return None
    roles = people_of(v).get(user)
    if not roles:
        return None
    return [r for r in (ROLE_APPLICANT, ROLE_HANDLER) if r in roles]


def scoped_index(now, user):
    """按权限过滤后的单据索引（无 user → 空）。"""
    idx, seq = orders_index(now)
    if not user:
        return {}, []
    keep = {no: v for no, v in idx.items() if related(v, user)}
    return keep, [no for no in seq if no in keep]


def users(now):
    """可选用户清单（POC：门户侧本应来自登录态/组织架构）。"""
    idx, seq = orders_index(now)
    agg = {}
    for no in seq:
        v = idx[no]
        for name, roles in people_of(v).items():
            a = agg.setdefault(name, {"姓名": name, "角色": [], "可见单数": 0,
                                     "需求单": []})
            for r in (ROLE_APPLICANT, ROLE_HANDLER):
                if r in roles and r not in a["角色"]:
                    a["角色"].append(r)
            a["可见单数"] += 1
            a["需求单"].append(no)
    lst = sorted(agg.values(), key=lambda x: (-x["可见单数"], x["姓名"]))
    return {"人数": len(lst), "用户": lst}


def me(now, user):
    idx, seq = scoped_index(now, user)
    roles, role_order = set(), (ROLE_APPLICANT, ROLE_HANDLER)
    for no in seq:
        roles.update(related(idx[no], user) or [])
    return {"姓名": user,
            "角色": [r for r in role_order if r in roles],
            "可见单数": len(seq),
            "可见需求单": [{"需求单号": no, "目录": "、".join(
                dict.fromkeys(i["目录名称"] for i in idx[no]["需求项"])) or "—",
                "最新状态": idx[no]["最新状态"],
                "我的角色": "、".join(related(idx[no], user) or [])} for no in seq],
            "权限说明": "仅可查询本人作为申请人的需求单，以及本人参与审核/处理的需求单"}


# ---------------------------------------------------------------- 意图路由
# 在办/待办视角先于流程判断（「审核人是谁、等了多久」要的是在办清单，不是流程说明）
KW_PENDING = re.compile(r"在审核|审核中|待审核|在办|等待|等了多久|多久了|审核人是谁|谁来审了|哪些需求单|需要催|催办")
KW_OVERVIEW = re.compile(r"统计|多少|几单|概况|总体|整体|分布|汇总|所有需求单|全部需求单|列表|情况")
# 「我的」视角：不指定单号时列出本人范围内的单
KW_MINE = re.compile(r"我的|我申请|我提交|我提的|我相关|和我相关|跟我相关|本人|我这边|我报的")
# 流程/口径类问题（「哪些审核环节」属流程而非统计）
KW_FLOW = re.compile(r"流程|环节|流转|需要多久|多长时间|谁来审$|什么意思|什么规则|"
                     r"口径|说明|状态有哪|签收是什么|评价是什么|办结是什么|什么是|怎么走")


def match_orders(query, idx, seq):
    """在给定索引内做关键词匹配（目录名 / 申请人 / 单位 / 状态 / 实施单号，双向子串）。"""
    hits = []
    for no in seq:
        v = idx[no]
        names = {v["申请人"], v["申请单位"], v["申请部门"], v["最新状态"]}
        names |= {it["目录名称"] for it in v["需求项"] if it["目录名称"]}
        names |= {it["目录编码"] for it in v["需求项"] if it["目录编码"]}
        names |= {im["实施单编号"] for im in v["实施单"]}
        for n in names:
            if n and len(n) >= 2 and (n in query or
                                      (len(query) >= 2 and query in n)):
                hits.append(no)
                break
    return list(dict.fromkeys(hits))


def route(query, now, user=None, lookup_only=False):
    """意图路由。数据范围按 user 收窄；越权单返回 denied（不含任何单据字段）。

    lookup_only=True 时只做「定位单据」（detail/list/denied/unknown），
    不发散到统计与流程——供 Dify 路由版分类器分流后调用。
    """
    idx_all, seq_all = orders_index(now)
    idx, seq = scoped_index(now, user)
    query = (query or "").strip()

    # 0) 无身份：除流程口径外一律拒绝
    if not user:
        if lookup_only:
            return "need_user", None, None
        if KW_FLOW.search(query):
            return "flow", FLOW_INFO, None
        return "need_user", None, None

    # 1) 显式单号
    m = re.search(r"\d{17,20}", query)
    if m:
        no = m.group(0)
        if no in idx:
            return "detail", idx[no], None
        if no in idx_all:
            return "denied", {"需求单号": no}, None

    # 2) 关键词命中单据（仅在本人可见范围内匹配）
    hits = match_orders(query, idx, seq)
    if len(hits) == 1:
        return "detail", idx[hits[0]], None
    if len(hits) > 1:
        return "list", [idx[h] for h in hits], None
    # 本人范围内没命中、但全库有 → 越权（不回传命中内容）
    if match_orders(query, idx_all, seq_all):
        return "denied", {}, None

    # 2.5) 「我的需求单」视角：列出本人范围内的单
    if KW_MINE.search(query):
        if not seq:
            return "empty", None, None
        if KW_OVERVIEW.search(query):
            return "overview", overview(now, user), None
        return "list", [idx[no] for no in seq], None

    if lookup_only:
        # 兜底：分类器若把「在办/统计」类问题误分到本分支，仍按统计作答，
        # 不把用户的合理问题答成「请提供单号」。
        if KW_PENDING.search(query) or KW_OVERVIEW.search(query):
            return "overview", overview(now, user), None
        return "unknown", None, None

    # 3) 在办视角 → 流程 → 统计
    if KW_PENDING.search(query):
        return "overview", overview(now, user), None
    if KW_FLOW.search(query):
        return "flow", FLOW_INFO, None
    if KW_OVERVIEW.search(query):
        return "overview", overview(now, user), None
    return "unknown", None, None



def brief(v):
    """列表用的单据摘要。"""
    return {"需求单号": v["需求单号"], "申请人": v["申请人"], "申请单位": v["申请单位"],
            "目录": "、".join(dict.fromkeys(i["目录名称"] for i in v["需求项"])) or "—",
            "最新状态": v["最新状态"], "当前大环节": v["当前大环节"],
            "环节内状态": v["环节内状态"],
            "当前审核人": v["当前审核人"], "已等待时长": v["已等待时长"]}


def overview(now, user=None):
    idx, seq = scoped_index(now, user)
    dist = {}
    for v in idx.values():
        dist[v["最新状态"]] = dist.get(v["最新状态"], 0) + 1
    stage_dist = {}
    for v in idx.values():
        k = "%s·%s" % (v["当前大环节"], v["环节内状态"])
        stage_dist[k] = stage_dist.get(k, 0) + 1
    audits = []
    for no in seq:
        v = idx[no]
        if v["当前大环节"] == "审核" and v["环节内状态"] == "审核中":
            audits.append({"需求单号": v["需求单号"],
                           "审核环节": v.get("待审环节") or
                           next((i["审核环节"] for i in v["需求项"] if i["审核环节"]), ""),
                           "当前审核人": v["当前审核人"],
                           "已等待时长": v["已等待时长"]})
    roles = set()
    for no in seq:
        roles.update(related(idx[no], user) or [])
    return {"需求单总数": len(idx), "状态分布": dist, "环节分布": stage_dist,
            "审核中在办": audits, "数据截至": DATA_UNTIL,
            "查询范围": {"用户": user or "",
                         "角色": [r for r in (ROLE_APPLICANT, ROLE_HANDLER) if r in roles],
                         "说明": "仅统计与当前用户相关的需求单（本人申请 + 本人参与审核/处理），"
                                 "不代表全省全量"}}


# ---------------------------------------------------------------- llm_input
def llm_of(intent, data, query, user=None, scope_n=None):
    L = ["intent = %s" % intent]
    if user:
        L.append("当前用户 = %s（可见范围：本人申请 + 本人参与审核/处理，共 %s 张单）"
                 % (user, scope_n if scope_n is not None else "?"))
    if intent == "detail":
        d = data
        L.append("进度条: " + " ".join(
            {"done": "[已完成]", "active": "[当前]", "failed": "[未通过]",
             "todo": "[未到]"}.get(p["state"], "") + p["stage"]
            for p in d["进度条"]))
        L.append("单据事实(JSON): %s" % json.dumps(d, ensure_ascii=False))
    elif intent == "list":
        L.append("命中 %d 张单:" % len(data))
        for b in map(brief, data):
            L.append("- %s" % json.dumps(b, ensure_ascii=False))
        L.append("可提示用户报出具体需求单号或目录名查看详情。")
    elif intent == "overview":
        L.append("统计事实(JSON，范围仅限当前用户相关单据): %s"
                 % json.dumps(data, ensure_ascii=False))
    elif intent == "flow":
        L.append("流程口径(JSON): %s" % json.dumps(data, ensure_ascii=False))
    elif intent == "empty":
        L.append("当前用户「%s」在系统中没有任何需求单（既非申请人，也非审核/处理人）。"
                 % (user or ""))
        L.append("应回复：您名下暂无需求单；如有疑问请联系数据共享中心确认账号是否正确。")
    elif intent == "denied":
        L.append("权限判定: 用户「%s」询问的单据不在其可见范围内。"
                 "**不得透露该单是否存在、单号、申请人、目录、状态或任何字段**，"
                 "也不得猜测评结果。" % (user or "未登录"))
        L.append("应回复：该需求单不在您的查询范围（仅可查询本人申请或本人参与审核/处理的需求单），"
                 "如需查询请联系数据共享中心或确认登录账号。")
        if not user:
            L.append("当前未提供用户身份，需先提示用户在页面顶部选择或输入姓名。")
    elif intent == "need_user":
        L.append("未提供用户身份，不能查询任何需求单。"
                 "应提示用户：请先在页面顶部选择或输入本人姓名，再进行查询。"
                 "（流程与状态口径类问题无需身份，可直接回答）")
    else:
        L.append("未能识别需求单或意图。请引导用户：报需求单号 / 目录名称查进度，"
                 "或询问「我相关的需求单整体情况」「用数单有哪些环节」。")
    return "\n".join(L)


def ask(query, now, user=None):
    intent, data, _ = route(query, now, user)
    _, seq = scoped_index(now, user) if user else ({}, [])
    return {"ok": True, "intent": intent, "user": user or "", "data": data,
            "data_until": DATA_UNTIL,
            "llm_input": llm_of(intent, data, query, user, len(seq))}


# ---------------------------------------------------------------- 数据截至
_rows0 = all_rows()
_ts = [pt(r["环节创建时间"]) for r in _rows0]
_ts += [pt(r["实施单签收时间"]) for r in _rows0]
_ts += [pt(r["实施完成时间"]) for r in _rows0]
_ts = [t for t in _ts if t]
DATA_UNTIL = max(_ts).strftime("%Y-%m-%d %H:%M:%S") if _ts else ""


# ---------------------------------------------------------------- HTTP
S = {"type": "string"}


# ---------------------------------------------------------------- OpenAPI 构造
# 手写深层嵌套字典容易漏括号，这里用辅助函数拼装，结构自证。
def _resp(schema):
    return {"200": {"description": "成功",
                    "content": {"application/json": {"schema": schema}}}}


def _obj(props, desc=None):
    d = {"type": "object", "properties": props}
    if desc:
        d["description"] = desc
    return d


def _arr(item):
    return {"type": "array", "items": item}


def _ref(name):
    return {"$ref": "#/components/schemas/" + name}


def _param(name, desc, example=None, required=True):
    p = {"name": name, "in": "query", "required": required,
         "description": desc, "schema": S}
    if example:
        p["example"] = example
    return p


def _ep(tag, op, summary, desc, params=None, resp=None):
    d = {"tags": [tag], "operationId": op, "summary": summary, "description": desc}
    if params:
        d["parameters"] = params
    d["responses"] = resp or {"200": {"description": "成功"}}
    return d


SCHEMAS = {
    "ProgressStep": _obj({
        "stage": {"type": "string", "enum": STAGES7, "example": "审核"},
        "state": {"type": "string", "enum": ["done", "active", "todo", "failed"],
                  "description": "已完成 / 当前 / 未到 / 未通过或终止", "example": "active"},
    }, "七阶段进度条中的一步"),
    "Item": _obj({
        "需求项编号": S,
        "目录编码": {"type": "string", "example": "POC-DEMO/006"},
        "目录名称": {"type": "string", "example": "综管个人积分统计表"},
        "流转环节": {"type": "string", "description": "该需求项最后一条流转环节原文",
                     "example": "数据评价"},
        "当前环节": {"type": "string", "enum": ["申请", "审核", "实施", "签收", "评价"],
                     "description": "映射后的大环节"},
        "审核环节": {"type": "string", "enum": ["地市信息中心审核", "数源部门审核",
                                             "主管部门审核", ""]},
        "审核状态": {"type": "string", "enum": ["通过", "不通过", ""]},
    }, "需求项（一张单可含多条目录）"),
    "Impl": _obj({
        "实施单编号": {"type": "string", "example": "20261019100504019350-T001"},
        "实施单状态": {"type": "string", "enum": ["未签收", "已签收", "已完成", "撤销",
                                                 "已终止", "进行中"]},
        "签收状态": {"type": "string", "enum": ["未签收", "已签收"]},
        "签收时间": {"type": "string", "description": "YYYY-MM-DD HH:MM:SS，未签收为空"},
        "完成时间": S,
    }, "实施单"),
    "LogEntry": _obj({
        "时间": {"type": "string", "example": "2026-10-19 16:42:39"},
        "环节": {"type": "string", "example": "主管部门审核"},
        "处理人": {"type": "string", "example": "演示审核员乙"},
        "审核结论": {"type": "string", "enum": ["通过", "不通过", ""]},
    }, "流转日志条目（按时间升序、已去重）"),
    "OrderDetail": _obj({
        "需求单号": {"type": "string", "example": "20261019100504019350"},
        "申请人": S, "申请部门": S,
        "申请单位": {"type": "string", "example": "潮州市烟草专卖局（公司）"},
        "申请时间": S, "数源单位": S,
        "最新状态": {"type": "string", "example": "已签收(待评论)",
                     "description": "需求单最新状态原文"},
        "状态为推导值": {"type": "boolean",
                         "description": "true 表示状态字段缺失、按流转轨迹推导"},
        "进度条": _arr(_ref("ProgressStep")),
        "当前大环节": {"type": "string", "enum": ["申请", "审核", "实施", "签收", "评价"]},
        "环节内状态": {"type": "string", "example": "待评价",
                       "description": "审核中/未通过/待实施/待签收/待评价/已评价等"},
        "当前审核人": {"type": "string", "description": "仅审核中非空"},
        "待审环节": {"type": "string", "enum": ["地市信息中心审核", "数源部门审核",
                                             "主管部门审核", ""],
                     "description": "仅审核中非空"},
        "已等待时长": {"type": "string", "example": "6 小时 11 分",
                       "description": "审核中=当前环节已等待；其他=「最近流转 X 前」"},
        "签收状态": {"type": "string", "enum": ["未签收", "已签收", ""]},
        "评价状态": {"type": "string", "enum": ["未评价", "已评价", "待评价", ""]},
        "需求项": _arr(_ref("Item")),
        "实施单": _arr(_ref("Impl")),
        "流转日志": _arr(_ref("LogEntry")),
    }, "需求单详情"),
    "OrderBrief": _obj({
        "需求单号": S, "申请人": S, "申请单位": S,
        "目录": {"type": "string", "description": "该单全部目录名，顿号分隔"},
        "最新状态": S, "当前大环节": S, "环节内状态": S,
        "当前审核人": S, "已等待时长": S,
    }, "单据摘要（列表/检索用）"),
    "Overview": _obj({
        "需求单总数": {"type": "integer", "example": 3,
                       "description": "当前用户可见范围内的需求单数（非全省全量）"},
        "状态分布": {"type": "object", "additionalProperties": {"type": "integer"},
                     "example": {"待主管部门审核": 1, "实施完成(待签收)": 1}},
        "环节分布": {"type": "object", "additionalProperties": {"type": "integer"},
                     "example": {"审核·审核中": 1, "签收·待签收": 1}},
        "审核中在办": _arr(_obj({
            "需求单号": S, "审核环节": S, "当前审核人": S, "已等待时长": S})),
        "数据截至": S,
        "查询范围": _ref("Scope"),
    }, "整体统计（按当前用户收窄）"),
    "Scope": _obj({
        "用户": S,
        "角色": {"type": "array", "items": {"type": "string",
                                          "enum": [ROLE_APPLICANT, ROLE_HANDLER]}},
        "说明": {"type": "string",
                 "example": "仅统计与当前用户相关的需求单（本人申请 + 本人参与审核/处理），不代表全省全量"},
    }, "权限范围说明"),
    "UserItem": _obj({
        "姓名": S,
        "角色": {"type": "array", "items": {"type": "string",
                                          "enum": [ROLE_APPLICANT, ROLE_HANDLER]}},
        "可见单数": {"type": "integer", "example": 3},
        "需求单": _arr(S),
    }, "可选用户（POC 用于下拉选择，生产应换成登录态）"),
    "Me": _obj({
        "姓名": S,
        "角色": {"type": "array", "items": S},
        "可见单数": {"type": "integer"},
        "可见需求单": _arr(_obj({"需求单号": S, "目录": S, "最新状态": S, "我的角色": S})),
        "权限说明": S,
    }, "当前用户身份与可见范围"),
    "FlowInfo": _obj({
        "进度条": {"type": "string",
                   "example": "发起 → 申请 → 审核 → 实施 → 签收 → 评价 → 办结"},
        "四环节": _arr(_obj({"环节": S, "状态": _arr(S)})),
        "规则": _arr(S),
    }, "流程与状态口径"),
}

ASK_RESP = _obj({
    "ok": {"type": "boolean", "example": True},
    "intent": {"type": "string",
               "enum": ["detail", "list", "overview", "flow", "unknown",
                        "denied", "need_user", "empty"],
               "description": "意图：单单详情/多单列表/整体统计/流程口径/未识别/"
                              "越权（denied）/缺身份（need_user）/名下无单（empty）"},
    "user": {"type": "string", "description": "当前用户姓名", "example": "张枫"},
    "data": {"description": "随 intent 变化：detail→OrderDetail；list→OrderBrief[]；"
                            "overview→Overview；flow→FlowInfo；denied/need_user→null",
             "oneOf": [_ref("OrderDetail"), _arr(_ref("OrderBrief")),
                       _ref("Overview"), _ref("FlowInfo")]},
    "data_until": {"type": "string", "example": "2026-10-20 09:44:43"},
    "llm_input": {"type": "string", "description": "给大模型的事实文本（含 intent 行与权限提示）"},
})

DETAIL_RESP = _obj({"ok": {"type": "boolean"}, "data": _ref("OrderDetail")})
SEARCH_RESP = _obj({"ok": {"type": "boolean"},
                    "命中": {"type": "integer", "example": 1},
                    "单据": _arr(_ref("OrderBrief"))})
OVERVIEW_RESP = _obj({"ok": {"type": "boolean"}, "data": _ref("Overview")})
FLOW_RESP = _obj({"ok": {"type": "boolean"}, "data": _ref("FlowInfo")})
USERS_RESP = _obj({"ok": {"type": "boolean"}, "人数": {"type": "integer"},
                   "用户": _arr(_ref("UserItem"))})
ME_RESP = _obj({"ok": {"type": "boolean"}, "data": _ref("Me")})


OPENAPI = {
    "openapi": "3.0.3",
    "info": {
        "title": "需求状态追踪智能体 API",
        "version": "1.0.0",
        "description": "需求单进度查询服务：意图路由、七阶段进度、当前审核人与已等待时长、"
                       "整体统计、流程口径。只读服务，不写业务库、不做催办。",
    },
    "servers": [
        {"url": "http://host.docker.internal:8094", "description": "POC（容器内访问宿主机）"},
        {"url": "http://127.0.0.1:8094", "description": "POC 本机直连"},
    ],
    "tags": [{"name": "问答", "description": "面向对话场景的统一入口"},
             {"name": "查询", "description": "结构化查询接口（均按 user 收窄）"},
             {"name": "身份", "description": "用户与权限范围"},
             {"name": "口径", "description": "流程与状态口径"}],
    "paths": {
        "/req/users": {"get": _ep(
            "身份", "reqUsers", "可选用户列表",
            "返回有需求单关联的自然人（姓名、角色、可见单数）。POC 用于页面下拉选择，"
            "生产环境应由门户登录态提供身份，不对外开放本接口。", resp=_resp(USERS_RESP))},
        "/req/me": {"get": _ep(
            "身份", "reqMe", "当前用户身份与可见范围",
            "返回该用户可见的需求单清单及「我的角色」（申请人/审核处理人）。",
            params=[_param("user", "当前用户姓名", "张枫")], resp=_resp(ME_RESP))},
        "/req/ask": {"get": _ep(
            "问答", "reqAsk", "统一问答入口（服务端意图路由）",
            "按 query 识别 intent（detail/list/overview/flow/denied/need_user/unknown），"
            "返回结构化事实与 llm_input。**数据范围按 user 收窄**；越权单返回 denied 且不含任何单据字段。",
            params=[_param("query", "用户原始问句",
                           "现在有哪些需求单在审核，审核人是谁，等了多久"),
                    _param("user", "当前用户姓名（门户登录态透传）", "张枫")],
            resp=_resp(ASK_RESP))},
        "/req/lookup": {"get": _ep(
            "问答", "reqLookup", "仅定位单据（供路由版）",
            "只做 detail/list/denied/unknown，不发散到统计与流程；供 Dify 分类器分流后的"
            "「单单进度」分支调用。",
            params=[_param("query", "用户原始问句", "综管个人积分统计表的进度"),
                    _param("user", "当前用户姓名", "张枫")],
            resp=_resp(ASK_RESP))},
        "/req/detail": {"get": _ep(
            "查询", "reqDetail", "单单详情",
            "返回某张需求单的七阶段进度、当前环节/审核人/等待时长、需求项、实施单与流转日志。",
            params=[_param("req_no", "需求单号（19 位数字字符串）", "20261019100504019350"),
                    _param("user", "当前用户姓名", "张枫")],
            resp={**_resp(DETAIL_RESP),
                  "403": {"description": "单据不属于当前用户（intent=denied）或缺少 user"},
                  "404": {"description": "需求单号不存在（ok=false + error）"}})},
        "/req/search": {"get": _ep(
            "查询", "reqSearch", "关键词筛单",
            "按单号/目录名称/目录编码/申请人/单位/实施单号双向子串匹配，仅在当前用户可见范围内返回。",
            params=[_param("keyword", "关键词", "物流管控对标"),
                    _param("user", "当前用户姓名", "张枫")],
            resp={**_resp(SEARCH_RESP),
                  "403": {"description": "命中的单据不属于当前用户（intent=denied）"}})},
        "/req/overview": {"get": _ep(
            "查询", "reqOverview", "范围内统计",
            "当前用户可见范围内的状态分布、环节分布、审核中在办清单（审核环节 + 审核人 + 已等待时长）。",
            params=[_param("user", "当前用户姓名", "张枫")],
            resp={**_resp(OVERVIEW_RESP),
                  "403": {"description": "缺少 user 参数（intent=need_user）"}})},
        "/req/flow": {"get": _ep(
            "口径", "reqFlow", "流程与状态口径",
            "七阶段进度条、四环节状态分类、审核顺序与展示规则。无需身份。",
            resp=_resp(FLOW_RESP))},
    },

    "components": {"schemas": SCHEMAS},
}


# ---------------------------------------------------------------- Dify 对话代理
DIFY_BASE = os.environ.get("DIFY_BASE", "http://127.0.0.1:8070/v1")
_OPENER = __import__("urllib.request", fromlist=["request"]).build_opener(
    __import__("urllib.request", fromlist=["request"]).ProxyHandler({}))


def app_key():
    p = os.path.join(HERE, "app_key.txt")
    try:
        with open(p, encoding="utf-8") as f:
            return f.read().strip()
    except OSError:
        return ""


def dify_chat(user, query, conversation_id=""):
    import urllib.request
    key = app_key()
    if not key:
        return {"ok": False, "error": "未找到 app_key.txt，请先运行 build_chatflow.py"}
    body = json.dumps({"inputs": {"user_name": user}, "query": query,
                       "response_mode": "blocking", "conversation_id": conversation_id,
                       "user": "poc-" + (user or "anonymous")}).encode("utf-8")
    r = urllib.request.Request(DIFY_BASE + "/chat-messages", data=body,
                               headers={"Authorization": "Bearer " + key,
                                        "Content-Type": "application/json"})
    try:
        with _OPENER.open(r, timeout=180) as resp:
            d = json.loads(resp.read().decode("utf-8"))
        return {"ok": True, "answer": d.get("answer", ""),
                "conversation_id": d.get("conversation_id", ""),
                "message_id": d.get("message_id", "")}
    except Exception as e:                          # noqa: BLE001
        detail = ""
        if hasattr(e, "read"):
            try:
                detail = e.read().decode("utf-8")[:500]
            except Exception:                       # noqa: BLE001
                pass
        return {"ok": False, "error": "%s %s" % (e, detail)}


class Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _html(self, path):
        try:
            with open(path, encoding="utf-8") as f:
                body = f.read().encode("utf-8")
        except OSError:
            self._send({"ok": False, "error": "缺少 %s" % os.path.basename(path)}, 404)
            return
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, fmt, *a):
        print("[%s] %s" % (dt.datetime.now().strftime("%H:%M:%S"), fmt % a))

    def do_GET(self):
        u = urlparse(self.path)
        qs = {k: v[0] for k, v in parse_qs(u.query).items()}
        user = (qs.get("user") or "").strip()
        now = dt.datetime.now()
        try:
            if u.path in ("/", "/ui"):
                self._html(os.path.join(HERE, "index.html"))
            elif u.path in ("/demo", "/demo.html"):
                self._html(os.path.join(HERE, "demo.html"))
            elif u.path == "/api/params":
                # Dify 应用参数（开场白 + 开场建议问题），供统一演示页拉取
                try:
                    import urllib.request as _ur
                    opener = _ur.build_opener(_ur.ProxyHandler({}))
                    req = _ur.Request("http://127.0.0.1:8070/v1/parameters",
                                      headers={"Authorization": "Bearer " + app_key()})
                    obj = json.loads(opener.open(req, timeout=8).read().decode("utf-8"))
                    self._send({"ok": True, "opening_statement": obj.get("opening_statement") or "",
                                "suggested_questions": obj.get("suggested_questions") or []})
                except Exception as e:                     # noqa: BLE001
                    self._send({"ok": False, "error": str(e)})
            elif u.path == "/api":
                self._send({"服务": "需求状态追踪 POC Mock API", "端口": 8094,
                            "数据截至": DATA_UNTIL,
                            "接口": ["/req/users", "/req/me?user=", "/req/ask?query=&user=",
                                     "/req/lookup?query=&user=", "/req/detail?req_no=&user=",
                                     "/req/search?keyword=&user=", "/req/overview?user=",
                                     "/req/flow", "/openapi.json", "POST /chat"]})
            elif u.path == "/openapi.json":
                self._send(OPENAPI)
            elif u.path == "/req/users":
                self._send({"ok": True, **users(now)})
            elif u.path == "/req/me":
                if not user:
                    self._send({"ok": False, "error": "缺少 user 参数"}, 400)
                else:
                    self._send({"ok": True, "data": me(now, user)})
            elif u.path == "/req/ask":
                self._send(ask(qs.get("query", ""), now, user))
            elif u.path == "/req/lookup":
                qy = qs.get("query", "")
                intent, data, _ = route(qy, now, user, lookup_only=True)
                _, seq = scoped_index(now, user) if user else ({}, [])
                self._send({"ok": True, "intent": intent, "user": user, "data": data,
                            "data_until": DATA_UNTIL,
                            "llm_input": llm_of(intent, data, qy, user, len(seq))})
            elif u.path == "/req/detail":
                no = qs.get("req_no", "").strip()
                idx_all, _ = orders_index(now)
                idx, _ = scoped_index(now, user)
                if not user:
                    self._send({"ok": False, "error": "缺少 user 参数", "intent": "need_user"}, 403)
                elif no in idx:
                    self._send({"ok": True, "data": idx[no]})
                elif no in idx_all:
                    self._send({"ok": False, "intent": "denied",
                                "error": "该需求单不在您的查询范围"}, 403)
                else:
                    self._send({"ok": False, "error": "需求单号不存在：%s" % no}, 404)
            elif u.path == "/req/search":
                kw = qs.get("keyword", "").strip()
                intent, data, _ = route(kw, now, user, lookup_only=True)
                if intent == "detail":
                    self._send({"ok": True, "命中": 1, "单据": [brief(data)]})
                elif intent == "list":
                    self._send({"ok": True, "命中": len(data),
                                "单据": [brief(v) for v in data]})
                elif intent == "denied":
                    self._send({"ok": False, "intent": "denied", "命中": 0,
                                "error": "该关键词命中的需求单不在您的查询范围"}, 403)
                else:
                    self._send({"ok": True, "命中": 0, "单据": []})
            elif u.path == "/req/overview":
                if not user:
                    self._send({"ok": False, "error": "缺少 user 参数", "intent": "need_user"}, 403)
                else:
                    self._send({"ok": True, "data": overview(now, user)})
            elif u.path == "/req/flow":
                self._send({"ok": True, "data": FLOW_INFO})
            else:
                self._send({"ok": False, "error": "unknown path"}, 404)
        except Exception as e:                      # noqa: BLE001
            import traceback
            traceback.print_exc()
            self._send({"ok": False, "error": "%s: %s" % (type(e).__name__, e)}, 500)

    def do_POST(self):
        u = urlparse(self.path)
        try:
            n = int(self.headers.get("Content-Length") or 0)
            payload = json.loads(self.rfile.read(n).decode("utf-8") or "{}")
        except Exception:                           # noqa: BLE001
            self._send({"ok": False, "error": "请求体不是合法 JSON"}, 400)
            return
        if u.path == "/chat":
            user = (payload.get("user") or "").strip()
            query = (payload.get("query") or "").strip()
            if not user:
                self._send({"ok": False, "error": "请先选择或输入姓名"}, 400)
            elif not query:
                self._send({"ok": False, "error": "问题不能为空"}, 400)
            else:
                self._send(dify_chat(user, query, payload.get("conversation_id") or ""))
        else:
            self._send({"ok": False, "error": "unknown path"}, 404)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8094)
    args = ap.parse_args()
    srv = ThreadingHTTPServer(("0.0.0.0", args.port), Handler)
    print("需求状态追踪 Mock API 监听 0.0.0.0:%d，数据截至 %s" % (args.port, DATA_UNTIL))
    srv.serve_forever()


if __name__ == "__main__":
    main()
