"""故事 13：心跳与"部分货品状态可能未更新"提示（AD-10）。TestClient，不连任何外部主机。"""
import datetime
import sqlite3
import uuid

import pytest
from fastapi.testclient import TestClient

import app as server
import preview
import snapshot
from test_snapshot import AUTH, SAME_ORIGIN, TOKEN, Clock, hide, publish, send, sign_in

STALE = '部分货品状态可能未更新，下单前请先问墨墨。'


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def db(tmp_path):
    return tmp_path / 'server.db'


@pytest.fixture
def client(db, clock):
    return TestClient(server.create_app(db_path=db, admin_token=TOKEN, clock=clock, cookie_secure=False,
                                        public_url='https://curio.test'),
                      follow_redirects=False, headers=SAME_ORIGIN)


def z(ts):
    return datetime.datetime.fromtimestamp(ts, datetime.timezone.utc).isoformat(
        timespec='milliseconds').replace('+00:00', 'Z')


def beat(client, sent, ebay=None, etsy=None):
    """ebay / etsy：距 sent 多少秒前成功（None：不带时间）。"""
    platforms = []
    for name, ago in (('ebay', ebay), ('etsy', etsy)):
        entry = {'platform': name}
        if ago is not None:
            entry['last_success_at'] = z(sent - ago)
        platforms.append(entry)
    return send(client, 'heartbeat', {'schema_version': 1, 'sent_at': z(sent), 'platforms': platforms})


def pages(client, code='CO-0001'):
    return client.get('/').text, client.get('/item/' + code).text


def test_never_published_shows_no_notice(client, clock):
    sign_in(client)
    home = client.get('/').text
    assert '墨墨正在准备' in home and STALE not in home


def test_published_without_heartbeat_shows_notice(client):
    publish(client, 1, n_items=12)
    sign_in(client)
    home, item = pages(client)
    assert STALE in home and STALE in item
    assert 'class="im stale" data-stale-note' in home


def test_fresh_heartbeat_no_notice(client, clock):
    publish(client, 1, n_items=12)
    sign_in(client)
    r = beat(client, clock.now, ebay=20 * 60, etsy=5 * 60)
    assert r.status_code == 200 and r.json()['applied'] is True
    home, item = pages(client)
    assert STALE not in home and STALE not in item


def test_two_hours_five_minutes_shows_notice_on_home_and_item(client, clock):
    publish(client, 1, n_items=12)
    sign_in(client)
    beat(client, clock.now, ebay=2 * 3600 + 300, etsy=60)
    home, item = pages(client)
    assert STALE in home and STALE in item
    # 下架后的单件页（已售出）也提示。
    hide(client, 'CO-0002', 5)
    assert STALE in client.get('/item/CO-0002').text


def test_age_grows_with_server_time_since_last_heartbeat(client, clock):
    publish(client, 1, n_items=12)
    sign_in(client)
    beat(client, clock.now, ebay=60 * 60, etsy=60 * 60)
    assert STALE not in client.get('/').text
    clock.now += 61 * 60                          # Mac 之后再没发心跳（关机、断网）
    assert STALE in client.get('/').text


def test_platform_without_success_time_is_stale(client, clock):
    publish(client, 1, n_items=12)
    sign_in(client)
    beat(client, clock.now, ebay=None, etsy=60)
    assert STALE in client.get('/').text


def test_mac_clock_skew_does_not_matter(client, clock):
    """Mac 时钟快了 3 小时：只用 Mac 自己的两个时间之差 + 服务器收到后经过的时间。"""
    publish(client, 1, n_items=12)
    sign_in(client)
    beat(client, clock.now + 3 * 3600, ebay=10 * 60, etsy=10 * 60)
    assert STALE not in client.get('/').text


def test_late_older_heartbeat_does_not_overwrite(client, clock, db):
    publish(client, 1, n_items=12)
    sign_in(client)
    assert beat(client, clock.now, ebay=60, etsy=60).json()['applied'] is True
    late = beat(client, clock.now - 600, ebay=3 * 3600, etsy=60)
    assert late.status_code == 200 and late.json()['applied'] is False
    assert STALE not in client.get('/').text
    with sqlite3.connect(db) as c:
        assert c.execute("SELECT count(*) FROM events WHERE kind='heartbeat'").fetchone()[0] == 0


@pytest.mark.parametrize('payload', [
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z', 'platforms': []},
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z', 'platforms': [{'platform': 'kleinanzeigen'}]},
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z',
     'platforms': [{'platform': 'ebay'}, {'platform': 'ebay'}]},
    {'schema_version': 1, 'sent_at': '2026-11-03 08:00', 'platforms': [{'platform': 'ebay'}]},
    {'schema_version': 1, 'sent_at': '2026-13-03T08:00:00.000Z', 'platforms': [{'platform': 'ebay'}]},
    {'schema_version': 2, 'sent_at': '2026-11-03T08:00:00.000Z', 'platforms': [{'platform': 'ebay'}]},
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z', 'platforms': [{'platform': 'ebay'}]},   # 缺 etsy
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z',
     'platforms': [{'platform': 'ebay'}, {'platform': 'etsy'}, {'platform': 'etsy'}]},
    {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z',                                      # 成功晚于发送
     'platforms': [{'platform': 'ebay', 'last_success_at': '2026-11-03T08:00:00.001Z'}, {'platform': 'etsy'}]},
])
def test_bad_heartbeat_rejected(client, payload):
    assert send(client, 'heartbeat', payload).status_code == 400


def test_heartbeat_needs_admin_token(client):
    env = {'schema_version': 1, 'event_id': str(uuid.uuid4()), 'kind': 'heartbeat',
           'created_at': '2026-11-03T08:00:00.000Z',
           'payload': {'schema_version': 1, 'sent_at': '2026-11-03T08:00:00.000Z', 'platforms': [{'platform': 'ebay'}]}}
    assert client.post('/admin/events', json=env).status_code == 401


def test_stale_rule_directly(db, clock):
    server.init_db(db)
    c = sqlite3.connect(db)
    try:
        assert snapshot.heartbeat_stale(c, clock.now) is False          # 从未发布
        c.execute("INSERT INTO snapshots(publish_no,watermark,payload,received_at) "
                  "VALUES(1,1,'{}','2026-11-03T08:00:00.000Z')")
        assert snapshot.heartbeat_stale(c, clock.now) is True           # 从未收到心跳
        snapshot.apply_heartbeat(c, {'schema_version': 1, 'sent_at': z(clock.now), 'platforms': [
            {'platform': 'ebay', 'last_success_at': z(clock.now - 7199)},
            {'platform': 'etsy', 'last_success_at': z(clock.now)}]}, z(clock.now), clock.now)
        assert snapshot.heartbeat_stale(c, clock.now) is False
        assert snapshot.heartbeat_stale(c, clock.now + 2) is True
    finally:
        c.close()


def test_future_stored_heartbeat_replaced_after_clock_correction(client, clock):
    """Mac 时钟曾快 3 小时、之后校正：已存的 sent_at 在服务器"未来"，后到的较早心跳照样生效。"""
    publish(client, 1, n_items=12)
    sign_in(client)
    assert beat(client, clock.now + 3 * 3600, ebay=60, etsy=60).json()['applied'] is True
    clock.now += 3 * 3600
    r = beat(client, clock.now - 3600, ebay=3 * 3600, etsy=60)     # 较早的 sent_at，但已存的那份不在未来了
    assert r.json()['applied'] is False
    clock.now -= 3 * 3600
    late = beat(client, clock.now, ebay=3 * 3600, etsy=60)
    assert late.json()['applied'] is True and STALE in client.get('/').text


def test_preview_never_shows_notice():
    payload = {'schema_version': 1, 'publish_no': 0, 'hide_watermark': 0, 'rank': [], 'pool': [], 'items': [],
               'tombstones': []}
    assert STALE not in preview.render(payload, '/')['body']
