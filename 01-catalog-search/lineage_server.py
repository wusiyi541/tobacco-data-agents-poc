# -*- coding: utf-8 -*-
"""血缘渲染服务（智能寻数 POC）
GET /health                       -> {"status":"ok"}
GET /lineage?table=<表名>          -> JSON {table, chain, authority_source, image_url}
GET /lineage.png?table=<表名>      -> PNG 图片（按层 ODS->DWD->DWS->ADS 溯源图）
"""
import json, os, hashlib
from http.server import HTTPServer, BaseHTTPRequestHandler
from urllib.parse import urlparse, parse_qs, quote

import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch, FancyArrowPatch
matplotlib.rcParams['font.sans-serif'] = ['Microsoft YaHei', 'SimHei']
matplotlib.rcParams['axes.unicode_minus'] = False

HERE = os.path.dirname(os.path.abspath(__file__))
PNG_DIR = os.path.join(HERE, 'png')
os.makedirs(PNG_DIR, exist_ok=True)
LN = json.load(open(os.path.join(HERE, 'lineage.json'), encoding='utf-8'))
NODES = {n['name']: n for n in LN['nodes']}
EDGES = [(e['source'], e['target'], e.get('desc', '')) for e in LN['edges']]
LAYER_COLOR = {'ODS': '#5B6B7C', 'DWD': '#2C6E8F', 'DWS': '#1A7A6E', 'ADS': '#B4632C'}
LAYER_ORDER = ['ODS', 'DWD', 'DWS', 'ADS']


def upstream(table):
    """返回 (血缘节点列表, 边列表)：table 及其全部上游，按层排布"""
    parents = {}
    for s, t, d in EDGES:
        parents.setdefault(t, []).append((s, d))
    seen, nodes, edges = {table}, [table], []
    stack = [table]
    while stack:
        cur = stack.pop()
        for s, d in parents.get(cur, []):
            edges.append((s, cur, d))
            if s not in seen:
                seen.add(s)
                nodes.append(s)
                stack.append(s)
    nodes.sort(key=lambda n: LAYER_ORDER.index(NODES[n]['layer']) if n in NODES else 9)
    return nodes, edges


def render_png(table):
    h = hashlib.md5(table.encode()).hexdigest()[:10]
    path = os.path.join(PNG_DIR, f'lineage_{h}.png')
    if os.path.exists(path):
        return path
    nodes, edges = upstream(table)
    nodes = [n for n in nodes if n in NODES]
    if not nodes:
        nodes = [table]
    # 每层一列，层内纵向排布
    layers = {}
    for n in nodes:
        layers.setdefault(NODES[n]['layer'], []).append(n)
    used = [l for l in LAYER_ORDER if l in layers]
    ncols = len(used)
    col_of = {l: i for i, l in enumerate(used)}
    pos = {}
    for l, ns in layers.items():
        for r, n in enumerate(ns):
            pos[n] = (col_of[l], -r)
    W = max(3.2 * ncols, 6.4)
    maxrows = max(len(v) for v in layers.values())
    H = max(1.9 * maxrows + 2.4, 4.2)
    fig, ax = plt.subplots(figsize=(W, H), dpi=110)
    ax.set_xlim(-0.7, ncols - 0.3)
    ax.set_ylim(-maxrows - 0.9, 1.7)
    ax.axis('off')
    # 层列标题
    for l in used:
        ax.text(col_of[l], 1.15, l, ha='center', va='center', fontsize=13,
                fontweight='bold', color=LAYER_COLOR[l])
    # 边（先画，压在框下）
    for s, t, d in edges:
        if s not in pos or t not in pos:
            continue
        (x1, y1), (x2, y2) = pos[s], pos[t]
        ax.add_patch(FancyArrowPatch((x1 + 0.38, y1), (x2 - 0.38, y2),
                                     arrowstyle='-|>', mutation_scale=16,
                                     color='#8A98A8', lw=1.6, zorder=1))
    # 节点框
    auth = LN['authority_map'].get(table, '')
    for n, (x, y) in pos.items():
        layer = NODES[n]['layer']
        hl = (n == table)
        fc = '#FFF6EC' if hl else '#FFFFFF'
        ec = '#B4632C' if hl else LAYER_COLOR[layer]
        box = FancyBboxPatch((x - 0.36, y - 0.32), 0.72, 0.64,
                             boxstyle='round,pad=0.03,rounding_size=0.08',
                             fc=fc, ec=ec, lw=2.4 if hl else 1.4, zorder=2)
        ax.add_patch(box)
        label = n if len(n) <= 14 else n[:13] + '…'
        ax.text(x, y + 0.08, label, ha='center', va='center', fontsize=10.5,
                fontweight='bold' if hl else 'normal', color='#12293F', zorder=3)
        ax.text(x, y - 0.17, NODES[n]['desc'][:12], ha='center', va='center',
                fontsize=8, color='#7A8896', zorder=3)
    ax.set_title(f'表级血缘链路 · 溯源「{table}」', fontsize=14, fontweight='bold',
                 color='#12293F', pad=14)
    if auth:
        ax.text(0.5, -maxrows - 0.55, f'权威来源：{auth}', transform=ax.transData,
                ha='center', fontsize=10.5, color='#1A7A6E', fontweight='bold')
    fig.savefig(path, bbox_inches='tight', facecolor='white')
    plt.close(fig)
    return path


class Handler(BaseHTTPRequestHandler):
    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path == '/health':
            return self._json({'status': 'ok'})
        if u.path == '/lineage' or u.path == '/lineage.png':
            table = (q.get('table') or [''])[0].strip()
            try:  # HTTP 请求行按 latin-1 解码，中文需还原为 UTF-8
                table = table.encode('latin-1').decode('utf-8')
            except (UnicodeEncodeError, UnicodeDecodeError):
                pass
            if not table:
                return self._json({'ok': False, 'error': '缺少 table 参数'}, 400)
            if table not in NODES:
                return self._json({'ok': False,
                                   'error': f'血缘中无该表：{table}'}, 404)
            nodes, edges = upstream(table)
            if u.path == '/lineage.png':
                try:
                    path = render_png(table)
                    data = open(path, 'rb').read()
                    self.send_response(200)
                    self.send_header('Content-Type', 'image/png')
                    self.send_header('Content-Length', str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
                except Exception as e:
                    self._json({'ok': False, 'error': str(e)}, 500)
                return
            return self._json({
                'ok': True,
                'table': table,
                'chain': [{'name': n, 'layer': NODES[n]['layer'],
                           'desc': NODES[n]['desc']} for n in nodes
                          if n in NODES],
                'authority_source': LN['authority_map'].get(table, ''),
                'image_url': f'http://127.0.0.1:8091/lineage.png?table={quote(table)}',
            })
        return self._json({'ok': False, 'error': 'not found'}, 404)

    def log_message(self, fmt, *args):
        print('[8091]', fmt % args)


if __name__ == '__main__':
    print('血缘渲染服务 http://127.0.0.1:8091  (GET /lineage?table=…, /lineage.png?table=…)')
    HTTPServer(('0.0.0.0', 8091), Handler).serve_forever()
