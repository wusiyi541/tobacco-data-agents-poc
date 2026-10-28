import os
# -*- coding: utf-8 -*-
"""批量创建 Dify 知识库并导入知识条目（控制台 API，Cookie + CSRF）"""
import json, io, sys, os
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
B = 'http://127.0.0.1:8070/console/api'

# 解析 Netscape cookie 文件（路径由环境变量 DIFY_COOKIE_FILE 指定）
cookie_parts, csrf, TOKEN = [], '', ''
for line in open(os.environ.get('DIFY_COOKIE_FILE', '.dify_cookie'), encoding='utf-8'):
    line = line.strip()
    if not line:
        continue
    if line.startswith('#HttpOnly_'):
        line = line[len('#HttpOnly_'):]
    elif line.startswith('#'):
        continue
    f = line.rstrip('\n').split('\t')
    if len(f) >= 7:
        cookie_parts.append(f'{f[5]}={f[6]}')
        if f[5] == 'csrf_token':
            csrf = f[6]
        if f[5] == 'access_token':
            TOKEN = f[6]
COOKIE = '; '.join(cookie_parts)
assert TOKEN, 'cookie jar 中无 access_token'
print('token from cookie jar, len', len(TOKEN))

def call(method, path, payload=None, is_json=True):
    url = B + path
    data = json.dumps(payload).encode('utf-8') if payload is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header('Cookie', COOKIE)
    req.add_header('X-CSRF-Token', csrf)
    req.add_header('Authorization', 'Bearer ' + TOKEN)
    if payload is not None:
        req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('ERR', method, path, e.code, e.read().decode('utf-8')[:200])
        return None

# 数据集 Service API Key（控制台创建，可复用已有）
def get_dataset_key():
    keys = call('GET', '/datasets/api-keys')
    if keys and keys.get('data'):
        return keys['data'][0]['token']
    r = call('POST', '/datasets/api-keys', {})
    return r.get('token')

DKEY = get_dataset_key()
print('dataset api key ok' if DKEY else 'dataset key FAIL')

def svc_call(path, payload):
    req = urllib.request.Request('http://127.0.0.1:8070/v1' + path,
                                 data=json.dumps(payload).encode('utf-8'), method='POST')
    req.add_header('Authorization', 'Bearer ' + DKEY)
    req.add_header('Content-Type', 'application/json')
    try:
        with urllib.request.urlopen(req, timeout=120) as r:
            return json.loads(r.read().decode('utf-8'))
    except urllib.error.HTTPError as e:
        print('SVC ERR', path, e.code, e.read().decode('utf-8')[:200])
        return None

DATASETS = [
    ('寻数POC-数据目录', f'{HERE}/kb_catalog.json'),
    ('寻数POC-业务术语库', f'{HERE}/kb_terms.json'),
    ('寻数POC-指标口径库', f'{HERE}/kb_metrics.json'),
    ('寻数POC-权威表清单', f'{HERE}/kb_authority.json'),
]

result = {}
for name, path in DATASETS:
    # 已存在则复用
    existing = call('GET', '/datasets?page=1&limit=50') or {}
    ds = next((d for d in existing.get('data', []) if d['name'] == name), None)
    if ds:
        ds_id = ds['id']
        print(f'[复用] {name} -> {ds_id}')
    else:
        r = call('POST', '/datasets', {'name': name, 'indexing_technique': 'high_quality',
                                       'permission': 'all_team_members'})
        ds_id = r['id']
        print(f'[新建] {name} -> {ds_id}')
    docs = json.load(open(path, encoding='utf-8'))
    ok, fail = 0, 0
    for d in docs:
        r = svc_call(f'/datasets/{ds_id}/document/create-by-text', {
            'name': d['name'][:60],
            'text': d['text'],
            'indexing_technique': 'high_quality',
            'process_rule': {'mode': 'automatic'},
        })
        if r and ('id' in r or 'document' in r or 'batch' in r):
            ok += 1
        else:
            fail += 1
    result[name] = ds_id
    print(f'  导入 {ok}/{len(docs)} 失败 {fail}')

json.dump(result, open(f'{HERE}/dataset_ids.json', 'w', encoding='utf-8'), ensure_ascii=False, indent=1)
print('完成', result)
