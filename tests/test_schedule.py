"""故事 2：调度门、间隔伸缩、夜间放慢、每日预算（AD-27、NFR-2）。"""
import json,shutil
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
import pytest
import yaml
from curio.config import load
from curio.store import Store
from curio.schedule import (BERLIN,Budget,BudgetExhausted,Scheduler,effective_interval,next_base_interval,
                            KV_BASE,KV_LAST_FETCH)

ROOT=Path(__file__).resolve().parents[1]

def at(day,hour,minute=0):return datetime(2026,11,day,hour,minute,tzinfo=BERLIN).timestamp()

class Clock:
    def __init__(self,ts):self.ts=ts
    def __call__(self):return self.ts

@pytest.fixture
def db(tmp_path):return Store(tmp_path/'curio.db')

@pytest.mark.parametrize('base,new,expected',[(20,12,10),(20,10,10),(50,0,60),(20,0,30),(20,3,20),(6,50,5),(60,1,60)])
def test_interval_scaling_and_clamp(base,new,expected):
    assert next_base_interval(base,new)==expected

def test_night_doubles_but_caps():
    assert effective_interval(20,at(2,3))==40          # 柏林 03:00
    assert effective_interval(20,at(2,12))==20
    assert effective_interval(40,at(2,1))==60          # 夜间仍不超过 60
    assert effective_interval(20,at(2,6,59))==40 and effective_interval(20,at(2,7))==20
    assert effective_interval(20,at(2,0,59))==20

def test_scheduler_due_and_record(db):
    clock=Clock(at(2,12));s=Scheduler(db,clock)
    assert s.fetch_due() and s.check_due()                 # 首次运行
    db.set(KV_BASE,20);s.mark_fetch_started();s.mark_check_started()
    clock.ts+=19*60;assert not s.fetch_due()
    clock.ts+=60;assert s.fetch_due() and not s.check_due()
    assert s.record_fetch(12)==10 and s.current_interval()==10
    clock.ts=at(2,12)+30*60;assert s.check_due()

def test_scheduler_night_uses_effective_interval(db):
    clock=Clock(at(2,3));s=Scheduler(db,clock)
    db.set(KV_BASE,20);s.mark_fetch_started()
    clock.ts+=30*60;assert not s.fetch_due()
    clock.ts+=10*60;assert s.fetch_due()
    assert s.summary()['fetch_interval_min']==40 and s.summary()['night_mode'] is True

def test_budget_split_and_exhaustion(db):
    b=Budget(db,{'ebay':10,'etsy':0,'kleinanzeigen':5},Clock(at(2,12)))
    for _ in range(6):b.spend('ebay','fetch')
    assert not b.available('ebay','fetch')                 # 新货最多 60%
    with pytest.raises(BudgetExhausted):b.spend('ebay','fetch')
    for _ in range(4):b.spend('ebay','check')              # 在售检查保底 40%
    assert not b.available('ebay','check')
    with pytest.raises(BudgetExhausted):b.spend('ebay','check')
    assert not b.available('etsy','check')
    snap=b.snapshot();assert snap['ebay']=={'used':10,'fetch':6,'check':4,'limit':10,'fetch_cap':6}

def test_check_may_use_fetch_leftover(db):
    b=Budget(db,{'ebay':10,'etsy':10,'kleinanzeigen':10},Clock(at(2,12)))
    for _ in range(10):b.spend('ebay','check')
    assert not b.available('ebay','check') and not b.available('ebay','fetch')

def test_budget_resets_on_berlin_midnight(db):
    clock=Clock(at(2,23,59));b=Budget(db,{'ebay':10,'etsy':10,'kleinanzeigen':10},clock)
    for _ in range(6):b.spend('ebay','fetch')
    assert not b.available('ebay','fetch')
    clock.ts=at(3,0,0)
    assert b.available('ebay','fetch') and b.snapshot()['ebay']['used']==0 and b.snapshot()['date']=='2026-11-03'

@pytest.mark.parametrize('budget',[{'ebay':-1},{'ebay':1.5},{'amazon':10},'lots'])
def test_budget_config_validation(tmp_path,budget):
    shutil.copytree(ROOT/'config',tmp_path/'config')
    settings=yaml.safe_load((tmp_path/'config/settings.yaml').read_text());settings['api_budget']=budget
    (tmp_path/'config/settings.yaml').write_text(yaml.safe_dump(settings))
    with pytest.raises(ValueError):load(tmp_path)

def test_budget_config_defaults(tmp_path):
    shutil.copytree(ROOT/'config',tmp_path/'config')
    settings=yaml.safe_load((tmp_path/'config/settings.yaml').read_text());settings['api_budget']={'ebay':100}
    (tmp_path/'config/settings.yaml').write_text(yaml.safe_dump(settings))
    assert load(tmp_path)['api_budget']=={'ebay':100,'etsy':4500,'kleinanzeigen':1500}

# ---- run() 端到端：外部采集全部模拟 ----

@pytest.fixture
def root(tmp_path):
    shutil.copytree(ROOT/'config',tmp_path/'config')
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    return tmp_path

def runs(root):
    with Store(root/'data/curio.db').connect() as c:
        if not c.execute("SELECT 1 FROM sqlite_master WHERE name='collection_runs'").fetchone():return []
        return [json.loads(r[0]) for r in c.execute('SELECT result FROM collection_runs ORDER BY id')]

def fake_ka(store,config,budget=None):
    store.set('ka_status','ok');return 0

def run_with(root,clock,ebay=None,check=None):
    from curio.curio_monitor import run
    with patch('curio.curio_monitor.collect_ebay',ebay or Mock(return_value=0)) as e,\
         patch('curio.curio_monitor.collect_ka',side_effect=fake_ka) as k,\
         patch('curio.curio_monitor.check_existing',check or Mock(return_value={'attempted':0,'errors':0})) as c:
        return run(root,clock),e,k,c

def test_second_wake_not_due_adds_no_run(root):
    clock=Clock(at(2,12))
    first,*_=run_with(root,clock)
    assert first['fetch_ran'] and first['status_check_ran']
    clock.ts+=5*60
    second,e,k,c=run_with(root,clock)
    assert second['status']=='idle' and not e.called and not k.called and not c.called
    assert len(runs(root))==1

def test_check_only_round(root):
    clock=Clock(at(2,12));run_with(root,clock)
    store=Store(root/'data/curio.db');store.set(KV_BASE,60)
    clock.ts+=30*60
    result,e,k,c=run_with(root,clock)
    assert not result['fetch_ran'] and result['status_check_ran'] and c.called and not e.called
    assert result['ebay_status']=='not_due' and result['etsy_status']=='disabled'
    assert len(runs(root))==2

def test_result_has_status_usage_and_interval(root):
    def ebay(store,api,config):
        api.budget.spend('ebay','fetch');api.budget.spend('ebay','fetch');return 0
    result,*_=run_with(root,Clock(at(2,12)),ebay=ebay)
    assert result['ebay_status']=='ok' and result['kleinanzeigen_status']=='ok' and result['etsy_status']=='disabled'
    assert result['api_usage']['ebay']['used']==2 and result['api_usage']['ebay']['limit']==4500
    assert result['fetch_interval_min']==45            # 默认 30 分钟，上一轮 0 件 → ×1.5
    assert 'next_fetch_at' in result
    assert runs(root)[-1]==result

def test_ebay_budget_exhausted_others_continue(root):
    clock=Clock(at(2,12))
    store=Store(root/'data/curio.db')
    store.set('api_usage',json.dumps({'date':'2026-11-02','ebay':{'fetch':2700,'check':1800}}))
    result,e,k,c=run_with(root,clock)
    assert result['ebay_status']=='budget_exhausted' and not e.called
    assert k.called and result['kleinanzeigen_status']=='ok' and c.called

def test_new_items_shorten_interval(root):
    from curio.curio_monitor import parse
    def ebay(store,api,config):
        for i in range(12):store.ingest(parse({'itemId':f'n{i}','title':'Meissen Tasse','price':{'value':'40','currency':'EUR'}},'Meissen'),config)
    store=Store(root/'data/curio.db');store.set(KV_BASE,20)
    result,*_=run_with(root,Clock(at(2,12)),ebay=ebay)
    assert result['ebay_new']==12 and result['new_total']==12 and result['fetch_interval_min']==10

def test_lock_prevents_parallel_rounds(root):
    from curio.curio_monitor import run
    store=Store(root/'data/curio.db');store.set('collect_lock_until',9e12)
    assert run(root,Clock(at(2,12)))=={'status':'already_running'}
    assert runs(root)==[]

def test_ebay_counts_oauth_and_get(tmp_path,db):
    from curio.curio_monitor import EbayAPI
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    b=Budget(db,{'ebay':10,'etsy':10,'kleinanzeigen':10},Clock(at(2,12)))
    api=EbayAPI(tmp_path,b);api.session=Mock()
    api.session.request.side_effect=[Mock(status_code=200,json=lambda:{'access_token':'t'}),Mock(status_code=200,json=lambda:{'itemSummaries':[]})]
    api.search('Meissen',0,200)
    assert b.snapshot()['ebay']['fetch']==2

def test_ebay_exhausted_sends_no_request(tmp_path,db):
    from curio.curio_monitor import EbayAPI
    (tmp_path/'.env').write_text('EBAY_CLIENT_ID=dummy\nEBAY_CLIENT_SECRET=dummy\n')
    b=Budget(db,{'ebay':0,'etsy':10,'kleinanzeigen':10},Clock(at(2,12)))
    api=EbayAPI(tmp_path,b);api.session=Mock()
    with pytest.raises(BudgetExhausted):api.search('Meissen',0,200)
    api.session.request.assert_not_called()

def test_ka_pages_counted_and_stop_when_exhausted(db):
    from curio.curio_monitor import collect_ka
    cfg=dict(load(),keywords=['Meissen'])
    b=Budget(db,{'ebay':10,'etsy':10,'kleinanzeigen':5},Clock(at(2,12)))   # 新货上限 3 页
    page=lambda k,p:([{'item_id':f'ka:{p}{i}','platform':'kleinanzeigen','title':'Meissen Tasse','price':10,'currency':'EUR'} for i in range(3)],None)
    with patch('curio.curio_monitor.fetch_kleinanzeigen',side_effect=page) as fetch,patch('curio.curio_monitor.time.sleep'):
        assert collect_ka(db,cfg,b)==9
    assert fetch.call_count==3 and db.get('ka_status')=='budget_exhausted'
    assert db.get('ka_incremental_known') is not None     # 保留增量边界，明天接着抓

def test_ka_block_pause_unchanged_with_budget(db):
    from curio.curio_monitor import collect_ka
    cfg=dict(load(),keywords=['Meissen'])
    b=Budget(db,{'ebay':10,'etsy':10,'kleinanzeigen':100},Clock(at(2,12)))
    with patch('curio.curio_monitor.fetch_kleinanzeigen',return_value=(None,'人机验证')) as fetch:
        collect_ka(db,cfg,b);collect_ka(db,cfg,b)
    assert fetch.call_count==1 and db.get('ka_status')=='paused'

def test_status_check_skips_exhausted_platform(db):
    from curio.curio_monitor import check_existing
    cfg=load()
    db.ingest({'item_id':'e1','platform':'ebay','title':'Meissen Tasse','price':40,'currency':'EUR'},cfg)
    b=Budget(db,{'ebay':0,'etsy':10,'kleinanzeigen':10},Clock(at(2,12)))
    api=Mock()
    summary=check_existing(db,api,dict(cfg,kleinanzeigen_enabled=False),b)
    api.detail.assert_not_called()
    assert summary['budget_exhausted']==['ebay'] and summary['attempted']==0
