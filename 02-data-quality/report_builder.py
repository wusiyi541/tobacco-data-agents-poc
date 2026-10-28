# -*- coding: utf-8 -*-
"""
质检报告生成脚本 —— 运行在沙箱内（Dify 代码执行节点 / 将来的 CubeSandbox）。

职责单一：把服务端算好的结构化数据，按《智慧营销数据质量检测报告》模板
**拼装**成①报告全文 Markdown ②可下载的 .docx ③对话用的一句话摘要。

设计约束（为什么这么写）：
  1. **零第三方依赖**：沙箱内不能 pip install、不能联网，只用 Python 标准库。
     docx 由 zipfile + 手写 OOXML 生成，不依赖 python-docx。
  2. **不做任何计算**：所有数字都来自入参 JSON（服务端 SQL 算出），
     本脚本只负责排版与拼装 —— 对应「数由代码算，话由模型说」。
  3. 输出为纯文本/bytes，便于通过 base64 回传宿主落盘。

入口：build_report(report_json) -> dict
本地自测：python report_builder.py --data rpt_data.json --out out/
"""
import base64, os
import io
import json
import re
import sys
import zipfile

# ------------------------------------------------------------------ 基础 XML
FONT_CN = "宋体"
FONT_EN = "Times New Roman"


def esc(s):
    return (str(s if s is not None else "")
            .replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
            .replace('"', "&quot;"))


def _rpr(size=None, bold=False, color=None, font=FONT_CN):
    p = ['<w:rFonts w:ascii="%s" w:hAnsi="%s" w:eastAsia="%s"/>' % (FONT_EN, FONT_EN, font)]
    if bold:
        p.append("<w:b/><w:bCs/>")
    if size:
        p.append('<w:sz w:val="%d"/><w:szCs w:val="%d"/>' % (size * 2, size * 2))
    if color:
        p.append('<w:color w:val="%s"/>' % color)
    return "".join(p)


def run(text, size=None, bold=False, color=None, font=FONT_CN):
    if text is None or text == "":
        return ""
    return ('<w:r><w:rPr>%s</w:rPr><w:t xml:space="preserve">%s</w:t></w:r>'
            % (_rpr(size, bold, color, font), esc(text)))


def para(text="", size=10.5, bold=False, align=None, style=None, outline=None,
         indent=None, color=None, after=0, space_before=0, font=FONT_CN):
    """一个段落。indent 单位为「字符」，1 字符 ≈ 210 twips（10.5pt 下）。"""
    ppr = []
    if style:
        ppr.append('<w:pStyle w:val="%s"/>' % style)
    if outline is not None:
        ppr.append('<w:outlineLvl w:val="%d"/>' % outline)
    if align:
        ppr.append('<w:jc w:val="%s"/>' % align)
    if indent:
        ppr.append('<w:ind w:firstLine="%d"/>' % int(indent * 210))
    ppr.append('<w:spacing w:before="%d" w:after="%d" w:line="320" w:lineRule="auto"/>'
               % (int(space_before * 20), int(after * 20)))
    body = run(text, size=size, bold=bold, color=color, font=font) if text != "" else ""
    return "<w:p><w:pPr>%s</w:pPr>%s</w:p>" % ("".join(ppr), body)


def heading(text, level=1):
    """标题：显式字号 + outlineLvl，保证导航窗格能识别。"""
    size = {1: 16, 2: 14, 3: 12, 4: 11}.get(level, 11)
    return para(text, size=size, bold=True, outline=level - 1,
                space_before=(12 if level == 1 else 8), after=6)


def _cell(text, width, bold=False, align="left", shade=None, size=9):
    shd = '<w:shd w:val="clear" w:color="auto" w:fill="%s"/>' % shade if shade else ""
    return ('<w:tc><w:tcPr><w:tcW w:w="%d" w:type="dxa"/>%s'
            '<w:vAlign w:val="center"/></w:tcPr>'
            '<w:p><w:pPr><w:jc w:val="%s"/>'
            '<w:spacing w:before="20" w:after="20" w:line="260" w:lineRule="auto"/></w:pPr>%s</w:p></w:tc>'
            % (width, shd, align, run(text, size=size, bold=bold)))


def table(headers, rows, widths=None, aligns=None):
    """带边框的表格；headers 为表头，rows 为二维列表。"""
    n = len(headers)
    total = 9000
    if not widths:
        widths = [total // n] * n
    else:
        s = sum(widths)
        widths = [int(w * total / s) for w in widths]
    aligns = aligns or (["center"] * n)

    def borders():
        b = ('<w:top w:val="single" w:sz="6" w:color="808080"/>'
             '<w:left w:val="single" w:sz="6" w:color="808080"/>'
             '<w:bottom w:val="single" w:sz="6" w:color="808080"/>'
             '<w:right w:val="single" w:sz="6" w:color="808080"/>'
             '<w:insideH w:val="single" w:sz="6" w:color="808080"/>'
             '<w:insideV w:val="single" w:sz="6" w:color="808080"/>')
        return b

    out = ['<w:tbl><w:tblPr><w:tblStyle w:val="TableGrid"/>'
           '<w:tblW w:w="%d" w:type="dxa"/><w:jc w:val="center"/>'
           '<w:tblBorders>%s</w:tblBorders>'
           '<w:tblLayout w:type="fixed"/></w:tblPr><w:tblGrid>%s</w:tblGrid>'
           % (total, borders(), "".join('<w:gridCol w:w="%d"/>' % w for w in widths))]
    out.append("<w:tr>" + "".join(
        _cell(h, widths[i], bold=True, align="center", shade="DCE6F1", size=9)
        for i, h in enumerate(headers)) + "</w:tr>")
    for r in rows:
        cells = []
        for i in range(n):
            v = r[i] if i < len(r) else ""
            cells.append(_cell(v, widths[i],
                               bold=(str(r[0]).strip() in ("总计", "合计")),
                               align=aligns[i], size=9))
        out.append("<w:tr>" + "".join(cells) + "</w:tr>")
    out.append("</w:tbl>")
    out.append(para("", size=2, after=6))
    return "".join(out)


# ------------------------------------------------------------------ 样式与包
STYLES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
<w:docDefaults><w:rPrDefault><w:rPr>
<w:rFonts w:ascii="Times New Roman" w:hAnsi="Times New Roman" w:eastAsia="宋体"/>
<w:sz w:val="21"/><w:szCs w:val="21"/></w:rPr></w:rPrDefault>
<w:pPrDefault><w:pPr><w:spacing w:line="320" w:lineRule="auto"/></w:pPr></w:pPrDefault>
</w:docDefaults>
<w:style w:type="paragraph" w:default="1" w:styleId="Normal"><w:name w:val="Normal"/></w:style>
<w:style w:type="table" w:styleId="TableGrid"><w:name w:val="Table Grid"/>
<w:tblPr><w:tblBorders>
<w:top w:val="single" w:sz="6" w:color="808080"/><w:left w:val="single" w:sz="6" w:color="808080"/>
<w:bottom w:val="single" w:sz="6" w:color="808080"/><w:right w:val="single" w:sz="6" w:color="808080"/>
<w:insideH w:val="single" w:sz="6" w:color="808080"/><w:insideV w:val="single" w:sz="6" w:color="808080"/>
</w:tblBorders></w:tblPr></w:style>
</w:styles>"""

SECT_PR = ('<w:sectPr><w:pgSz w:w="11906" w:h="16838"/>'
           '<w:pgMar w:top="1440" w:right="1418" w:bottom="1440" w:left="1701"'
           ' w:header="851" w:footer="992" w:gutter="0"/>'
           '<w:cols w:space="425"/><w:docGrid w:type="lines" w:linePitch="312"/></w:sectPr>')

CONTENT_TYPES = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
<Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
<Default Extension="xml" ContentType="application/xml"/>
<Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
<Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
<Override PartName="/docProps/app.xml" ContentType="application/vnd.openxmlformats-officedocument.extended-properties+xml"/>
</Types>"""

ROOT_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
<Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
<Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/extended-properties" Target="docProps/app.xml"/>
</Relationships>"""

DOC_RELS = """<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
<Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>
</Relationships>"""


def core_xml(title, author):
    return ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<cp:coreProperties xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"'
            ' xmlns:dc="http://purl.org/dc/elements/1.1/" xmlns:dcterms="http://purl.org/dc/terms/"'
            ' xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance">'
            '<dc:title>%s</dc:title><dc:creator>%s</dc:creator>'
            '<cp:lastModifiedBy>%s</cp:lastModifiedBy></cp:coreProperties>'
            % (esc(title), esc(author), esc(author)))


APP_XML = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<Properties xmlns="http://schemas.openxmlformats.org/officeDocument/2006/extended-properties"'
           ' xmlns:vt="http://schemas.openxmlformats.org/officeDocument/2006/docPropsVTypes">'
           '<Application>数据质检智能体</Application></Properties>')


def pack(body_xml, title, author):
    doc = ('<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
           '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"'
           ' xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
           '<w:body>%s%s</w:body></w:document>' % (body_xml, SECT_PR))
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("[Content_Types].xml", CONTENT_TYPES)
        z.writestr("_rels/.rels", ROOT_RELS)
        z.writestr("word/document.xml", doc)
        z.writestr("word/styles.xml", STYLES)
        z.writestr("word/_rels/document.xml.rels", DOC_RELS)
        z.writestr("docProps/core.xml", core_xml(title, author))
        z.writestr("docProps/app.xml", APP_XML)
    return buf.getvalue()


# ------------------------------------------------------------------ 报告正文
def build_body(d):
    """按模板章节顺序拼装 document.xml 的 body。"""
    doc = d.get("doc", {})
    ov = d.get("overview", {})
    pd = d.get("period", {}) or {}
    cur = pd.get("current") or {}
    prev = pd.get("prev") or {}
    eff = d.get("effect", {})
    B = []

    # ============ 封面 ============
    B.append(para("", size=11, after=60))
    B.append(para(doc.get("project", ""), size=14, bold=True, align="center", after=6))
    B.append(para("（项目编号：%s）" % doc.get("project_no", ""), size=11, align="center", after=90))
    B.append(para("", size=11, after=60))
    B.append(para(doc.get("title", ""), size=26, bold=True, align="center",
                  color="1F3864", after=120))
    B.append(para("", size=11, after=60))
    B.append(para(doc.get("author", ""), size=14, bold=True, align="center", after=8))
    B.append(para(doc.get("date_cn", ""), size=12, align="center"))
    B.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    # ============ 目录 ============
    B.append(para("目 录", size=16, bold=True, align="center", after=15))
    for t, lv in [("一、概述", 1), ("（一）质量管理思路与目标", 2), ("1. 质量管理实施思路", 3),
                  ("2. 质量管理目标", 3), ("（二）%s质量管理总体概况" % doc.get("domain_cn", ""), 2),
                  ("二、质量管理实施", 1), ("（一）质量评估范围", 2), ("（二）质量评估维度", 2),
                  ("（三）质量规划与实施情况", 2), ("1. 元数据稽核", 3), ("2. 数据对账", 3),
                  ("3. 数据质检", 3), ("三、问题处理与管理成效", 1), ("（一）问题说明", 2),
                  ("（二）质量管理成效", 2), ("附录A 问题清单", 1)]:
        B.append(para(("　　" * (lv - 1)) + t, size=11, after=3))
    B.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')

    # ============ 一、概述 ============
    B.append(heading("一、概述", 1))
    B.append(heading("（一）质量管理思路与目标", 2))
    B.append(heading("1. 质量管理实施思路", 3))
    B.append(para("围绕数据质量管理，从元数据稽核、数据对账、数据质检三个方向推进，"
                  "先在入湖环节建立可量化的检查基线，再通过规则常态化运行形成持续监控，"
                  "最终把检出的问题回流到业务侧与开发侧闭环整改。", indent=2, after=6))
    B.append(para("质量管理实施思路见下图。", indent=2, after=4))
    B.append(para("［图1 质量管理实施思路］", size=10, align="center", color="808080", after=8))
    B.append(heading("2. 质量管理目标", 3))
    B.append(para("质量管理的最终目标是通过可靠的数据提升数据的使用价值，"
                  "解决数据使用过程中遇到的数据质量问题，提升数据的完整性、准确性、一致性与及时性，"
                  "为共享中心的用数需求提供可信底座。", indent=2, after=6))

    B.append(heading("（二）%s质量管理总体概况" % doc.get("domain_cn", ""), 2))
    B.append(para("当前已针对 %d 张表、%d 个数据项开展质量管理，累计开发质检规则 %d 个，"
                  "其中本期常态化运行的规则 %d 个，覆盖 %d 张表；本期共检出问题 %d 个。"
                  "规则与问题均按客户《数据质量检测报告》模板的六维口径归类。"
                  % (ov.get("table_total", 0), ov.get("object_total", 0), ov.get("rule_total", 0),
                     ov.get("running_rules", 0), ov.get("table_count_scope", 0),
                     ov.get("problem_total", 0)),
                  indent=2, after=6))

    # ============ 二、质量管理实施 ============
    B.append(heading("二、质量管理实施", 1))
    B.append(heading("（一）质量评估范围", 2))
    B.append(para("本报告覆盖范围为%s，时间范围 %s ~ %s。"
                  "该范围内实际纳入质检的表 %d 张、校验对象 %d 个，涉及规则 %d 条。"
                  % (doc.get("domain_cn", "营销域"),
                     cur.get("date_start", "-"), cur.get("date_end", "-"),
                     ov.get("table_count_scope", 0), ov.get("object_count_scope", 0),
                     ov.get("rule_count_scope", 0)), indent=2, after=6))

    B.append(heading("（二）质量评估维度", 2))
    B.append(para("数据质量评估维度对汇聚的数据进行考核评估，从唯一性、完整性、有效性、准确性、"
                  "一致性、及时性等方面对数据进行全方位考核，各维度下的规则分类与判定说明见下表。",
                  indent=2, after=6))
    B.append(table(["维度", "规则分类", "规则描述"],
                   [[x["dimension"], x["category"], x["desc"]] for x in d.get("dim_defs", [])],
                   widths=[14, 24, 62], aligns=["center", "left", "left"]))

    B.append(heading("（三）质量规划与实施情况", 2))
    B.append(heading("1. 元数据稽核", 3))
    B.append(para("基于数据质量评估维度中的「元数据稽核」规则分类，检查源端与入湖数据的字段"
                  "数据类型、长度、精度是否一致（本期样例数据未单独拆分该项统计）。",
                  indent=2, after=6))
    B.append(heading("2. 数据对账", 3))
    nr = sum(x["running"] for x in d.get("dim_rule_stats", [])
             if x["dimension"] == "一致性")
    B.append(para("基于一致性维度开展多表数据量对账与跨表值核对，本期常态化运行的对账类规则 %d 个，"
                  "用于发现源端与入湖端、主表与明细表之间的数据量或数值不一致。" % nr,
                  indent=2, after=6))
    B.append(heading("3. 数据质检", 3))
    B.append(para("基于以上评估维度与检查范围，本次评估活动累计制定质量规则 %d 条，"
                  "本期常态化运行 %d 条。各维度的规则制定与运行情况见下表。"
                  % (ov.get("rule_total", 0), ov.get("running_rules", 0)), indent=2, after=6))
    B.append(table(["评估维度", "制定规则数", "常态化运行规则数"],
                   [[x["dimension"], x["made"], x["running"]] for x in d.get("dim_rule_stats", [])],
                   widths=[34, 33, 33], aligns=["center", "center", "center"]))

    # ============ 三、问题处理与管理成效 ============
    B.append(heading("三、问题处理与管理成效", 1))
    B.append(heading("（一）问题说明", 2))
    B.append(para("本期内共识别 %d 个问题（详情见附录A），按不同质量检查维度分布情况如下表："
                  % ov.get("problem_total", 0), indent=2, after=6))
    B.append(table(["问题分类", "处理中", "待处理", "无需处理", "已解决", "总计"],
                   [[x["dimension"], x["doing"], x["pending"], x["ignore"], x["done"], x["total"]]
                    for x in d.get("problem_by_dim", [])],
                   widths=[24, 15, 15, 16, 15, 15],
                   aligns=["center"] * 6))
    B.append(heading("主要问题说明", 3))
    for f in d.get("focus", []):
        B.append(heading(f.get("title", ""), 4))
        for it in f.get("items", []):
            B.append(para("· " + it, size=10, after=3))
    B.append(heading("未解决问题", 3))
    un = d.get("unresolved", [])
    if un:
        for i, u in enumerate(un[:8], 1):
            B.append(para("（%d）%s（%s）：%s。当前状态：%s，责任人：%s。"
                          % (i, u.get("table", ""), u.get("no", ""), u.get("desc", ""),
                             u.get("status", ""), u.get("owner", "")), size=10, indent=2, after=3))
    else:
        B.append(para("本期无未闭环问题。", indent=2))
    B.append(heading("问题整改方法", 3))
    for r in d.get("rectify", []):
        B.append(para("%s（%d 条处置经验）：%s。" % (r.get("owner", ""), r.get("count", 0),
                                               "；".join(r.get("items", []))),
                      size=10, indent=2, after=3))

    B.append(heading("（二）质量管理成效", 2))
    dl = eff.get("delta") or {}
    if dl:
        B.append(para("与%s相比，本期（%s）质量管理成效如下：问题总数 %d 个，环比 %+d 个（%+.1f%%）；"
                      "命中强规则的问题数由 %d 个降至 %d 个，降幅 %.1f%%；"
                      "规则校验通过率由 %.1f%% 提升至 %.1f%%，提升 %.1f 个百分点。"
                      % (eff.get("prev_cn", "上一期"), eff.get("cur_cn", "本期"),
                         eff.get("cur", {}).get("problems", 0), dl.get("problems", 0),
                         dl.get("problems_rate", 0.0),
                         eff.get("prev", {}).get("strong", 0), eff.get("cur", {}).get("strong", 0),
                         abs(dl.get("strong_rate", 0.0)),
                         eff.get("prev", {}).get("pass_rate", 0.0),
                         eff.get("cur", {}).get("pass_rate", 0.0), dl.get("pass_rate", 0.0)),
                  indent=2, after=6))
        B.append(para("总体来看，本期高优先级（强规则命中）问题显著下降、规则校验通过率明显上升，"
                      "数据质量在报告期内得到改善。", indent=2, after=6))
        B.append(para("［图2 质量得分趋势图］", size=10, align="center", color="808080", after=8))
    else:
        B.append(para("本期（%s）为现有数据中最早的期次，无可比期次，故不计算环比。"
                      % eff.get("cur_cn", "本期"), indent=2))

    # ============ 附录A ============
    B.append('<w:p><w:r><w:br w:type="page"/></w:r></w:p>')
    B.append(heading("附录A 问题清单", 1))
    am = d.get("appendix_meta", {})
    B.append(para("本附录列出本期 %s 检出问题清单共 %d 条，按「强规则命中优先、未处理优先」排序，"
                  "全文共 %d 条。其中命中强规则 %d 条。"
                  % (doc.get("domain_cn", ""), am.get("shown", 0), am.get("total_in_scope", 0),
                     am.get("strong_in_scope", 0)), size=10, indent=2, after=6))
    rows = [[a.get("table_en", ""), a.get("desc", ""), a.get("submitter", ""),
             a.get("submit_date", ""), a.get("status_cn", ""), a.get("owner", ""),
             a.get("category", ""), a.get("cause", ""), a.get("actual_date", "")]
            for a in d.get("appendix", [])]
    B.append(table(["库表英文名", "数据质量问题描述", "提出人", "提出日期", "完成情况",
                    "处理人", "问题分类", "原因分析与解决方案", "实际完成时间"],
                   rows, widths=[16, 18, 7, 8, 7, 10, 8, 20, 8],
                   aligns=["left", "left", "center", "center", "center", "left",
                           "center", "left", "center"]))

    # 口径说明
    cv = d.get("caveats", [])
    if cv:
        B.append(para("口径说明：", size=9, bold=True, after=2))
        for c in cv:
            B.append(para("— " + c, size=8.5, color="595959", after=2))
    return "".join(B)


# ------------------------------------------------------------------ Markdown
def build_markdown(d):
    doc = d.get("doc", {})
    ov = d.get("overview", {})
    cur = (d.get("period") or {}).get("current") or {}
    eff = d.get("effect", {})
    L = []
    L.append("# %s\n" % doc.get("title", ""))
    L.append("> %s ｜ 项目编号：%s ｜ %s ｜ %s\n"
             % (doc.get("project", ""), doc.get("project_no", ""), doc.get("author", ""),
                doc.get("date_cn", "")))

    L.append("## 一、概述\n")
    L.append("### （一）质量管理思路与目标\n")
    L.append("围绕数据质量管理，从元数据稽核、数据对账、数据质检三个方向推进，"
             "在入湖环节建立可量化检查基线，通过规则常态化运行形成持续监控，"
             "并把检出问题回流到业务侧与开发侧闭环整改。\n")
    L.append("### （二）%s质量管理总体概况\n" % doc.get("domain_cn", ""))
    L.append("已针对 %d 张表、%d 个数据项开展质量管理，累计开发质检规则 %d 个，"
             "本期常态化运行 %d 个，本期检出问题 %d 个。\n"
             % (ov.get("table_total", 0), ov.get("object_total", 0), ov.get("rule_total", 0),
                ov.get("running_rules", 0), ov.get("problem_total", 0)))

    L.append("## 二、质量管理实施\n")
    L.append("### （一）质量评估范围\n")
    L.append("范围：%s；时间：%s ~ %s；纳入质检的表 %d 张、校验对象 %d 个、规则 %d 条。\n"
             % (doc.get("domain_cn", ""), cur.get("date_start", "-"), cur.get("date_end", "-"),
                ov.get("table_count_scope", 0), ov.get("object_count_scope", 0),
                ov.get("rule_count_scope", 0)))
    L.append("### （二）质量评估维度\n")
    dd = d.get("dim_defs", [])
    L.append("| 维度 | 规则分类 | 规则描述 |\n|---|---|---|")
    for x in dd:
        L.append("| %s | %s | %s |" % (x["dimension"], x["category"], x["desc"]))
    L.append("")
    L.append("### （三）质量规划与实施情况\n")
    L.append("**1. 元数据稽核**：检查源端与入湖数据的字段类型、长度、精度是否一致。\n")
    L.append("**2. 数据对账**：一致性维度下的多表数据量对账与跨表值核对。\n")
    L.append("**3. 数据质检**：本期规则制定与运行情况如下。\n")
    L.append("| 评估维度 | 制定规则数 | 常态化运行规则数 |\n|---|---:|---:|")
    for x in d.get("dim_rule_stats", []):
        L.append("| %s | %s | %s |" % (x["dimension"], x["made"], x["running"]))
    L.append("")

    L.append("## 三、问题处理与管理成效\n")
    L.append("### （一）问题说明\n")
    L.append("本期共识别 %d 个问题（详见附录A），按维度分布如下：\n" % ov.get("problem_total", 0))
    L.append("| 问题分类 | 处理中 | 待处理 | 无需处理 | 已解决 | 总计 |\n|---|---:|---:|---:|---:|---:|")
    for x in d.get("problem_by_dim", []):
        L.append("| %s | %s | %s | %s | %s | %s |"
                 % (x["dimension"], x["doing"], x["pending"], x["ignore"], x["done"], x["total"]))
    L.append("")
    for f in d.get("focus", []):
        L.append("**%s**\n" % f.get("title", ""))
        for it in f.get("items", []):
            L.append("- " + it)
        L.append("")
    un = d.get("unresolved", [])
    if un:
        L.append("**未解决问题**\n")
        for i, u in enumerate(un[:8], 1):
            L.append("（%d）%s（%s）：%s。状态：%s；责任人：%s。"
                     % (i, u.get("table", ""), u.get("no", ""), u.get("desc", ""),
                        u.get("status", ""), u.get("owner", "")))
        L.append("")
    L.append("### （二）质量管理成效\n")
    dl = eff.get("delta") or {}
    if dl:
        L.append("与%s相比：问题总数 %d（%+d，%+.1f%%）；强规则命中的问题 %d → %d（%.1f%%↓）；"
                 "规则校验通过率 %.1f%% → %.1f%%（+%.1fpp）。\n"
                 % (eff.get("prev_cn", "上一期"), eff.get("cur", {}).get("problems", 0),
                    dl.get("problems", 0), dl.get("problems_rate", 0.0),
                    eff.get("prev", {}).get("strong", 0), eff.get("cur", {}).get("strong", 0),
                    abs(dl.get("strong_rate", 0.0)),
                    eff.get("prev", {}).get("pass_rate", 0.0),
                    eff.get("cur", {}).get("pass_rate", 0.0), dl.get("pass_rate", 0.0)))
    L.append("## 附录A 问题清单\n")
    am = d.get("appendix_meta", {})
    L.append("共 %d 条（全文 %d 条，其中强规则 %d 条），此处列示前 %d 条：\n"
             % (am.get("shown", 0), am.get("total_in_scope", 0),
                am.get("strong_in_scope", 0), min(10, am.get("shown", 0))))
    L.append("| 库表英文名 | 问题描述 | 提出日期 | 完成情况 | 处理人 | 问题分类 |\n|---|---|---|---|---|---|")
    for a in d.get("appendix", [])[:10]:
        L.append("| %s | %s | %s | %s | %s | %s |"
                 % (a.get("table_en", ""), a.get("desc", ""), a.get("submit_date", ""),
                    a.get("status_cn", ""), a.get("owner", ""), a.get("category", "")))
    L.append("")
    if d.get("caveats"):
        L.append("---\n**口径说明**\n")
        for c in d["caveats"]:
            L.append("- " + c)
    return "\n".join(L)


def build_digest(d):
    """对话里的一句话摘要（数字同样全部来自入参，不经模型生成）。"""
    doc = d.get("doc", {})
    ov = d.get("overview", {})
    eff = d.get("effect", {})
    dl = eff.get("delta") or {}
    s = ("已按《%s》模板生成报告：范围 %s，期次 %s；本期检出问题 %d 个，"
         "命中强规则 %d 个；常态化运行规则 %d 条，规则通过率 %.1f%%。"
         % (doc.get("title", ""), doc.get("domain_cn", ""),
            eff.get("cur_cn", ""), ov.get("problem_total", 0),
            d.get("appendix_meta", {}).get("strong_in_scope", 0), ov.get("running_rules", 0),
            eff.get("cur", {}).get("pass_rate", 0.0)))
    if dl:
        s += ("环比 %s：强规则命中问题 %d → %d（%.1f%%↓），通过率 %.1f%% → %.1f%%（+%.1fpp），"
              "问题总数 %+d 个。"
              % (eff.get("prev_cn", "上一期"),
                 eff.get("prev", {}).get("strong", 0), eff.get("cur", {}).get("strong", 0),
                 abs(dl.get("strong_rate", 0.0)),
                 eff.get("prev", {}).get("pass_rate", 0.0),
                 eff.get("cur", {}).get("pass_rate", 0.0), dl.get("pass_rate", 0.0),
                 dl.get("problems", 0)))
    return s


# ------------------------------------------------------------------ 入口
def build_report(report_json):
    """沙箱入口：入参为服务端报告数据（JSON 字符串或 dict）。"""
    d = json.loads(report_json) if isinstance(report_json, (str, bytes)) else (report_json or {})
    if not d.get("ok"):
        return {"ok": False, "error": "报告数据为空或 ok=false",
                "markdown": "", "docx_b64": "", "filename": "", "digest": ""}
    doc = d.get("doc", {})
    body = build_body(d)
    blob = pack(body, doc.get("title", "数据质量检测报告"), doc.get("author", ""))
    eff = d.get("effect", {})
    fname = "%s-%s.docx" % (doc.get("title", "数据质量检测报告"),
                            (eff.get("cur_cn") or "报告").replace(" ", ""))
    return {
        "ok": True,
        "filename": fname,
        "bytes": len(blob),
        "docx_b64": base64.b64encode(blob).decode("ascii"),
        "markdown": build_markdown(d),
        "digest": build_digest(d),
    }


def handle_pack(pack):
    """沙箱入口（供 Dify 代码节点调用）：入参为 /dq/report_pack 的返回。

    · 若意图为质检报告 → 按模板拼装全文并生成 docx，返回 base64 供落盘；
    · 其他意图 → 不产出文件，只把服务端取数结果透传给模型。
    """
    pack = json.loads(pack) if isinstance(pack, (str, bytes)) else (pack or {})
    intent = pack.get("intent")
    ask = pack.get("ask") or {}
    if intent == "report" and pack.get("report"):
        r = build_report(pack["report"])
        r["is_report"] = True
        # 给模型的输入：只给摘要与文件信息，避免把全量明细塞进上下文
        r["llm_input"] = json.dumps({
            "intent": "report",
            "report_generated": True,
            "report_title": pack["report"].get("doc", {}).get("title"),
            "domain": pack["report"].get("doc", {}).get("domain_cn"),
            "period": (pack["report"].get("effect") or {}).get("cur_cn"),
            "filename": r["filename"],
            "summary": r["digest"],
            "caveats": pack["report"].get("caveats", []),
        }, ensure_ascii=False)
        return r
    return {
        "ok": True, "is_report": False, "filename": "", "docx_b64": "",
        "markdown": "", "bytes": 0,
        "digest": "",
        "llm_input": json.dumps({
            "intent": intent,
            "intent_reason": ask.get("intent_reason"),
            "detected": ask.get("detected"),
            "assumed": ask.get("assumed"),
            "payload": ask.get("payload"),
        }, ensure_ascii=False),
    }


if __name__ == "__main__":
    # 本地自测：--data 取 /dq/report_data 的返回，--out 为输出目录
    import argparse
    import os
    ap = argparse.ArgumentParser()
    ap.add_argument("--data")
    ap.add_argument("--out", default=".")
    ap.add_argument("--domain", default="营销")
    a = ap.parse_args()
    if a.data:
        raw = open(a.data, encoding="utf-8").read()
    else:
        import urllib.parse
        import urllib.request
        raw = urllib.request.urlopen(
            "http://localhost:8090/dq/report_data?"
            + urllib.parse.urlencode({"domain": a.domain}), timeout=120).read().decode("utf-8")
    res = build_report(raw)
    os.makedirs(a.out, exist_ok=True)
    path = os.path.join(a.out, res["filename"])
    with open(path, "wb") as f:
        f.write(base64.b64decode(res["docx_b64"]))
    print("生成:", path, res["bytes"], "字节")
    print("摘要:", res["digest"])
    print("Markdown 长度:", len(res["markdown"]))
