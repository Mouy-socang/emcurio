import copy,json
from pathlib import Path
from unittest.mock import patch
import pytest
from curio.store import Store
from curio.vision import init,validate,Failure,confirm,work,packed

def sample():return {'taxonomy_version':'curio_porcelain_v1','object_type':'cup','object_subtype':None,'components':[{'component_id':'c1','component_type':'cup','quantity':None,'form':{'plan_shape':'round'}}],'decorations':None,'dimensions':None,'field_meta':{'/object_type':{'status':'known','basis':'image_observation','evidence_ids':['im1']}}}

def test_validation_references_units_and_unknown():
 d=sample();assert validate(d,{'im1'})['object_subtype'] is None
 for mutate in [lambda x:x.update(object_subtype='dinner'),lambda x:x.update(dimensions=[{'component_id':'c2','measurements':{}}]),lambda x:x.update(dimensions=[{'component_id':'c1','measurements':{'height_cm':-1},'evidence_ids':['im1']}]),lambda x:x['field_meta']['/object_type'].update(evidence_ids=['fake'])]:
  x=sample();mutate(x)
  with pytest.raises(Failure):validate(x,{'im1'})

def setup(db):
 init(db)
 with db.connect() as c:
  c.execute("INSERT INTO product_snapshots VALUES(1,'one','fp','now',?)",(packed({'item_id':'one','images':[{}]}),))
  c.execute("INSERT INTO visual_jobs(id,snapshot_id,model,prompt_hash,state,created_at) VALUES(1,1,'test','p','queued','now')")

def test_queue_cache_and_manual_reference(tmp_path):
 db=Store(tmp_path/'curio.db');setup(db);s=sample();s['evidence']=[{'id':'im1','kind':'image','locator':'images/photo.jpg'}]
 with patch('curio.vision.infer',return_value=(s,{})) as call:
  work(db,2,tmp_path);work(db,2,tmp_path);assert call.call_count==1
 with db.connect() as c:
  assert c.execute('SELECT count(*) FROM porcelain_references').fetchone()[0]==0
  rid=c.execute('SELECT id FROM visual_results').fetchone()[0]
 corrected=copy.deepcopy(s);corrected['components'][0]['quantity']=1
 confirm(db,rid,corrected,'卖家明确出售1杯')
 with db.connect() as c:
  assert json.loads(c.execute('SELECT data FROM visual_results').fetchone()[0])['components'][0]['quantity'] is None
  assert json.loads(c.execute('SELECT data FROM porcelain_references').fetchone()[0])['components'][0]['quantity']==1

def test_queue_retry_keeps_failure_sanitized(tmp_path):
 db=Store(tmp_path/'curio.db');setup(db)
 with patch('curio.vision.infer',side_effect=Failure('NVIDIA HTTP 429')):work(db,3,tmp_path)
 with db.connect() as c:
  row=c.execute('SELECT state,attempts FROM visual_jobs').fetchone();assert tuple(row)==('retry',1)
  assert c.execute('SELECT count(*) FROM visual_results').fetchone()[0]==0
  metric=c.execute('SELECT attempt,outcome,error FROM visual_attempt_metrics').fetchone()
  assert tuple(metric)==(1,'failed','NVIDIA HTTP 429')

def test_conservative_unknown_and_no_dimension_guess():
 from curio.vision import conservative_candidate
 d=sample();d['decorations']=[{'component_id':'c1','motif_subjects':'floral botanical','motif_styles':'other','ground':{'present':True,'colors':['white']}}]
 d['dimensions']=[{'component_id':'c1','measurements':{'diameter_cm':8},'raw_text':'8cm','evidence_ids':['im1']}]
 conservative_candidate(d)
 assert d['decorations'][0]['motif_subjects']==['floral','botanical']
 assert d['decorations'][0]['motif_styles'] is None
 assert d['decorations'][0]['ground']['present'] is None
 assert d['dimensions'][0]['measurements']=={}
 validate(d,{'im1'})

def test_invalid_subtype_becomes_unknown_candidate():
 from curio.vision import conservative_candidate
 d=sample();d['object_type']='plate';d['object_subtype']='plate'
 conservative_candidate(d)
 assert d['object_subtype'] is None
 assert d['field_meta']['/object_subtype']['candidates']==['plate']
 validate(d,{'im1'})

def test_worker_claims_gone_before_active(tmp_path):
 from curio.config import load
 db=Store(tmp_path/'curio.db');setup(db)
 base=dict(platform='ebay',title='Meissen Porzellan Tasse',price=20,currency='EUR')
 db.ingest(dict(base,item_id='one'),load());db.ingest(dict(base,item_id='gone'),load());db.mark_gone('gone')
 with db.connect() as c:
  c.execute("INSERT INTO product_snapshots VALUES(2,'gone','gone-fp','now',?)",(packed({'item_id':'gone','images':[{}]}),))
  c.execute("INSERT INTO visual_jobs(id,snapshot_id,model,prompt_hash,state,created_at) VALUES(2,2,'test','p','queued','now')")
 result=sample();result['evidence']=[{'id':'im1'}]
 with patch('curio.vision.infer',return_value=(result,{})):work(db,1,tmp_path)
 with db.connect() as c:assert c.execute('SELECT item_id FROM visual_results').fetchone()[0]=='gone'

def test_visual_launchd_persists_without_credentials():
 from scripts.install_visual_worker import configuration
 d=configuration()
 assert d['KeepAlive'] is True and d['RunAtLoad'] is True
 assert d['ProgramArguments'][-1]=='curio.vision_worker'
 assert 'NVIDIA_API_KEY' not in str(d)

def test_human_field_evidence_preserves_model(tmp_path):
 db=Store(tmp_path/'curio.db');setup(db);d=sample();d['evidence']=[{'id':'im1','kind':'image'}]
 with patch('curio.vision.infer',return_value=(d,{})):work(db,1,tmp_path)
 corrected=copy.deepcopy(d);corrected['components'][0]['quantity']=2;corrected['field_meta']['/components/0/quantity']={'status':'known','basis':'reference','evidence_ids':['human_review']}
 confirm(db,1,corrected,'卖家出售两件，照片为不同角度')
 with db.connect() as c:
  ref=json.loads(c.execute('SELECT data FROM porcelain_references').fetchone()[0]);assert ref['evidence'][-1]['kind']=='human_note'
  assert ref['components'][0]['quantity']==2
  assert json.loads(c.execute('SELECT data FROM visual_results').fetchone()[0])['components'][0]['quantity'] is None

def test_page_prefers_manual_classification(tmp_path):
 from curio.vision import page
 db=Store(tmp_path/'curio.db');setup(db);d=sample();d['evidence']=[{'id':'im1','kind':'image'}]
 with patch('curio.vision.infer',return_value=(d,{})):work(db,1,tmp_path)
 edit=copy.deepcopy(d);edit['object_type']='bowl';edit['object_subtype']=None
 confirm(db,1,edit,'人工改为碗')
 assert page(db)['results'][0]['data']['object_type']=='bowl'
 with db.connect() as c:assert json.loads(c.execute('select data from visual_results').fetchone()[0])['object_type']=='cup'

def test_nested_labels_and_alias_basis_do_not_crash():
 from curio.vision import conservative_candidate
 d=sample();d['decorations']=[{'component_id':'c1','motif_subjects':[{'label':'flower'}],'palette_colors':['blue','made_up_color']}]
 d['field_meta']['/object_type']['basis']='visual_observation'
 conservative_candidate(d)
 assert d['decorations'][0]['motif_subjects'] is None
 assert d['decorations'][0]['palette_colors']==['blue']
 assert d['field_meta']['/object_type']['basis']=='image_observation'
 # Valid blue subset has no source metadata: no automatic verified claim.
 d['field_meta']['/decorations/0/palette_colors']['evidence_ids']=['im1']
 validate(d,{'im1'})

def test_unexpected_exception_releases_job(tmp_path):
 db=Store(tmp_path/'curio.db');setup(db)
 with patch('curio.vision.infer',side_effect=TypeError('private response')):work(db,1,tmp_path)
 with db.connect() as c:
  r=c.execute('select state,error from visual_jobs').fetchone()
  assert r['state']=='needs_review' and 'private response' not in r['error']

def test_same_evidence_uses_cached_api_results(tmp_path):
 from curio.vision import infer
 image=tmp_path/'data/porcelain/images/photo.jpg';image.parent.mkdir(parents=True);image.write_bytes(b'fake image for mocked API')
 data={'item_id':'cache','title':'Meissen Tasse','description':'','condition':'','url':'https://example.test/item','images':[{'file':'images/photo.jpg','mime':'image/jpeg','url':'https://i.ebayimg.com/example.jpg'}]}
 response=json.dumps(sample())
 with patch('curio.vision.request_model',side_effect=[('White floral cup',{}),(response,{})]) as call:
  first=infer(data,'test-model',tmp_path)
  second=infer(data,'test-model',tmp_path)
  assert first==second and call.call_count==2
