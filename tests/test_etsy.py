"""故事 2：Etsy 采集与在售检查（移植自 legacy，修掉 README 列出的坑）。"""
import json,shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
import requests
from curio.config import load,etsy_credentials
from curio.errors import CollectionError
from curio.curio_monitor import check_existing
from curio.etsy import EtsyAPI,collect_etsy,parse,judge_listings
from curio.schedule import BERLIN,Budget
from curio.store import Store

ROOT=Path(__file__).resolve().parents[1]
NOON=datetime(2026,11,2,12,tzinfo=BERLIN).timestamp()

@pytest.fixture
def cfg():return dict(load(),keywords=['Meissen'])
@pytest.fixture
def db(tmp_path):return Store(tmp_path/'curio.db')

def listing(lid,price=4000,state='active',quantity=1,title='Meissen Tasse Rose'):
    return {'listing_id':lid,'title':title,'description':'no chips','state':state,'quantity':quantity,
            'price':{'amount':price,'divisor':100,'currency_code':'EUR'},'url':f'https://www.etsy.com/listing/{lid}',
            'original_creation_timestamp':1793000000,'images':[{'rank':1,'url_fullxfull':f'https://img/{lid}.jpg'}]}

class FakeEtsy:
    """按路径返回预置结果；batch 第 fail_batch 次调用（从 1 起）抛错。"""
    def __init__(self,active=(),details=None,fail_batch=None):
        self.active=list(active);self.details=details or {};self.fail_batch=fail_batch
        self.calls=[];self.batches=0;self.purpose='fetch'
    def get(self,path,params):
        self.calls.append((path,dict(params)))
        if path=='/listings/active':
            o,n=params['offset'],params['limit'];return {'results':[dict(l,images=None) for l in self.active[o:o+n]]}
        self.batches+=1
        if self.batches==self.fail_batch:raise CollectionError('Etsy 网络请求失败')
        ids=params['listing_ids'].split(',')
        return {'results':[self.details[i] for i in ids if i in self.details]}

def test_parse_shape():
    row=parse(listing(7,price=12345),'Meissen')
    assert row['item_id']=='etsy:7' and row['platform']=='etsy' and row['price']==123.45 and row['currency']=='EUR'
    assert row['image']=='https://img/7.jpg' and row['created'].endswith('+00:00') and row['description']=='no chips'
    assert parse(dict(listing(8),price=None),'')['price'] is None     # 未知价格不当作 0

def test_collect_ingests_and_dedups(db,cfg):
    items=[listing(i) for i in (3,2,1)]
    api=FakeEtsy(items,{str(l['listing_id']):l for l in items})
    assert collect_etsy(db,api,cfg)==3
    assert {'etsy:1','etsy:2','etsy:3'}<=db.known()
    with db.connect() as c:
        r=c.execute("SELECT platform,image FROM items WHERE item_id='etsy:3'").fetchone()
        assert r['platform']=='etsy' and r['image']=='https://img/3.jpg'
    api.calls.clear()
    assert collect_etsy(db,api,cfg)==0                      # 一页全已知 → 停，不补图
    assert [p for p,_ in api.calls]==['/listings/active']
    with db.connect() as c:assert c.execute("SELECT count(*) FROM items WHERE platform='etsy'").fetchone()[0]==3

def test_collect_pages_until_page_all_known(db,cfg):
    items=[listing(i) for i in range(250,0,-1)]
    api=FakeEtsy(items,{str(l['listing_id']):l for l in items})
    assert collect_etsy(db,api,cfg)==250
    assert [p['offset'] for path,p in api.calls if path=='/listings/active']==[0,100,200]

def test_collect_image_failure_retries_next_round(db,cfg):
    items=[listing(1)]
    api=FakeEtsy(items,{'1':items[0]},fail_batch=1)
    with pytest.raises(CollectionError):collect_etsy(db,api,cfg)
    assert 'etsy:1' not in db.known()
    assert collect_etsy(db,api,cfg)==1

def test_price_change_recorded(db,cfg):
    api=FakeEtsy([listing(1,4000)],{'1':listing(1,4000)});collect_etsy(db,api,cfg)
    db.ingest(parse(listing(1,3000),''),cfg)
    with db.connect() as c:
        assert [r[0] for r in c.execute("SELECT price FROM price_history WHERE item_id='etsy:1' ORDER BY rowid")]==[40.0,30.0]
        assert c.execute("SELECT count(*) FROM items WHERE item_id='etsy:1'").fetchone()[0]==1

@pytest.mark.parametrize('detail,outcome',[
    (listing(1,quantity=0),'gone'),                 # 售罄
    (listing(1,quantity=None),'active'),            # 库存未知 ≠ 0
    (listing(1,quantity=2),'active'),
    (listing(1,state='sold_out'),'gone'),
    (listing(1,state='inactive'),'gone'),
    ({k:v for k,v in listing(1).items() if k!='state'},'unknown')])   # 没有 state：保持原状态
def test_judge_quantity_and_state(detail,outcome):
    verdicts,error=judge_listings(FakeEtsy(details={'1':detail}),['etsy:1'])
    assert error is None and verdicts['etsy:1'][0]==outcome

def seed(db,cfg,n):
    for i in range(1,n+1):db.ingest(parse(listing(i),'Meissen'),cfg)

def test_check_partial_batch_failure_keeps_rest(db,cfg):
    seed(db,cfg,150)
    details={str(i):listing(i,quantity=0 if i%2 else 1) for i in range(1,151)}
    etsy=FakeEtsy(details=details,fail_batch=2)
    summary=check_existing(db,None,dict(cfg,kleinanzeigen_enabled=False),None,etsy)
    assert etsy.batches==2 and summary['attempted']==100 and summary['etsy_error']
    assert summary['gone']+summary['active']==100 and summary['errors']==0
    with db.connect() as c:
        status=dict(c.execute("SELECT item_id,status FROM items WHERE platform='etsy'").fetchall())
        events=dict(c.execute("SELECT item_id,outcome FROM status_check_events").fetchall())
    # 奇数编号 quantity 0 → gone；哪 100 件落在第一批取决于轮换顺序，所以按编号核对。
    assert all(o==('gone' if int(i.split(':')[1])%2 else 'active') for i,o in events.items())
    assert len(events)==100                                   # 失败批次的货不写检查记录，保持轮换位置
    for iid,state in status.items():
        if iid not in events:assert state=='active'           # 未取到详情：保持原状态
        else:assert state==('gone' if events[iid]=='gone' else 'active')

def test_check_sold_out_marked_gone(db,cfg):
    seed(db,cfg,1)
    summary=check_existing(db,None,dict(cfg,kleinanzeigen_enabled=False),None,FakeEtsy(details={'1':listing(1,quantity=0)}))
    assert summary['gone']==1
    with db.connect() as c:assert c.execute("SELECT status FROM items WHERE item_id='etsy:1'").fetchone()[0]=='gone'

def test_check_etsy_budget_exhausted(db,cfg):
    seed(db,cfg,2)
    b=Budget(db,{'ebay':10,'etsy':0,'kleinanzeigen':10},lambda:NOON)
    etsy=FakeEtsy(details={})
    summary=check_existing(db,None,dict(cfg,kleinanzeigen_enabled=False),b,etsy)
    assert summary['budget_exhausted']==['etsy'] and etsy.calls==[]

def response(code,data=None):
    r=Mock(status_code=code);r.json.return_value=data or {};return r

def test_api_counts_every_request_incl_retries_and_header_fallback(db):
    b=Budget(db,{'ebay':10,'etsy':100,'kleinanzeigen':10},lambda:NOON)
    session=Mock();session.get.side_effect=[response(403),response(429),response(200,{'results':[]})]
    api=EtsyAPI('KEY','SECRET',b,session)
    with patch('curio.etsy.time.sleep'):assert api.get('/listings/active',{})=={'results':[]}
    headers=[c.kwargs['headers']['x-api-key'] for c in session.get.call_args_list]
    assert headers==['KEY:SECRET','KEY','KEY'] and b.snapshot()['etsy']['fetch']==3
    session.get.side_effect=[response(200,{})];api.get('/listings/active',{})
    assert session.get.call_args.kwargs['headers']['x-api-key']=='KEY'   # 记住能用的写法

def test_api_errors_never_leak_keys(db):
    session=Mock();session.get.side_effect=requests.RequestException('KEY-SENTINEL SECRET-SENTINEL')
    api=EtsyAPI('KEY-SENTINEL','SECRET-SENTINEL',None,session)
    with pytest.raises(CollectionError) as error:api.get('/listings/active',{})
    assert 'SENTINEL' not in str(error.value)
    session.get.side_effect=[response(401),response(401)]
    with pytest.raises(CollectionError) as error:api.get('/listings/active',{})
    assert 'SENTINEL' not in str(error.value) and '鉴权失败' in str(error.value)

def test_credentials_only_from_dotenv(tmp_path,monkeypatch):
    monkeypatch.setenv('ETSY_API_KEY','from-env')
    assert etsy_credentials(tmp_path) is None                  # 没有 .env
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=x\nETSY_API_KEY=\n')
    assert etsy_credentials(tmp_path) is None                  # 空值视为未启用
    (tmp_path/'.env').write_text('ETSY_API_KEY="k1"\nETSY_SHARED_SECRET=s1\n')
    assert etsy_credentials(tmp_path)==('k1','s1')

def test_run_etsy_disabled_without_key(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT/'config',tmp_path/'config')
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    with patch('curio.curio_monitor.collect_ebay',return_value=0),\
         patch('curio.curio_monitor.collect_ka',side_effect=lambda s,c,b=None:s.set('ka_status','ok')),\
         patch('curio.curio_monitor.check_existing',return_value={'errors':0}),\
         patch('curio.etsy.collect_etsy') as collect:
        result=run(tmp_path,lambda:NOON)
    assert result['etsy_status']=='disabled' and not collect.called

def test_run_etsy_enabled_ingests(tmp_path):
    from curio.curio_monitor import run
    shutil.copytree(ROOT/'config',tmp_path/'config')
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\nETSY_API_KEY=k\nETSY_SHARED_SECRET=s\n')
    items=[listing(11),listing(12)]
    def fake_get(self,path,params):
        self.budget.spend('etsy',self.purpose)
        return FakeEtsy(items,{str(l['listing_id']):l for l in items}).get(path,params)
    with patch('curio.curio_monitor.collect_ebay',return_value=0),\
         patch('curio.curio_monitor.collect_ka',side_effect=lambda s,c,b=None:s.set('ka_status','ok')),\
         patch('curio.curio_monitor.check_existing',return_value={'errors':0}),\
         patch('curio.etsy.EtsyAPI.get',fake_get),patch('curio.etsy.time.sleep'):
        result=run(tmp_path,lambda:NOON)
    assert result['etsy_status']=='ok' and result['etsy_new']==2
    # 第 1 个关键词：搜索 + 补图；第 2 个关键词搜到的都已入库，只搜索不补图。
    assert result['api_usage']['etsy']['fetch']==3
    assert {'etsy:11','etsy:12'}<=Store(tmp_path/'data/curio.db').known()

def test_requests_spaced_at_least_200ms(db):
    session=Mock();session.get.return_value=response(200,{})
    api=EtsyAPI('k','s',None,session);clock=[100.0];slept=[]
    def sleep(s):slept.append(s);clock[0]+=s
    with patch('curio.etsy.time.monotonic',side_effect=lambda:clock[0]),patch('curio.etsy.time.sleep',side_effect=sleep):
        api.get('/a',{});clock[0]+=0.05;api.get('/a',{});clock[0]+=1;api.get('/a',{})
    assert slept==[pytest.approx(0.15)]

@pytest.mark.parametrize('etsy_env',['', 'ETSY_API_KEY=k\nETSY_SHARED_SECRET=s\n'])
def test_etsy_disabled_or_403_does_not_block_others(tmp_path,etsy_env):
    """验收：Etsy 未启用或返回 403 时 etsy_status 为 disabled/error，eBay 与 Kleinanzeigen 照常完成。"""
    from curio.curio_monitor import run,parse as ebay_parse
    shutil.copytree(ROOT/'config',tmp_path/'config')
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n'+etsy_env)
    def ebay(store,api,config):
        store.ingest(ebay_parse({'itemId':'v1|1|0','title':'Meissen Tasse','price':{'value':'40','currency':'EUR'}},'Meissen'),config)
    def ka(store,config,budget=None):
        store.ingest({'item_id':'ka:1','platform':'kleinanzeigen','title':'KPM Berlin Teller','price':30,'currency':'EUR'},config)
        store.set('ka_status','ok')
    session=Mock();session.get.return_value=response(403)
    with patch('curio.curio_monitor.collect_ebay',side_effect=ebay),patch('curio.curio_monitor.collect_ka',side_effect=ka),\
         patch('curio.curio_monitor.check_existing',return_value={'errors':0}),\
         patch('curio.etsy.requests.Session',return_value=session),patch('curio.etsy.time.sleep'):
        result=run(tmp_path,lambda:NOON)
    assert result['etsy_status']==('error' if etsy_env else 'disabled')
    # 两种 key 写法各试一次后即停（鉴权失败对所有关键词一样），并进入 1 小时退避。
    if etsy_env:assert result['etsy']=='Etsy 鉴权失败，1 小时后重试' and result['api_usage']['etsy']['fetch']==2
    assert result['ebay_status']=='ok' and result['kleinanzeigen_status']=='ok'
    assert result['ebay_new']==1 and result['kleinanzeigen_new']==1

def test_placeholder_secret_skipped(db):
    session=Mock();session.get.return_value=response(200,{'results':[]})
    api=EtsyAPI('KEY','待填 secret',None,session)
    api.get('/listings/active',{})
    assert [c.kwargs['headers']['x-api-key'] for c in session.get.call_args_list]==['KEY']
    with pytest.raises(CollectionError) as error:EtsyAPI('钥匙 x','',None,session).get('/a',{})
    assert '格式无效' in str(error.value)

def test_judge_quantity_zero_evidence():
    verdicts,_=judge_listings(FakeEtsy(details={'1':listing(1,quantity=0)}),['etsy:1'])
    assert verdicts['etsy:1'][1]=='Etsy quantity 0; unavailable, sale unknown'

def test_check_missing_state_kept(db,cfg):
    seed(db,cfg,1)
    summary=check_existing(db,None,dict(cfg,kleinanzeigen_enabled=False),None,FakeEtsy(details={'1':{'listing_id':1,'quantity':0}}))
    assert summary['unknown']==1 and summary['gone']==0
    with db.connect() as c:assert c.execute("SELECT status FROM items WHERE item_id='etsy:1'").fetchone()[0]=='active'

def test_check_active_new_price_updates(db,cfg):
    seed(db,cfg,1)
    check_existing(db,None,dict(cfg,kleinanzeigen_enabled=False),None,FakeEtsy(details={'1':listing(1,price=2500)}))
    with db.connect() as c:
        assert c.execute("SELECT listed_price FROM items WHERE item_id='etsy:1'").fetchone()[0]==25.0
        assert [r[0] for r in c.execute("SELECT price FROM price_history WHERE item_id='etsy:1' ORDER BY rowid")]==[40.0,25.0]

def test_non_object_json_is_collection_error():
    session=Mock();session.get.return_value=response(200,['not','a','dict'])
    with pytest.raises(CollectionError) as error:EtsyAPI('k','s',None,session).get('/a',{})
    assert '响应无效' in str(error.value)

def test_one_keyword_failure_does_not_stop_next(db):
    cfg=dict(load(),keywords=['Meissen','KPM Berlin'])
    items=[listing(5)]
    class Api(FakeEtsy):
        def get(self,path,params):
            if path=='/listings/active' and params['keywords']=='Meissen':raise CollectionError('Etsy 网络请求失败')
            return super().get(path,params)
    with pytest.raises(CollectionError) as error:collect_etsy(db,Api(items,{'5':items[0]}),cfg)
    assert 'Meissen' in str(error.value) and 'etsy:5' in db.known()

def test_unexpected_etsy_check_error_does_not_stop_other_platforms(db,cfg):
    seed(db,cfg,1)
    db.ingest({'item_id':'e1','platform':'ebay','title':'Meissen Tasse','price':40,'currency':'EUR'},cfg)
    etsy=Mock();etsy.get.side_effect=RuntimeError('boom')
    api=Mock();api.detail.return_value=None
    summary=check_existing(db,api,dict(cfg,kleinanzeigen_enabled=False),None,etsy)
    assert summary['etsy_error'] and summary['gone']==1 and api.detail.called

def test_collection_error_is_single_class_under_module_entry():
    """launchd 以 `python -m curio.curio_monitor` 运行，模块代码在新命名空间里执行；
    其中的 CollectionError 必须与 etsy 抛出的是同一个类，run() 的 except 才接得住。"""
    import subprocess,sys
    probe=("import runpy,curio.etsy as e;"
           "ns=runpy.run_module('curio.curio_monitor',run_name='probe');"
           "assert ns['CollectionError'] is e.CollectionError")
    subprocess.run([sys.executable,'-c',probe],cwd=ROOT,check=True)

def test_auth_failure_backs_off_one_hour_then_clears(tmp_path):
    from curio.curio_monitor import run
    from curio.etsy import AUTH_BACKOFF_KEY
    import time as real_time
    shutil.copytree(ROOT/'config',tmp_path/'config')
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\nETSY_API_KEY=k\nETSY_SHARED_SECRET=s\n')
    session=Mock();session.get.return_value=response(403)
    clock=[NOON]
    def once():
        with patch('curio.curio_monitor.collect_ebay',return_value=0),\
             patch('curio.curio_monitor.collect_ka',side_effect=lambda s,c,b=None:s.set('ka_status','ok')),\
             patch('curio.etsy.requests.Session',return_value=session),patch('curio.etsy.time.sleep'):
            return run(tmp_path,lambda:clock[0])
    store=Store(tmp_path/'data/curio.db')
    first=once()
    assert first['etsy_status']=='error' and session.get.call_count==2      # 403 × 两种写法，检查也不再发
    assert float(store.get(AUTH_BACKOFF_KEY))>real_time.time()+3500
    store.set('schedule_last_fetch_at',0);store.set('schedule_last_status_check_at',0)
    second=once()
    assert second['etsy_status']=='error' and second['etsy']=='Etsy 鉴权失败，1 小时后重试'
    assert session.get.call_count==2 and second['api_usage']['etsy']['used']==2  # 退避期间零请求
    store.set(AUTH_BACKOFF_KEY,real_time.time()-1)
    session.get.return_value=response(200,{'results':[]})
    store.set('schedule_last_fetch_at',0);store.set('schedule_last_status_check_at',0)
    third=once()
    assert third['etsy_status']=='ok' and store.get(AUTH_BACKOFF_KEY) is None
