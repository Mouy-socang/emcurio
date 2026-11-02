"""Build a local, reviewable daily V2 report using frozen evidence and historical customer signals."""
import argparse
import collections
import datetime as dt
import json
from pathlib import Path
import re
import sys

ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from curio.product_v2 import extract,FIELDS
from daily_v2_sources import off_topic
from trial_customer_screen import features,overlap

ORDER=['优先复核','次级候选','资料待补','暂不优先','规则排除','已人工审核','已下架']

def customer_matches(item, product, evidence):
    f=features(item['title'])
    if product['maker'] in ('Teichert','Sörnewitz','E. M. Schmidt'):return []
    matches=[]
    for e in evidence:
        shared=overlap(f,e['features'])
        if not shared:continue
        unmet=[]
        for key in ('decor','qualifiers'):unmet.extend(sorted(set(e['features'][key])-set(f[key])))
        if e['kind']=='历史需求线索' and not set(e['features']['form']).issubset(f['form']):unmet.append('器型/配套未完整对应')
        if e['features']['sizes_cm'] and not set(e['features']['sizes_cm'])&set(f['sizes_cm']):unmet.append('尺寸未对应')
        strong=bool(shared['decor']) and not unmet
        if e['kind']=='历史订单线索' and not strong:continue
        matches.append(dict(e,matched=shared,unmet_requirements=unmet,strong=strong))
    matches.sort(key=lambda m:(m['strong'],m['kind']=='历史需求线索',m.get('date') or ''),reverse=True)
    distinct={}
    for m in matches:distinct.setdefault((m['customer_id'],m['kind']),m)
    return list(distinct.values())

def main():
    a=argparse.ArgumentParser();a.add_argument('--date',required=True);args=a.parse_args()
    out=ROOT/'data'/('daily_v2_'+args.date.replace('-',''))
    snapshot=json.loads((out/'source_snapshot.json').read_text())
    source=ROOT/'data/trial_screening_20261005/证据索引.json'
    old=json.loads(source.read_text());evidence=old['signals']+old['orders'];results=[]
    for original in snapshot['items']:
        item=dict(original);path=out/'details'/(re.sub(r'[^a-zA-Z0-9_-]','_',item['item_id'])+'.json');detail=json.loads(path.read_text()) if path.exists() else {}
        excluded=off_topic(item['title'])
        row=extract(item,detail)
        if excluded:
            row['candidate_product']=dict(row['product']);row['inferences']=[];row['warnings']=[];row['seller_grade']={'label':None,'value':'unknown','claims':[]}
            row['title_preview_zh']=excluded+'（梅森相关搜索结果，排除）';row['notes']=['非本轮目标，保留原文供核查；不将地名映射为瓷器品牌'];row['title_status']='排除记录中文说明'
        p=row['candidate_product'];matches=[] if excluded else customer_matches(item,p,evidence)
        strong=[m for m in matches if m['strong']]
        manual=snapshot['manual'].get(item['item_id'])
        gone=detail.get('status')=='gone' or any(x.get('estimatedAvailabilityStatus')=='OUT_OF_STOCK' for x in detail.get('availability',[])) or item['status']!='active'
        serious=any(x in row['warnings'] for x in ('破损','修复','磕口','裂纹','需修理'))
        if manual:bucket='已人工审核';reason='保留原人工判断：'+manual['decision']
        elif gone:bucket='已下架';reason='来源或本地状态显示不可售，不推断为已成交'
        elif excluded:bucket='规则排除';reason=excluded
        elif serious:bucket='规则排除';reason='卖家文字有'+ '、'.join(row['warnings'])+'线索，依破损/修复默认不要规则排除；可人工复核'
        elif not p['object_type']:bucket='资料待补';reason='器型或实际出售组成不足，保留工作标题等待核实'
        elif strong:bucket='优先复核';reason='具体图案与器型有历史客户/订单线索；需求有效性及商品事实仍待核验'
        elif matches or item['agent_decision']=='want':bucket='次级候选';reason='有部分客户线索，但装饰、规格或配套要求尚未齐全'
        else:bucket='暂不优先';reason='未找到足够具体的历史客户线索；不代表不能卖或不值得买'
        # Live listing price remains separate from acquisition cost and target sale price.
        if detail.get('price'):
            item['listed_price']=float(detail['price']['value']);item['currency']=detail['price'].get('currency',item['currency'])
        if detail.get('buying'):item['buying']=','.join(detail['buying'])
        score=max((len(m['matched']['decor'])*20+10*bool(m['matched']['maker']) for m in strong),default=0)+min(5,len({m['customer_id'] for m in strong}))
        row.update(item=item,source_snapshot=original,bucket=bucket,reason=reason,priority_score=score,customer_evidence=matches[:8],customer_evidence_count=len(matches),strong_customer_count=len({m['customer_id'] for m in strong}),detail_status=detail.get('status','not_requested'),detail_retrieved_at=detail.get('retrieved_at'),owner_decision=manual,suggested_purchase_price=None)
        results.append(row)
    results.sort(key=lambda r:r['item']['first_seen'],reverse=True);results.sort(key=lambda r:(ORDER.index(r['bucket']),-r['priority_score']))
    summary=dict(date=args.date,timezone=snapshot['timezone'],scope='今日首次入库（并非全部今日上架）；柏林时间 00:00 起至快照时间',captured_at=snapshot['captured_at'],generated_at=dt.datetime.now(dt.timezone.utc).isoformat(),items=len(results),buckets=dict(collections.Counter(r['bucket'] for r in results)),details=dict(collections.Counter(r['detail_status'] for r in results)),schema_fields=len(FIELDS),customer_evidence_as_of='2026-10-05',last_collection=snapshot['runs'][0])
    payload=dict(summary=summary,field_definitions=FIELDS,results=results)
    (out/'完整字段与初筛.json').write_text(json.dumps(payload,ensure_ascii=False,indent=2))
    (out/'中文标题清单.txt').write_text('\n'.join(f"{n+1}. [{r['bucket']}] {r['title_preview_zh']}\n   {r['item']['listed_price']} {r['item']['currency']} | {r['item']['url']}" for n,r in enumerate(results)))
    render(out,payload)
    print(json.dumps(summary,ensure_ascii=False,indent=2))

def render(out,payload):
    template=(ROOT/'scripts/daily_v2_template.html').read_text()
    data=json.dumps(payload,ensure_ascii=False).replace('<','\\u003c').replace('\u2028','\\u2028').replace('\u2029','\\u2029')
    (out/'今日商品初筛.html').write_text(template.replace('__DATA__',data))

if __name__=='__main__':main()
