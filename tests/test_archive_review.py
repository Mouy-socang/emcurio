import json
import pytest
from curio.store import Store,now
from curio.vision import init as init_vision
from curio.archive_review import init,review,FIELDS,page

def test_review_appends_and_preserves_original(tmp_path):
 s=Store(tmp_path/'test.db');init_vision(s);init(s)
 with s.connect() as c:
  c.execute('INSERT INTO product_snapshots(id,data) VALUES(1,?)',(json.dumps({'title':'原文'}),))
  c.execute('INSERT INTO archive_classifications(id,batch_id,snapshot_id,item_id,created_at,data) VALUES(1,?,?,?,?,?)',('batch',1,'item',now(),json.dumps({'fields':{'品牌':'原始候选'}})))
 fields={k:'待核' for k in FIELDS}
 review(s,1,fields,'首次审核');fields['品牌']='人工修改';review(s,1,fields,'再次修改')
 assert page(s)['results'][0]['reviewed_fields']['品牌']=='人工修改'
 with s.connect() as c:
  assert c.execute('SELECT count(*) FROM archive_classification_reviews').fetchone()[0]==2
  assert json.loads(c.execute('SELECT data FROM archive_classifications').fetchone()[0])['fields']['品牌']=='原始候选'
 with pytest.raises(ValueError):review(s,1,{'品牌':'缺少字段'},'')
 with pytest.raises(ValueError):review(s,99,fields,'')

def test_function_form_revision_keeps_user_review_and_base(tmp_path):
 s=Store(tmp_path/'test.db');init_vision(s);init(s)
 original={'fields':{'功能':{'value':'原功能'},'器型':{'value':'原器型'}}}
 patch={'fields':{'功能':{'value':'摩卡杯碟候选'},'器型':{'value':'厂家器型待确认；波浪口沿'}},'classification_axes':{'厂家器型':{'名称':None}},'classification_rule':'独立维度','knowledge_references':[]}
 with s.connect() as c:
  c.execute('INSERT INTO product_snapshots(id,data) VALUES(1,?)',(json.dumps({'title':'原文'}),))
  c.execute('INSERT INTO archive_classifications(id,batch_id,snapshot_id,item_id,created_at,data) VALUES(1,?,?,?,?,?)',('batch',1,'item',now(),json.dumps(original)))
  c.execute('INSERT INTO archive_classification_updates(classification_id,created_at,data) VALUES(1,?,?)',(now(),json.dumps(patch)))
 fields={k:'人工审核值' for k in FIELDS};review(s,1,fields,'人工优先')
 r=page(s)['results'][0]
 assert r['data']['fields']['功能']['value']=='摩卡杯碟候选'
 assert r['data']['fields']['器型']['value']!='摩卡杯碟候选'
 assert r['reviewed_fields']['器型']=='人工审核值'
 with s.connect() as c:assert json.loads(c.execute('SELECT data FROM archive_classifications').fetchone()[0])==original
