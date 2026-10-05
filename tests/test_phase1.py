import json,sqlite3,threading,urllib.request
from unittest.mock import Mock,patch
import pytest
from curio.config import load,credentials
from curio.store import Store
from curio.curio_monitor import collect_ebay,EbayAPI,CollectionError,collect_ka,check_existing
from screening.pipeline import process,NoopProvider

@pytest.fixture
def cfg():return load()
@pytest.fixture
def db(tmp_path):return Store(tmp_path/'curio.db')
def item(i='one',price=40,title='Meissen rote Rose Tasse'):
    return dict(item_id=i,platform='ebay',title=title,price=price,currency='EUR',buying='FIXED_PRICE',url='https://www.ebay.de/itm/example',image='')
def raw(i,price=40):return dict(itemId=i,title='Meissen Tasse',price={'value':str(price),'currency':'EUR'},buyingOptions=['FIXED_PRICE'])

def test_incremental_stops_known(db,cfg):
    db.ingest(item('known'),cfg);db.set('ebay_initialized','1')
    api=Mock();api.search.return_value=[raw('new'),raw('known'),raw('older')]
    assert collect_ebay(db,api,cfg)==1
    assert db.known()=={'known','new'};api.search.assert_called_once()

def test_backfill_1000(db,cfg):
    api=Mock();api.search.side_effect=lambda k,o,n:[raw(str(i)) for i in range(o,o+n)]
    assert collect_ebay(db,api,cfg)==1000
    assert api.search.call_count==5;assert db.get('ebay_initialized')=='1'

def test_skipped_retained(db,cfg):
    db.ingest(item(title='Meissen defekt Tasse'),cfg)
    row=db.page('excluded')['items'][0]
    assert row['agent_decision']=='skip' and row['agent_reason']

def test_judge_once_price_only(db,cfg):
    from curio import judge
    with patch('curio.rules.judge.judge',wraps=judge.judge) as call:
        db.ingest(item(),cfg);db.ingest(item(),cfg);db.ingest(item(price=30),cfg)
        assert call.call_count==2
    with db.connect() as c:assert c.execute('SELECT COUNT(*) FROM price_history').fetchone()[0]==2

def test_review_snapshot_idempotency_and_hide(db,cfg):
    db.ingest(item(),cfg)
    first=db.review('one','skip','太贵',20,'request1',1)
    assert db.review('one','skip','太贵',20,'request1',1)==first
    assert db.page()['total']==0
    with db.connect() as c:
        row=c.execute('SELECT * FROM cases').fetchone()
        assert row['fair_price']==20 and row['agent_reason'] and row['rule_version']==1

@pytest.mark.parametrize('decision,reason,fair',[('skip','',10),('bad','test',10),('want','','-1'),('want','','nan')])
def test_invalid_review(db,cfg,decision,reason,fair):
    db.ingest(item(),cfg)
    with pytest.raises(ValueError):db.review('one',decision,reason,fair,'r',1)

def test_stale_review(db,cfg):
    db.ingest(item(),cfg);db.ingest(item(price=20),cfg)
    with pytest.raises(ValueError):db.review('one','want','',None,'r',1)

def test_override_excluded(db,cfg):
    db.ingest(item(title='Meissen defekt'),cfg);db.review('one','want','愿意修复',10,'r',1)
    assert db.page('excluded')['items'][0]['owner_decision']=='want'
    with db.connect() as c:assert c.execute('SELECT agent_decision FROM cases').fetchone()[0]=='skip'

def test_budget_zero_no_provider_call(db):
    provider=Mock(side_effect=AssertionError('must not call'))
    result={'decision':'probe','reason':'test'}
    with db.connect() as c:assert process(c,item(),result,0,provider,provider)==result
    provider.screen.assert_not_called()

def test_noop(db):
    with db.connect() as c:assert process(c,item(),{'decision':'want'},1)=={'decision':'want'}

def test_credentials_never_persist_or_log(db,cfg,tmp_path,capsys,caplog,monkeypatch):
    secret='SENTINEL-secret-72';client='SENTINEL-client-38'
    (tmp_path/'.env').write_text(f'EBAY_CLIENT_ID={client}\nEBAY_CLIENT_SECRET={secret}\n')
    monkeypatch.setenv('EBAY_CLIENT_SECRET','wrong-env')
    assert credentials(tmp_path)==(client,secret)
    api=EbayAPI(tmp_path);api.session=Mock();api.session.request.side_effect=__import__('requests').RequestException(secret)
    with pytest.raises(CollectionError) as error:api.search('Meissen',0,200)
    db.ingest(dict(item(),access_token=secret,client_secret=secret),cfg)
    text=str(error.value)+capsys.readouterr().out+caplog.text+db.path.read_bytes().decode(errors='ignore')
    assert secret not in text and client not in text

def test_fee_and_damaged_threshold(db,cfg):
    cfg.update(auction_fee_rate=.25,cheap_damaged_max=20)
    db.ingest(dict(item(price=10,title='Meissen defekt'),buying='AUCTION'),cfg)
    r=db.page()['items'][0];assert r['total_price']==12.5 and r['agent_decision']=='probe'
    db.ingest(item('ka',10),cfg)
    with db.connect() as c:assert c.execute("SELECT total_price FROM items WHERE item_id='ka'").fetchone()[0]==10

def test_ka_pause(db,cfg):
    with patch('curio.curio_monitor.fetch_kleinanzeigen',return_value=(None,'人机验证')) as fetch:
        collect_ka(db,cfg);collect_ka(db,cfg);assert fetch.call_count==1
    assert float(db.get('ka_pause_until'))>__import__('time').time()+7100

def test_delist_and_price_refresh(db,cfg):
    db.ingest(item('one'),cfg);db.ingest(item('two'),cfg)
    api=Mock();api.detail.side_effect=[None,raw('two',20)]
    check_existing(db,api,cfg)
    with db.connect() as c:
        assert c.execute("SELECT status FROM items WHERE item_id='one'").fetchone()[0]=='gone'
        assert c.execute("SELECT listed_price FROM items WHERE item_id='two'").fetchone()[0]==20

def test_http_review(db,cfg):
    from curio.review import server
    import re
    db.ingest(item(),cfg);httpd=server(db,port=0);thread=threading.Thread(target=httpd.serve_forever,daemon=True);thread.start()
    base='http://127.0.0.1:'+str(httpd.server_port)
    try:
        html=urllib.request.urlopen(base).read().decode();token=re.search("const token='([^']+)'",html)[1]
        data=json.dumps(dict(item_id='one',decision='skip',reason='太贵',fair_price=20,request_id='http',rule_version=1)).encode()
        req=urllib.request.Request(base+'/api/review',data,headers={'Content-Type':'application/json','X-Curio-Token':token})
        assert json.load(urllib.request.urlopen(req))['case_id']==1
        assert json.load(urllib.request.urlopen(base+'/api/items'))['total']==0
    finally:httpd.shutdown();httpd.server_close()

def test_readonly_import_keeps_owner_skip(tmp_path,db,cfg):
    from scripts.import_desktop import migrate
    source=tmp_path/'legacy.db'
    c=sqlite3.connect(source)
    c.executescript("CREATE TABLE items(item_id TEXT,platform TEXT,title TEXT,price REAL,currency TEXT,first_seen TEXT,last_seen TEXT); CREATE TABLE decisions(item_id TEXT,decision TEXT,reason TEXT,fair_price TEXT,agent_decision TEXT,agent_reason TEXT,decided_at TEXT);")
    c.execute("INSERT INTO items VALUES('legacy','ebay','Meissen Rose Tasse',40,'EUR','2026-09-01','2026-09-01')")
    c.execute("INSERT INTO decisions VALUES('legacy','skip','太贵','20欧','probe','原规则','2026-09-01')");c.commit();c.close()
    before=source.read_bytes();assert migrate(source,db,cfg)['reviews']==1
    assert source.read_bytes()==before and db.page()['total']==0
    assert migrate(source,db,cfg)['reviews']==0

def test_unknown_price_not_free(db,cfg):
    from curio.curio_monitor import _ka_price
    assert _ka_price('VB') is None
    db.ingest(item(price=None),cfg)
    assert db.page()['items'][0]['listed_price'] is None

def test_api_query_contract(tmp_path):
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    api=EbayAPI(tmp_path);api.get=Mock(return_value={'itemSummaries':[]})
    api.search('Meissen',200,200)
    params=api.get.call_args.args[1]
    assert params['sort']=='newlyListed' and 'itemLocationCountry:DE' in params['filter']
    assert 'AUCTION' in params['filter'] and params['offset']==200

def test_failed_initial_backfill_resumes(db,cfg):
    api=Mock();api.search.side_effect=[[raw(str(i)) for i in range(200)],CollectionError('offline')]
    with pytest.raises(CollectionError):collect_ebay(db,api,cfg)
    assert db.get('ebay_initialized') is None
    api.search.reset_mock();api.search.side_effect=None;api.search.return_value=[]
    collect_ebay(db,api,cfg)
    assert api.search.call_args.args[1]==200

def test_ka_known_featured_does_not_hide_new(db,cfg):
    from curio.curio_monitor import collect_ka
    db.ingest(dict(item('old'),platform='kleinanzeigen'),cfg)
    rows=[dict(item('old'),platform='kleinanzeigen'),dict(item('new'),platform='kleinanzeigen')]
    with patch('curio.curio_monitor.fetch_kleinanzeigen',side_effect=[(rows,None),([rows[0]],None)]),patch('curio.curio_monitor.time.sleep'):
        assert collect_ka(db,cfg)==1
    assert 'new' in db.known()

def test_ebay_interrupted_incremental_does_not_lose_tail(db,cfg):
    db.ingest(item('old'),cfg);db.set('ebay_initialized','1')
    api=Mock();batch=[raw(str(i)) for i in range(200)]
    api.search.side_effect=[batch,CollectionError('network')]
    with pytest.raises(CollectionError):collect_ebay(db,api,cfg)
    api.search.side_effect=[batch,[raw('tail'),raw('old')]]
    assert collect_ebay(db,api,cfg)==1
    assert 'tail' in db.known() and db.get('ebay_incremental_known') is None

def test_hourly_launchd_configuration():
    from scripts.install_hourly import configuration
    data=configuration()
    assert data['StartInterval']==3600
    assert data['ProgramArguments'][-1]=='curio.curio_monitor'
    assert data['RunAtLoad'] is False

@pytest.mark.parametrize('title',['Baugrundstück in Meißen','Terrassenplatten Meissen Beton 2.0','KPM Berlin Gutschein','Meissen Wohnung zu vermieten','Meissen Job Vollzeit'])
def test_irrelevant_hidden_but_retained(db,cfg,title):
    db.ingest(item(title=title),cfg)
    assert db.page()['total']==0
    row=db.page('excluded')['items'][0]
    assert row['agent_decision']=='skip' and '硬过滤' in row['agent_reason']

def test_damaged_exception_cannot_rescue_job(db,cfg):
    cfg['cheap_damaged_max']=100
    db.ingest(item(title='Meissen Job defekt',price=1),cfg)
    assert db.page()['total']==0
