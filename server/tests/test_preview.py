"""故事 11：预览的纯渲染入口（server/preview.py，AD-28）——与客人页同一份模板、同一组渲染函数。

同一份快照：服务器客人页渲染出的 HTML 与预览渲染（去掉 /preview 前缀后）逐字相同；页内只引用站内地址。
"""
import json
import re
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app as server
import preview
from test_snapshot import AUTH, SAME_ORIGIN, TOKEN, Clock, build, make_item, put_image, send, sign_in

HERE = Path(__file__).resolve().parents[1]


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def client(db):
    return TestClient(server.create_app(db_path=db, admin_token=TOKEN, clock=Clock(), cookie_secure=False,
                                        public_url='https://curio.test'),
                      follow_redirects=False, headers=SAME_ORIGIN)


@pytest.fixture
def published(client, db):
    items = [make_item(n, [(put_image(client, f'b{n}'), put_image(client, f't{n}'))]) for n in range(1, 31)]
    payload = build(7, items, rank=[it['code'] for it in items][5:15])
    assert send(client, 'snapshot', payload).status_code == 200
    sign_in(client)
    with sqlite3.connect(db) as c:
        received = c.execute('SELECT received_at FROM snapshots WHERE publish_no=7').fetchone()[0]
    return payload, received


def unprefix(html):
    html = html.replace('<form method="get" action="/preview/">', preview.LOGOUT_FORM)
    return re.sub(r'(\s(?:href|src|action))="/preview/', r'\1="/', html)


@pytest.mark.parametrize('path,query', [('/', ''), ('/', 'form=FN-044'), ('/', 'n=40'), ('/item/CO-0003', ''),
                                        ('/item/CO-9999', ''), ('/legal', '')])
def test_preview_html_matches_guest_page(client, published, path, query):
    payload, received = published
    out = preview.render(payload, path, query, received)
    live = client.get(path + ('?' + query if query else ''))
    assert out['type'] == 'html' and out['status'] == live.status_code
    assert unprefix(out['body']) == live.text
    # 页内地址全部在预览前缀下，没有外部地址。
    assert not re.search(r'(?:href|src|action)="/(?!preview/)', out['body'])
    assert not re.search(r'https?://', out['body'])


def test_preview_pool_batch_matches_api(client, published):
    payload, received = published
    out = preview.render(payload, '/api/pool', 'offset=20', received)
    live = client.get('/api/pool?offset=20').json()
    assert out['type'] == 'json' and unprefix(out['body']['html']) == live['html']
    assert {k: v for k, v in out['body'].items() if k != 'html'} == {k: v for k, v in live.items() if k != 'html'}


def test_preview_rank_order_is_candidate_rank(published):
    payload, received = published
    html = preview.render(payload, '/', '', received)['body']
    ranked = re.findall(r'<li class="rk"><a class="rk-a" href="/preview/item/(CO-\d+)"', html)
    assert ranked == payload['rank']
    assert 'action="/preview/"' in html and 'method="get"' in html     # "退出登录"在预览里不退出


def test_unknown_path_and_bad_prefix(published):
    payload, received = published
    assert preview.render(payload, '/admin/x', '', received)['status'] == 404
    with pytest.raises(ValueError):
        preview.render(payload, '/', '', received, prefix='//evil.example')


def test_prefix_urls_leaves_external_and_protocol_relative_alone():
    html = '<a href="//x.example/a">a</a><img src="/img/a.jpg"><a href="/item/CO-0001">b</a>'
    assert preview.prefix_urls(html, '/preview') == (
        '<a href="//x.example/a">a</a><img src="/preview/img/a.jpg"><a href="/preview/item/CO-0001">b</a>')


def test_cli_reads_stdin_and_writes_json(published):
    payload, received = published
    r = subprocess.run([sys.executable, 'preview.py'], cwd=HERE, capture_output=True, timeout=60,
                       input=json.dumps({'payload': payload, 'path': '/', 'received_at': received}).encode())
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out['status'] == 200 and '今日榜' in out['body']


def test_preview_with_fee_and_fx_matches_live_page_except_local_switch(client, db, monkeypatch):
    """故事 12：带费用档位与汇率的候选快照——预览按欧元显示、费用文字照常、币种切换只在页内（无表单、不提交）。"""
    from datetime import datetime, timedelta, timezone
    now = Clock()()
    monkeypatch.setattr(preview.time, 'time', lambda: now)
    items = [make_item(n, [(put_image(client, f'fb{n}'), put_image(client, f'ft{n}'))]) for n in range(1, 13)]
    for it in items:
        it.update(fee_tier='B', fee_amount=42, fee_label='30cm 以下花瓶')
    payload = build(9, items)
    payload.update(fx_rate=7.8, fx_date=(datetime.fromtimestamp(now, timezone.utc).date() - timedelta(days=1)).isoformat())
    assert send(client, 'snapshot', payload).status_code == 200
    sign_in(client)
    with sqlite3.connect(db) as c:
        received = c.execute('SELECT received_at FROM snapshots WHERE publish_no=9').fetchone()[0]
    switch = re.compile(r'<form class="cur".*?</form>|<div class="cur".*?</div>\s*</div>', re.S)
    for path in ('/', '/item/CO-0001'):
        body = preview.render(payload, path, '', received)['body']
        live = client.get(path).text
        local = switch.search(body).group(0)
        assert 'data-local' in local and '<form' not in local and 'action=' not in local and 'type="button"' in local
        assert 'value="EUR" role="radio" aria-label="欧元" aria-checked="true"' in local     # 欧元默认
        assert '<form class="cur" method="post" action="/currency"' in live
        flat = lambda h: re.sub(r'\s+', ' ', switch.sub('', h))           # 模板分支留下的空行不算差异
        assert flat(unprefix(body)) == flat(live)
    item = preview.render(payload, '/item/CO-0001', '', received)['body']
    assert '约 €42' in item and '（B 档：30cm 以下花瓶）' in item and '¥' not in item.replace('>¥<', '')
