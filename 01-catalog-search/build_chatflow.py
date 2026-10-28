# -*- coding: utf-8 -*-
"""构建「智能寻数POC」Chatflow：意图识别 + 寻数卡片 + 血缘图 + 通用问答"""
import json, io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import urllib.request, urllib.error

HERE = os.path.dirname(os.path.abspath(__file__))
B = 'http://127.0.0.1:8070'

csrf = ''; cookie = ''; TOKEN = ''
for line in open(os.environ.get('DIFY_COOKIE_FILE', '.dify_cookie'), encoding='utf-8'):
    line = line.strip()
    if line.startswith('#HttpOnly_'):
        line = line[len('#HttpOnly_'):]
    elif line.startswith('#'):
        continue
    f = line.split('\t')
    if len(f) >= 7:
        cookie += f[5] + '=' + f[6] + '; '
        if f[5] == 'csrf_token':
            csrf = f[6]
        if f[5] == 'access_token':
            TOKEN = f[6]

def call(method, path, payload=None, base='console/api'):
    req = urllib.request.Request(f'{B}/{base}' + path,
                                 data=json.dumps(payload).encode('utf-8') if payload is not None else None,
                                 method=method)
    req.add_header('Cookie', cookie)
    req.add_header('X-CSRF-Token', csrf)
    req.add_header('Authorization', 'Bearer ' + TOKEN)
    if payload is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('ERR', method, path, e.code, e.read().decode('utf-8')[:300])
        return None

DS = json.load(open(f'{HERE}/dataset_ids.json', encoding='utf-8'))
MODEL = {"provider": "langgenius/openai_api_compatible/openai_api_compatible",
         "name": "MiniMax-M3", "mode": "chat",
         "completion_params": {"temperature": 0.2, "top_p": 1, "enable_thinking": False}}

# 上游 MiniMax 偶发 529 overloaded_error（集群过载），属瞬时错误但**不是**限流：
# 短间隔连打只会加重拥堵，所以这里只做「少量 + 长间隔」的兜底重试。
# Dify 节点重试是固定 interval、无退避无抖动（graph_engine/error_handler.py 直接 time.sleep），
# 真正的指数退避 + 抖动放在 demo_server.py 的接入层做，节点层只保留 1 次兜底。
RETRY = {"retry_enabled": True, "max_retries": 1, "retry_interval": 8000}

TABLES = [n['name'] for n in json.load(open(f'{HERE}/lineage.json', encoding='utf-8'))['nodes']]

CODE_FILTER = r'''
import re
def main(query: str, chunks: list) -> dict:
    q = query or ''
    items = []
    for c in (chunks or []):
        text = c.get('content', '') if isinstance(c, dict) else str(c)
        def g(tag):
            m = re.search(r'\u3010' + tag + r'\u3011([^\n\u3010]+)', text)
            return m.group(1).strip() if m else ''
        name, tags, desc = g('目录名称'), g('业务标签'), g('目录简介')
        # 目录简介里常夹带【目录编码】307022...【所属机构】… 这类标签块，
        # 不清掉会被模型抄进卡片标题。只保留中文叙述，去掉方括号块与编号串。
        desc = re.sub(r'\u3010[^\u3011]*\u3011', ' ', desc)
        desc = re.sub(r'\b\d{8,}\S*', ' ', desc)
        desc = re.sub(r'\s+', ' ', desc).strip(' 　|/')
        # 名称只取第一个标签之前的内容，避免"名称+机构+编码"粘连
        name = re.sub(r'\u3010.*$', '', name).strip()
        items.append({'name': name, 'tags': tags, 'desc': desc[:120],
                      'fields': g('包含字段')[:150], 'org': g('所属机构'),
                      'dtype': g('数据类型'), 'score': 0.0})
    segs = re.findall(r'[\u4e00-\u9fa5A-Za-z0-9_]{2,}', q)
    for it in items:
        hay = it['name'] + it['tags'] + it['desc']
        for s in segs:
            if s in hay:
                it['score'] += 2
            elif any(s[i:i+2] in hay for i in range(len(s) - 1)):
                it['score'] += 0.5
    items.sort(key=lambda x: -x['score'])
    items = items[:5]
    lines = []
    for i, it in enumerate(items, 1):
        lines.append(f"{i}. {it['name']}\n   业务标签：{it['tags']}\n   简介：{it['desc']}\n   字段：{it['fields']}")
    return {'context': '\n'.join(lines), 'count': len(items)}
'''

CODE_MATCH = r'''
import re, json
TABLES = json.loads(TABLES_JSON)
def main(query: str) -> dict:
    q = re.sub(r'\s+', '', query or '')
    hit = ''
    for t in TABLES:
        if t in q:
            hit = t
            break
    return {'table': hit or '未识别', 'found': 'yes' if hit else 'no'}
'''.replace('TABLES_JSON', json.dumps(json.dumps(TABLES, ensure_ascii=False)))

CODE_LINEAGE = r'''
import json as _json
def main(http_body: str, table: str) -> dict:
    try:
        d = _json.loads(http_body)
        assert d.get('ok')
    except Exception:
        return {'markdown': '血缘接口返回异常：' + (http_body or '')[:200]}
    parts = [f"{n['layer']} · {n['name']}" for n in d.get('chain', [])]
    chain_md = ' → '.join(parts) if parts else table
    auth = d.get('authority_source') or '暂未在权威表清单中认定'
    url = d.get('image_url', '')
    md = (f"已为你溯源「{table}」的表级血缘链路：\n\n{chain_md}\n\n"
          f"![血缘链路图]({url})\n\n"
          f"- **权威来源**：{auth}\n"
          f"- 血缘链路与图片由血缘接口直接渲染，不经大模型，口径可复现。")
    return {'markdown': md}
'''

CODE_TRIM = r'''
import re
def main(text: str) -> dict:
    """裁掉落选说明段：模型偶尔仍会补一段解释未推荐目录，这里在回答节点前清掉。"""
    t = (text or '').strip()
    # 1) 从「未推荐说明 / 提示：/ 落选说明 / 未推荐目录」所在行起，整段丢弃
    t = re.split(r'\n\s*>?\s*(?:未推荐说明|未推荐目录|落选说明|落选目录|提示)\s*[：:]', t)[0]
    # 2) 行内的「候选N（…）」清单也一并清掉
    t = re.sub(r'\n\s*>?\s*候选\s*\d+\s*[（(][^\n]*', '', t)
    # 3) 「目录#N」这类引用整行删除
    t = re.sub(r'\n[^\n]*目录\s*#\s*\d+[^\n]*', '', t)
    # 4) 按钮偶尔被模型套上 **粗体**，剥掉星号只留链接本体
    t = re.sub(r'\*{0,2}(\[[^\]]*加入选数车[^\]]*\]\(#\))\*{0,2}', r'\1', t)
    return {'text': t.strip()}
'''

nodes = []
def node(nid, ntype, title, pos, **data):
    d = {'id': nid, 'type': 'custom', 'position': pos,
         'data': {'type': ntype, 'title': title, **data}}
    nodes.append(d)

node('start', 'start', '开始', {'x': 60, 'y': 300}, variables=[])
node('classifier', 'question-classifier', '意图识别', {'x': 300, 'y': 300},
     query_variable_selector=['sys', 'query'], model=MODEL,
     classes=[{'id': 'seek', 'name': '寻找数据'},
              {'id': 'lineage', 'name': '查询权威表或血缘'},
              {'id': 'other', 'name': '其他咨询'}],
     instructions=('判断用户意图：'
                   '「寻找数据」=想找数据、找表、找目录、找指标、问哪里有某类数据、想申请数据；'
                   '「查询权威表或血缘」=问某张表是不是权威、数据从哪来、上下游血缘、哪张表是源头；'
                   '「其他咨询」=问术语含义、指标口径定义、功能使用方法、寒暄等。'),
     topics=[], vision={'enabled': False}, retry_config=RETRY)

# —— 分支① 寻数 ——
node('kr_catalog', 'knowledge-retrieval', '检索-数据目录', {'x': 620, 'y': 80},
     query_variable_selector=['sys', 'query'], dataset_ids=[DS['寻数POC-数据目录']],
     retrieval_mode='multiple',
     multiple_retrieval_config={'top_k': 8, 'score_threshold': 0.3,
                                'reranking_enable': False,
                                'reranking_model': {'reranking_provider_name': '', 'reranking_model_name': ''}})
node('code_filter', 'code', '混合过滤-标签加权', {'x': 880, 'y': 80},
     code_language='python3', code=CODE_FILTER,
     variables=[{'variable': 'query', 'value_selector': ['sys', 'query']},
                {'variable': 'chunks', 'value_selector': ['kr_catalog', 'result']}],
     outputs={'context': {'type': 'string'}, 'count': {'type': 'number'}})
node('llm_seek', 'llm', '卡片成文', {'x': 1140, 'y': 80}, model=MODEL,
     prompt_template=[
         {'id': 'sys', 'role': 'system', 'text': (
             '你是省烟草公司数据共享中心的「智能寻数助手」。把用户的数据需求与候选目录匹配，输出推荐卡片。\n\n'
             '# 铁律\n'
             '1. 只能使用「候选目录」里给出的目录，严禁编造或引入候选之外的任何表名、字段名。\n'
             '2. 每个推荐目录输出一张卡片，格式（保持 markdown）：\n\n'
             '**N. 目录名称**\n'
             '- 匹配理由：一句话，结合用户需求，从简介与业务标签提炼\n'
             '- 主要字段：从「字段」里挑 3~5 个业务字段（去掉系统技术字段）\n'
             '- 业务标签：原文标签\n\n'
             '每张卡片下另起一行输出按钮：**[🛒 加入选数车](#)**（链接保持 # 不变，后续由门户接入真实链接）\n'
             '3. 开头总述一句：「根据你的描述，匹配到 N 个数据目录，请勾选后加入选数车：」\n'
             '4. 若候选没有一个与需求相关，直接说明未匹配到合适目录并建议换一种说法，不得硬凑。\n'
             '5. 卡片序号（1. 2. …）仅对本次推荐的目录连续编号，从 1 开始。\n'
             '6. **只输出被推荐的目录**。未推荐的候选一律不出现在回答里，'
             '不解释落选原因、不列举落选目录、不写"未推荐说明"之类的段落。\n'
             '7. 只输出回答正文，不输出推理过程、JSON 或代码。')},
         {'id': 'user', 'role': 'user', 'text': (
             '用户需求：{{#sys.query#}}\n\n候选目录（已按相关度排序）：\n{{#code_filter.context#}}')}],
     context={'enabled': False, 'variable_selector': []},
     vision={'enabled': False}, retry_config=RETRY)
node('code_trim', 'code', '答案后处理', {'x': 1400, 'y': 80},
     code_language='python3', code=CODE_TRIM,
     variables=[{'variable': 'text', 'value_selector': ['llm_seek', 'text']}],
     outputs={'text': {'type': 'string'}})
node('answer_seek', 'answer', '回答-寻数卡片', {'x': 1660, 'y': 80},
     answer='{{#code_trim.text#}}')

# —— 分支② 血缘 ——
node('code_match', 'code', '表名匹配', {'x': 620, 'y': 330},
     code_language='python3', code=CODE_MATCH,
     variables=[{'variable': 'query', 'value_selector': ['sys', 'query']}],
     outputs={'table': {'type': 'string'}, 'found': {'type': 'string'}})
node('if_found', 'if-else', '是否命中表名', {'x': 860, 'y': 330},
     logical_operator='and',
     conditions=[{'variable_selector': ['code_match', 'found'],
                  'comparison_operator': 'contains', 'value': 'yes'}])
node('http_lineage', 'http-request', '血缘取数', {'x': 1100, 'y': 250},
     method='get', url='http://host.docker.internal:8091/lineage?table={{#code_match.table#}}',
     authorization={'type': 'no-auth', 'config': None},
     headers='', params='', body={'type': 'none', 'data': []},
     timeout={'max_connect_timeout': 30, 'max_read_timeout': 120, 'max_write_timeout': 120})
node('code_lineage', 'code', '血缘回答拼装', {'x': 1340, 'y': 250},
     code_language='python3', code=CODE_LINEAGE,
     variables=[{'variable': 'http_body', 'value_selector': ['http_lineage', 'body']},
                {'variable': 'table', 'value_selector': ['code_match', 'table']}],
     outputs={'markdown': {'type': 'string'}})
node('answer_lineage', 'answer', '回答-血缘图', {'x': 1580, 'y': 250},
     answer='{{#code_lineage.markdown#}}')
node('answer_nohit', 'answer', '回答-未命中', {'x': 1100, 'y': 460},
     answer=('未能从你的提问中识别到具体表名。请用目录里的表名来追问，例如：\n\n'
             '「卡片库_经济运行_月度统计 的数据是从哪来的？」\n「零售户终端销售信息表 的血缘是什么？」'))

# —— 分支③ 其他 ——
node('kr_other', 'knowledge-retrieval', '检索-术语口径权威', {'x': 620, 'y': 620},
     query_variable_selector=['sys', 'query'],
     dataset_ids=[DS['寻数POC-业务术语库'], DS['寻数POC-指标口径库'], DS['寻数POC-权威表清单']],
     retrieval_mode='multiple',
     multiple_retrieval_config={'top_k': 5, 'score_threshold': 0.3,
                                'reranking_enable': False,
                                'reranking_model': {'reranking_provider_name': '', 'reranking_model_name': ''}})
node('llm_other', 'llm', '通用成文', {'x': 880, 'y': 620}, model=MODEL,
     prompt_template=[
         {'id': 'sys', 'role': 'system', 'text': (
             '你是省烟草公司数据共享中心的「智能寻数助手」，负责回答业务术语、指标口径、权威表等咨询。\n\n'
             '# 铁律\n'
             '1. 只依据「参考资料」回答；参考资料没有的内容，明确说「知识库暂无该内容，建议咨询数据运营组」，不得编造。\n'
             '2. 用户打招呼或问「你能做什么／怎么用」时，不依赖参考资料，按以下固定口径自我介绍：\n'
             '   你好！我是数据共享中心的「智能寻数助手」，可以协助您：\n'
             '   - **推荐数据目录**：说出您想要的数据（如"各地市卷烟销量"），我会以卡片形式推荐相关数据目录，'
             '并可一键加入选数车发起申请；\n'
             '   - **查询权威来源**：确认指标数据取自哪张表、数据从哪来；\n'
             '   - **解释业务术语与指标口径**：如"12313""访销/拜访""有效零售户数""终端活跃率"。\n'
             '   您可以直接描述想要的数据，也可以告诉我指标或术语名称。\n'
             '3. 回答简洁、分点，语气面向业务人员。\n'
             '4. 只输出回答正文，不输出推理过程、JSON 或代码。')},
         {'id': 'user', 'role': 'user', 'text': (
             '用户问题：{{#sys.query#}}\n\n参考资料：\n{{#context#}}')}],
     context={'enabled': True, 'variable_selector': ['kr_other', 'result']},
     vision={'enabled': False}, retry_config=RETRY)
node('answer_other', 'answer', '回答-通用', {'x': 1140, 'y': 620},
     answer='{{#llm_other.text#}}')

edges = []
NTYPE = {n['id']: n['data']['type'] for n in nodes}
def edge(sid, tid, handle='source'):
    edges.append({'id': f'edge-{sid}-{tid}', 'type': 'custom',
                  'source': sid, 'sourceHandle': handle,
                  'target': tid, 'targetHandle': 'target',
                  'data': {'sourceType': NTYPE[sid], 'targetType': NTYPE[tid],
                           'isInIteration': False, 'isInLoop': False},
                  'zIndex': 0})

edge('start', 'classifier')
edge('classifier', 'kr_catalog', 'seek')
edge('classifier', 'code_match', 'lineage')
edge('classifier', 'kr_other', 'other')
edge('kr_catalog', 'code_filter')
edge('code_filter', 'llm_seek')
edge('llm_seek', 'code_trim')
edge('code_trim', 'answer_seek')
edge('code_match', 'if_found')
edge('if_found', 'http_lineage', 'true')
edge('http_lineage', 'code_lineage')
edge('code_lineage', 'answer_lineage')
edge('if_found', 'answer_nohit', 'false')
edge('kr_other', 'llm_other')
edge('llm_other', 'answer_other')

graph = {'nodes': nodes, 'edges': edges,
         'environment_variables': [], 'conversation_variables': []}
# 「下一步问题建议」提示词：三类各 1 条、顺序固定（血缘 / 权威来源 / 相关数据）
SUGGEST_PROMPT = (
    '你是省烟草公司数据共享中心「智能寻数助手」的提问建议器。'
    '请阅读对话记录中助手的最新回答，生成 3 条用户接下来最可能问的问题，每条不超过 20 个字，'
    '必须严格按下面三类各出 1 条，顺序固定：\n'
    '第 1 条·血缘：针对回答里出现的某张具体表，追问它的数据来源与加工链路（上游是哪张表、经过哪些加工）。\n'
    '第 2 条·权威来源：针对回答里出现的指标、字段或业务概念，追问权威表或标准口径以哪张表为准。\n'
    '第 3 条·相关数据：顺着当前话题，推荐继续查找的另一类相关数据目录。\n'
    '要求：\n'
    '1. 三条都必须紧扣最新回答里出现的具体表名、字段名、指标或业务概念，不得空泛；\n'
    '2. 用业务人员的口吻直接提问，像日常说话，不要客套、不要引号书名号；\n'
    '3. 三条互不重复，且都能被「查血缘、查权威表、找相关数据目录」这三类能力回答；\n'
    '4. 只输出 JSON 数组，形如 ["问题1","问题2","问题3"]，不要任何解释或多余文字。'
)
# 开场白与开场引导问题属于「运营文案」，控制台里常被手工增删，不应该由脚本每次重置。
# 以前的写法是把整个 features POST 覆盖 → 每跑一次脚本，手工加的条目就被冲回 3 条默认值。
# 现在的取值优先级：opening.json（本地，可复现）> 线上当前值（手工改的保住）> 兜底文案。
OPENING_FILE = f'{HERE}/opening.json'
FALLBACK_OPENING = ('您好，我是智能寻数助手。用日常业务语言告诉我您想找什么数据，'
                    '我帮您推荐数据目录并可加入选数车；也可以追问某张表的权威来源与血缘链路。')
FALLBACK_QUESTIONS = ['我想看各地市卷烟销量和同比，要能按月份拆',
                      '卡片库_经济运行_月度统计 的数据是从哪来的？',
                      '「购销调存」是什么意思？']

# 以下两项才是脚本该管的能力开关
features_fixed = {
    'suggested_questions_after_answer': {
        'enabled': True,
        'prompt': SUGGEST_PROMPT,
        # 这个内部调用默认走默认模型实例、不带 completion_params → MiniMax 思考过程会
        # 吃满 256 token，JSON 数组还没输出就被截断（解析为空）。显式关思考并放宽 token。
        'model': {
            'provider': 'langgenius/openai_api_compatible/openai_api_compatible',
            'name': 'MiniMax-M3',
            'mode': 'chat',
            'completion_params': {'temperature': 0.3, 'max_tokens': 512, 'enable_thinking': False},
        },
    },
    'retriever_resource': {'enabled': True},
}

# 建应用或复用
apps = call('GET', '/apps?page=1&limit=50')['data']
app = next((a for a in apps if a['name'] == '智能寻数POC'), None)
if app:
    app_id = app['id']
    print('[复用] app', app_id)
else:
    app = call('POST', '/apps', {'name': '智能寻数POC', 'mode': 'advanced-chat',
                                 'icon_type': 'emoji', 'icon': '🧭', 'icon_background': '#FFEAD5'})
    app_id = app['id']
    print('[新建] app', app_id)

cur = call('GET', f'/apps/{app_id}/workflows/draft') or {}
cur_feat = cur.get('features') or {}
local_opening = {}
if os.path.exists(OPENING_FILE):
    local_opening = json.load(open(OPENING_FILE, encoding='utf-8')) or {}

# 以线上 features 为基底再叠加，除下面显式改写的键外，控制台调过的设置一律不动
features = dict(cur_feat)
features.update(features_fixed)
features['opening_statement'] = (local_opening.get('opening_statement')
                                 or cur_feat.get('opening_statement')
                                 or FALLBACK_OPENING)
features['suggested_questions'] = (local_opening.get('suggested_questions')
                                   or cur_feat.get('suggested_questions')
                                   or FALLBACK_QUESTIONS)
print(f'[开场问题] {len(features["suggested_questions"])} 条（来源：'
      f'{"opening.json" if local_opening.get("suggested_questions") else ("线上现值" if cur_feat.get("suggested_questions") else "兜底")}）')
for _q in features['suggested_questions']:
    print('   -', _q)

payload = {'graph': graph, 'features': features}
if cur.get('hash'):
    payload['hash'] = cur['hash']
r = call('POST', f'/apps/{app_id}/workflows/draft', payload)
print('draft:', 'ok' if r is not None else 'FAIL')
r = call('POST', f'/apps/{app_id}/workflows/publish', {})
print('publish:', 'ok' if r is not None else 'FAIL')

# 应用 API key（保存完整 token）
# 注意：单应用上限 10 个 key，超了 publish 后这里必然 400 max_keys_exceeded，
# 属无害 —— 沿用已有的 app_key.txt 即可，不要让脚本在这里崩掉。
keys = call('GET', f'/apps/{app_id}/api-keys')
appkey = (keys or {}).get('data', [{}])[0].get('token') if keys else None
r = call('POST', f'/apps/{app_id}/api-keys', {})
if r and r.get('token'):
    appkey = r['token']
if appkey:
    open(f'{HERE}/app_key.txt', 'w').write(appkey)
    print('app key saved, len', len(appkey))
else:
    print('app key: 沿用 app_key.txt（未新建）')
