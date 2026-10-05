import importlib.util
import json
import sqlite3
from pathlib import Path

spec = importlib.util.spec_from_file_location('wechat_feed', Path(__file__).parents[1]/'scripts/wechat_feed.py')
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def test_feed_hides_reviewed_gone_rejected_and_customer_data(tmp_path):
    data = tmp_path/'data'; data.mkdir()
    report = data/'trial_screening_20261005'; report.mkdir()
    c = sqlite3.connect(data/'curio.db')
    c.executescript('''CREATE TABLE items(item_id,title,listed_price,total_price,currency,buying,url,checked_at,status,agent_decision);
        CREATE TABLE cases(item_id);''')
    for iid,status,decision in [('ok','active','probe'),('gone','gone','probe'),('skip','active','skip'),('reviewed','active','want')]:
        c.execute('INSERT INTO items VALUES(?,?,?,?,?,?,?,?,?,?)',(iid,'Meissen Tasse',12,12,'EUR','AUCTION','https://example.org/item','2026-10-05',status,decision))
    c.execute("INSERT INTO cases VALUES('reviewed')");c.commit();c.close()
    payload={'summary':{'generated_at':'2026-10-05'},'results':[
        {'bucket':'优先复核','item':{'item_id':iid,'local_image':'../../private-customer.jpg'},
         'candidate_attributes':{'maker':['Meissen'],'form':['杯'],'decor':[]},
         'evidence':[{'text':'PRIVATE CUSTOMER MESSAGE','customer_id':'PRIVATE-ID'}]}
        for iid in ['ok','gone','skip','reviewed']]}
    (report/'完整初筛.json').write_text(json.dumps(payload))
    result=module.feed(tmp_path)
    assert [i['item_id'] for i in result['items']]==['ok']
    assert result['items'][0]['image_file']==''
    assert 'PRIVATE' not in json.dumps(result)
    assert module.feed(tmp_path,offset=3)['items']==[]
