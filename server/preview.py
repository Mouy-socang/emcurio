"""发布前预览的纯渲染入口（AD-28；故事 11）。

Mac 后台不装 Jinja2（Mac 端只用标准库 + requests + PyYAML），所以预览由 Mac 以子进程调用本文件：
`server/.venv/bin/python preview.py`，标准输入一份 JSON，标准输出一份 JSON。渲染与服务器客人页用同一份
`server/templates` 与 `guest.py` 里的同一组渲染函数（home_page / item_view_page / pool_batch），只是数据换成
Mac 生成的候选快照（隐藏集合为空），页内地址加上前缀（默认 `/preview`），由 Mac 后台路由提供。

本文件不读写数据库、不连任何主机、不记录任何行为；只渲染。

输入：{"payload": 候选快照, "path": "/" | "/item/CO-NNNN" | "/legal" | "/api/pool",
       "query": "form=FN-001&n=40", "received_at": "2026-11-03T09:40:00.000Z", "prefix": "/preview"}
输出：{"status": 200, "type": "html" | "json", "body": "<html…>" | {…}}
"""
import json
import re
import sys
import time
from urllib.parse import parse_qs

import guest
import snapshot

ITEM_RE = re.compile(r'/item/(CO-[0-9]{4,8})')
PREFIX_RE = re.compile(r'/[a-z][a-z0-9-]{0,30}')
_ATTR = re.compile(r'(\s(?:href|src|action))="/(?!/)')
LOGOUT_FORM = '<form method="post" action="/logout">'


def prefix_urls(html, prefix):
    """页内站内地址（href / src / action 以 / 开头的）加上前缀；"退出登录"在预览里改成回到预览首页。"""
    html = _ATTR.sub(lambda m: f'{m.group(1)}="{prefix}/', html)
    return html.replace(LOGOUT_FORM.replace('"/logout"', f'"{prefix}/logout"'), f'<form method="get" action="{prefix}/">')


def render(payload, path, query='', received_at=None, prefix='/preview'):
    """返回 dict(status, type, body)。payload 为候选快照（结构同 contract/schemas/snapshot.v1）。"""
    if not PREFIX_RE.fullmatch(prefix):
        raise ValueError('前缀无效')
    env = guest.make_env()
    view = snapshot.View(payload.get('publish_no', 0), received_at, payload, {})
    params = {k: v[0] for k, v in parse_qs(query or '', keep_blank_values=True).items()}
    # 预览按未关联客人的默认（欧元）显示；币种切换只在页面内换算、不保存（无账号）。
    cur = guest.currency_ctx('EUR', view, time.time(), local=True)
    if path == '/api/pool':
        body = guest.pool_batch(env, view, params, cur)
        body['html'] = prefix_urls(body['html'], prefix)
        return {'status': 200, 'type': 'json', 'body': body}
    if path == '/':
        status, html = guest.home_page(env, view, params, cur)
    elif ITEM_RE.fullmatch(path):
        status, html = guest.item_view_page(env, view, path[len('/item/'):], cur)
    elif path == '/legal':
        version, terms_html = guest.load_terms(guest.DEFAULT_TERMS)
        status, html = guest.render_page(env, 'legal.html', '用户协议与隐私说明', version=version, html=terms_html,
                                         back='/')
    else:
        status, html = 404, ('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><title>页面不存在 · Curiobject'
                             '</title><p>页面不存在。<a href="/">返回首页</a></p></html>')
    return {'status': status, 'type': 'html', 'body': prefix_urls(html, prefix)}


def main():
    data = json.loads(sys.stdin.buffer.read().decode('utf-8'))
    out = render(data['payload'], data.get('path', '/'), data.get('query', ''), data.get('received_at'),
                 data.get('prefix', '/preview'))
    sys.stdout.buffer.write(json.dumps(out, ensure_ascii=False).encode('utf-8'))


if __name__ == '__main__':
    main()
