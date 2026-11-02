import sqlite3
from curio import market
from curio.store import Store

def test_market_shared_mark_restore_and_live_refresh(tmp_path,monkeypatch):
    monkeypatch.setattr(market,'DB',tmp_path/'curio.db')
    store=Store(tmp_path/'curio.db')
    with store.connect() as c:
        c.execute("INSERT INTO items(item_id,platform,title,description,status,agent_decision,listed_price,currency,url,first_seen) VALUES('one','ebay','Meissen Tasse','','active','want',100,'EUR','https://www.ebay.de/itm/1','2026-10-19')")
    market.init(store)
    assert market.query({})['total']==1
    market.set_decision('ebay','one',True,'无关')
    assert market.query({})['total']==0
    assert market.query({'scope':['irrelevant']})['total']==1
    assert store.page()['total']==0
    assert store.page('excluded')['total']==1
    market.set_decision('ebay','one',False,'恢复')
    assert market.query({})['total']==1 and store.page()['total']==1
    with store.connect() as c:c.execute("UPDATE items SET title='Meissen Vase',status='gone',listed_price=80 WHERE item_id='one'")
    d=market.query({'status':['delisted']})
    assert d['total']==1 and d['median']==80 and d['items'][0]['type']=='花瓶'
    legacy=tmp_path/'legacy.sqlite'
    with sqlite3.connect(legacy) as c:
        c.execute('CREATE TABLE decisions(platform,item_id,irrelevant,reason,updated_at)')
        c.execute('CREATE TABLE events(id,platform,item_id,irrelevant,reason,created_at)')
        c.execute("INSERT INTO decisions VALUES('ebay','one',1,'邮票','2027-10-19')")
        c.execute("INSERT INTO events VALUES(1,'ebay','one',1,'邮票','2027-10-19')")
    assert market.migrate_legacy(legacy)['events']==1
    assert market.migrate_legacy(legacy)['events']==0
    with store.connect() as c:
        assert c.execute('SELECT count(*) FROM items').fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM cases').fetchone()[0]==0
        assert c.execute('SELECT reason FROM market_decisions').fetchone()[0]=='邮票'

def test_market_type_chart_groups_codes_not_working_titles(tmp_path,monkeypatch):
    monkeypatch.setattr(market,'DB',tmp_path/'curio.db')
    store=Store(tmp_path/'curio.db')
    with store.connect() as c:
        for n,title in enumerate(['Meissen Tasse mit Untertasse','Meissen Mokka Gedeck','Meissen Schüssel','Meissen Schale']):
            c.execute("INSERT INTO items(item_id,platform,title,description,status,agent_decision,url,first_seen) VALUES(?,'ebay',?,'','active','want',?,'2026-10-19')",(str(n),title,'https://www.ebay.de/itm/'+str(n)))
    market.init(store)
    d=market.query({});groups={r['code']:r for r in d['types']}
    assert groups['cup_set']['count']==2
    assert groups['bowl']['count']==2
    assert groups['bowl']['label']=='碗'
    assert sum(r['count'] for r in d['types'])==d['total']
    assert market.query({'type':['cup_set']})['total']==2
