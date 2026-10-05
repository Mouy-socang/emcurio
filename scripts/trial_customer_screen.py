"""Read-only, local, evidence-linked trial. Does not modify production decisions."""
import collections
import datetime as dt
import hashlib
import html
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
SOURCE = Path('/Users/shichunxiao/Desktop/goodcustomer/已整理')
OUT = ROOT / 'data/trial_screening_20261005'
ALIASES = {
 'maker': {
  'Meissen': r'meissen|meißen|梅森|迈森', 'KPM': r'(?<![a-z])kpm(?![a-z])|柏林皇家',
  'Royal Copenhagen': r'royal copenhagen|哥本哈根', 'Herend': r'herend|赫伦',
  'Sèvres': r's[èe]vres|塞弗尔|塞夫勒', 'Rosenthal': r'rosenthal|罗森塔尔'},
 'form': {
  '杯': r'(?<!unter)tasse|\bcups?\b|杯|茶盏|gedeck|becher|\bmug\b',
  '碟': r'untertasse|saucers?|碟', '盘': r'teller|\bplates?\b|盘',
  '花瓶': r'\bvase[n]?\b|花瓶', '壶': r'kanne|teapot|coffee ?pot|壶',
  '碗': r'schale|schüssel|schuessel|\bbowls?\b|碗',
  '瓷偶': r'figur|\bgroup\b|瓷偶|人偶|塑像|雕塑|小天使|小男孩|putto',
  '盒罐': r'\bdose|\bjars?\b|porcelain box|trinket box|盒|罐',
  '烛台': r'leuchter|candlestick|烛台', '瓷板': r'porzellanbild|plaque|瓷板',
  '纸镇': r'briefbeschwerer|paperweight|纸镇', '烟灰缸': r'aschenbecher|ashtray|烟灰缸',
  '勺': r'löffel|loeffel|spoon|勺', '托盘': r'tablett|\btray\b|托盘'},
 'decor': {
  '黄玫瑰': r'gelbe\w* rose|yellow rose|黄玫瑰',
  '红玫瑰': r'rote\w* rose|red rose|红玫瑰',
  '蓝洋葱': r'zwiebelmuster|blue onion|蓝洋葱|兰洋葱',
  '钴蓝': r'kobalt|cobalt|钴蓝', '天鹅服务': r'schwanenservice|swan service|天鹅服务',
  '天鹅颈': r'schwanenhals|swan.?neck|天鹅颈',
  'B-form': r'(?<![a-z])b[ -]?form(?![a-z])|\bbf\b|b边', 'X-form': r'(?<![a-z])x[ -]?form(?![a-z])|\bxf\b',
  'A-form': r'\ba[ -]?form\b|a边', '库尔兰': r'kurland|库尔兰',
  '唐草': r'musselmalet|blue fluted|唐草', '橡树叶': r'eichenlaub|oak lea|橡树叶',
  '镂空': r'durchbruch|reticulated|镂空', '立体花': r'aufgelegte blumen|applied flowers|立体花',
  '柏林杯': r'berliner form|berlin cup|柏林杯', 'Hentschel': r'hentschel|亨切尔', '印度花': r'indisch|indian flower|印度之花|印度花', '葡萄藤': r'weinlaub|vine leaf|葡萄藤', '多花': r'blumenbukett|buket|多花', '散花': r'streubl|散花'},
}
NEGATIVE = re.compile(r'不喜欢|不太喜欢|不想|不太想|不要|放弃|想卖|卖掉|挂闲鱼|已[经]?买|买了|收了|找到了|找到|找的|收到|谢谢|退了|他.{0,4}喜欢|他们|大家|男老板|南方人|北方人')
INTENT = re.compile(r'想要|想买|想找|喜欢|帮我|留意|需要|我要|我收|在集')
QUALIFIERS = {
 '高花瓶': r'高的花瓶|高花瓶|tall vase',
 '小男孩': r'小男孩|knabe|junge|boy', '方形': r'方盘|方碟|square|quadratisch', '椭圆': r'椭圆|oval',
 '超大': r'超大|jumbo|übergroß|riesig', '带盖': r'带盖|mit deckel|covered',
 '高脚': r'高脚|fußschale|fussschale|fußteller|footed|tazza',
 '多层': r'多层|etagere|tiered', '云朵': r'云朵|neuer ausschnitt',
 '手握': r'手握|henkellos|handleless', '盖碗': r'盖碗|deckelschale|covered bowl',
 '摩卡': r'摩卡|mokka|mocca|mocha|espresso', '大件': r'大托盘|大壶|groß|gross|large|jumbo|übergroß',
 '小件': r'小壶|迷你|mini|klein|small', '一等': r'一等|1\.?\s*wahl|erste wahl',
 '重金': r'重金|顶级金|prunkvergold|reich.*gold', '宽金': r'宽金|breiter gold',
}

def features(text):
    f = {k: sorted(n for n, pat in group.items() if re.search(pat, text, re.I)) for k, group in ALIASES.items()}
    if '托盘' in f['form'] and '盘' in f['form']: f['form'].remove('盘')
    f['qualifiers'] = sorted(k for k,p in QUALIFIERS.items() if re.search(p, text, re.I))
    f['sizes_cm'] = re.findall(r'(\d+(?:[.,]\d+)?)\s*(?:cm|厘米|公分)', text, re.I)
    return f

def overlap(a, b):
    if a['maker'] and b['maker'] and not set(a['maker']) & set(b['maker']):
        return None
    shared = {k: sorted(set(a[k]) & set(b[k])) for k in ALIASES}
    if not shared['form']:
        return None
    # A series/pattern plus form is useful; maker + generic form alone is weak.
    if not shared['decor'] and not shared['maker']:
        return None
    return shared

def read(path):
    return json.loads(path.read_text())

def main():
    OUT.mkdir(parents=True, exist_ok=True)
    now = dt.datetime.now(dt.timezone.utc)
    signals, orders, audit = [], [], []
    profiles = sorted(SOURCE.rglob('客户总档案.json'))
    for p in profiles:
        d = read(p)
        for category in ('想要', '喜欢'):
            for e in d.get(category, []):
                t = e.get('text', '')
                f = features(t)
                reason = None
                if e.get('side') != 'customer': reason = '非客户发言'
                elif NEGATIVE.search(t): reason = '包含否定、第三方或完成线索；不作正向匹配'
                elif not INTENT.search(t): reason = '缺少明确意向'
                elif not f['form'] or not (f['maker'] or f['decor']): reason = '缺少可定位的品牌/图案与器型'
                if reason:
                    audit.append({'customer_id': p.parent.name, 'message_id': e.get('message_id'), 'reason': reason})
                    continue
                signals.append({'kind': '历史需求线索', 'customer_id': p.parent.name,
                    'text': t, 'date': e.get('datetime'), 'message_id': e.get('message_id'),
                    'source_path': str(p), 'features': f,
                    'caveat': '未核实需求是否仍有效或已满足；只匹配部分文字特征，其余要求须复核'})
    all_products = sorted(SOURCE.rglob('商品资料.json'))
    unassigned = 0
    for p in all_products:
        if '待确认归属' in p.parts:
            unassigned += 1
            continue
        d = read(p)
        cid = next((part for part in p.parts if re.fullmatch(r'MM\d+', part)), None)
        if not cid: continue
        text = ' '.join(str(d.get(k) or '') for k in ('raw_title', 'raw_description', 'maker', 'object_type'))
        f = features(text)
        if not f['form'] or not (f['decor'] or f['maker']): continue
        transactions = read(p.with_name('订单与退款原记录.json'))
        if re.search(r'破损|磕碰|修复|裂纹|残损', text): continue
        # Any refund/cancellation evidence prevents positive order support.
        if any(str(x.get('是否退款_原记载') or '').strip() not in ('', '无', '否', '0', '未退款')
               or x.get('退款金额') not in (None, 0, '', '0') for x in transactions):
            continue
        orders.append({'kind': '历史订单线索', 'customer_id': cid, 'product_id': d['product_id'],
            'text': d.get('raw_title'), 'features': f, 'source_path': str(p),
            'caveat': '订单表记录；付款、履约、客户归属仍待核实，不作为实际成交价或购买概率',
            'date': None})
    # One consistent in-memory SQLite snapshot; live DB remains read-only.
    live = sqlite3.connect(f'file:{ROOT / "data/curio.db"}?mode=ro', uri=True)
    snap = sqlite3.connect(':memory:'); live.backup(snap); live.close()
    snap.row_factory = sqlite3.Row
    items = [dict(x) for x in snap.execute('SELECT * FROM items')]
    manual = {x['item_id']: dict(x) for x in snap.execute('SELECT * FROM cases ORDER BY id')}
    runs = [dict(x) for x in snap.execute('SELECT * FROM collection_runs ORDER BY id DESC LIMIT 2')]
    results = []
    for item in items:
        f = features(item['title'] + ' ' + (item.get('description') or ''))
        matches = []
        for e in signals + orders:
            shared = overlap(f, e['features'])
            if not shared: continue
            unmet = []
            for k in ('decor', 'qualifiers'):
                unmet.extend(sorted(set(e['features'][k])-set(f[k])))
            # Do not turn a compound request for a footed tiered fruit dish into a dinner plate match.
            requested_forms = set(e['features']['form'])
            if e['kind']=='历史需求线索' and not requested_forms.issubset(set(f['form'])):
                unmet.append('器型/配套未完整对应')
            if e['features']['sizes_cm']:
                if not set(e['features']['sizes_cm']) & set(f['sizes_cm']): unmet.append('尺寸未对应')
            strong = bool(shared['decor']) and not unmet
            if e['kind'] == '历史订单线索' and not strong: continue
            specificity = len(shared['decor']) * 2 + bool(shared['maker'])
            match = dict(e, matched=shared, unmet_requirements=unmet, strength='具体图案与器型' if strong else '部分相似，要求未齐', specificity=specificity)
            matches.append(match)
        matches.sort(key=lambda x: (x['strength']=='具体图案与器型', x['kind']=='历史需求线索', x['specificity'], x['date'] or ''), reverse=True)
        distinct = {}; evidence = []
        for m in matches:
            key = (m['customer_id'], m['kind'])
            if key not in distinct:
                distinct[key] = True; evidence.append(m)
        strong = [m for m in evidence if m['strength']=='具体图案与器型']
        if item['item_id'] in manual:
            bucket = '已人工审核'; why = '保留店主判断，本次不改变'
        elif item.get('status') != 'active':
            bucket = '已下架或非在售'; why = '采集记录显示非在售'
        elif item['agent_decision'] == 'skip':
            bucket = '规则排除'; why = item['agent_reason']
        elif strong:
            bucket = '优先复核'; why = '具体图案/系列与器型有客户或订单线索；尚不能直接判断值得买'
        elif evidence or item['agent_decision'] == 'want':
            bucket = '次级候选'; why = '只有宽泛客户线索或原规则支持，暂不足以重点推荐'
        else:
            bucket = '暂不优先'; why = '当前文字证据未找到具体客户/订单匹配；不代表卖不掉'
        score = (max((m['specificity'] for m in strong), default=0)*10
                 + min(len({m['customer_id'] for m in strong}), 5)
                 + 5*any(m['kind']=='历史需求线索' for m in strong))
        results.append({'item': item, 'candidate_attributes': f, 'attributes_status': '从标题/文字提取，非核验事实',
            'bucket': bucket, 'priority_score': score, 'reason': why, 'matched_customers': len({m['customer_id'] for m in evidence}),
            'evidence': evidence[:8], 'evidence_count': len(evidence),
            'owner_decision': manual.get(item['item_id'], {}).get('decision'),
            'suggested_purchase_price': None,
            'missing': ['照片、底款、品相及真伪未核验', '尺寸、组件数量和具体款式须与原始需求逐项对照',
                        '心理价暂缺：没有已确认的可比成交价和正式价格线',
                        '总价沿用库中口径，未知运费/税费未计入；拍卖为当前价',
                        '本轮未访问原网页复核在售状态']})
    order = ['优先复核', '次级候选', '暂不优先', '规则排除', '已人工审核', '已下架或非在售']
    results.sort(key=lambda r: r['item'].get('created') or r['item'].get('first_seen') or '', reverse=True)
    results.sort(key=lambda r: (order.index(r['bucket']), -r['priority_score']))
    # Pick at most 30 varied candidates; repeated pattern/form does not dominate the first page.
    shortlisted = []; quota = collections.Counter()
    for r in results:
        if r['bucket'] != '优先复核': continue
        f=r['candidate_attributes']; key=(tuple(f['decor']), tuple(f['form']))
        if quota[key]>=3: continue
        if len(shortlisted)>=30: break
        quota[key]+=1; shortlisted.append(r['item']['item_id'])
    summary = {'generated_at': now.isoformat(), 'mode': '本地文字规则与客户证据试筛；无外部模型调用',
        'catalog_source': str(ROOT/'data/curio.db'), 'source_root': str(SOURCE),
        'items': len(results), 'platforms': dict(collections.Counter(x['platform'] for x in items)),
        'buckets': dict(collections.Counter(r['bucket'] for r in results)), 'shortlist_count': len(shortlisted),
        'profiles_read': len(profiles), 'product_records_read': len(all_products),
        'unassigned_products_not_used': unassigned, 'usable_order_records': len(orders),
        'usable_demand_signals': len(signals), 'signal_exclusions': len(audit),
        'last_collection_runs': runs,
        'limitations': ['具体客户仍需确认需求有效性；历史线索不是购买承诺',
            '历史订单付款与履约未核实，不推算成交价、利润或心理价',
            'V2字段只做候选提取，未完成全字段分类',
            '保留原规则及人工判断；未实现跨平台同物图片去重',
            '本报告是商品库快照，不是本轮重新抓取全部网站']}
    payload = {'summary': summary, 'shortlist_item_ids': shortlisted, 'results': results}
    (OUT/'完整初筛.json').write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    (OUT/'证据索引.json').write_text(json.dumps({'signals': signals, 'orders': orders, 'signal_audit': audit}, ensure_ascii=False, indent=2))
    render(payload)
    print(json.dumps(summary, ensure_ascii=False, indent=2))

def render(payload):
    data = json.dumps(payload, ensure_ascii=False).replace('<', '\\u003c')
    page = '''<!doctype html><html lang="zh"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>Curio · 本地初筛</title>
<style>body{margin:0;background:#f4f3ef;color:#213a30;font:16px system-ui}header,main{max-width:1200px;margin:auto;padding:24px}h1{font-size:28px}p{line-height:1.7}.muted{color:#68756e;font-size:14px}select,input,button{padding:10px;border:1px solid #cad2cb;border-radius:8px;background:white;font-size:16px;max-width:100%}.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(290px,1fr));gap:18px}.card{background:white;padding:16px;border-radius:14px}.card img{width:100%;height:220px;object-fit:contain;background:#fafafa}.price{font-size:16px;font-weight:650}.tag{font-size:13px;color:#416857}h2{font-size:17px;line-height:1.5}li{margin:8px 0;line-height:1.55}details{margin-top:14px;font-size:14px}a{color:#2f6654}nav{display:flex;gap:8px;flex-wrap:wrap;margin:20px 0}.empty{padding:40px}blockquote{margin:8px 0;border-left:3px solid #c6d6c9;padding-left:10px}footer{padding:24px}strong{color:#264d3d}</style>
<header><h1>Curio · 第一轮客户证据初筛</h1><p id="intro"></p><p class="muted">这是本地文字初筛，优先级不是购买建议。历史需求可能已经满足；订单表价格不是实际成交价。照片、底款、真假与完整配套尚未核验。未修改人工审核。商品文字已保存在本地，图片未缓存时需要联网。</p><nav><select id="bucket"><option>精选先看</option><option>优先复核</option><option>次级候选</option><option>暂不优先</option><option>规则排除</option><option>已人工审核</option><option>已下架或非在售</option><option>全部</option></select><select id="platform"><option>全部平台</option><option>ebay</option><option>kleinanzeigen</option></select><input id="search" placeholder="搜索标题、图案、客户编号"><a href="完整初筛.json" download>下载完整 JSON</a></nav><p id="count" class="muted"></p></header><main><div id="cards" class="grid"></div><nav><button id="prev">上一页</button><span id="page"></span><button id="next">下一页</button></nav></main>
<script>const D=__DATA__;const $=id=>document.getElementById(id);const esc=s=>String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));const url=s=>{try{let u=new URL(s);return ['https:','http:'].includes(u.protocol)?esc(s):'#'}catch{return '#'}};let page=0;
$('intro').textContent=`共 ${D.summary.items} 件商品 · ${D.summary.profiles_read} 份客户档案 · ${D.summary.product_records_read} 条商品档案。精选 ${D.summary.shortlist_count} 件先看。生成时间：${D.summary.generated_at}`;
function draw(){let b=$('bucket').value,p=$('platform').value,q=$('search').value.toLowerCase();let rows=D.results.filter(r=>(b==='全部'||(b==='精选先看'?D.shortlist_item_ids.includes(r.item.item_id):r.bucket===b))&&(p==='全部平台'||r.item.platform===p)&&(!q||JSON.stringify([r.item.title,r.candidate_attributes,r.evidence.map(e=>e.customer_id)]).toLowerCase().includes(q)));let pages=Math.max(1,Math.ceil(rows.length/30));page=Math.min(page,pages-1);$('count').textContent=`本组 ${rows.length} 件 · 全库分组：`+Object.entries(D.summary.buckets).map(([k,v])=>k+' '+v).join(' / ');$('page').textContent=`${page+1} / ${pages}`;$('prev').disabled=page===0;$('next').disabled=page>=pages-1;
$('cards').innerHTML=rows.slice(page*30,page*30+30).map(r=>{let i=r.item,attrs=Object.values(r.candidate_attributes).flat().join(' · ');return `<article class="card">${i.image?`<img loading="lazy" referrerpolicy="no-referrer" src="${i.local_image?esc(i.local_image):url(i.image)}" alt="商品照片" onerror="this.alt='图片暂不可用；可打开原链接'">`:''}<p class="tag">${esc(r.bucket)} · ${esc(i.platform)}</p><h2>${esc(i.title)}</h2><p class="price">${/auction|拍卖/i.test(i.buying||'')?'拍卖当前价':'标价'} ${esc(i.listed_price??'待询价')} ${esc(i.currency)} · 总价 ${esc(i.total_price??'未知')}</p><p class="muted">${esc(attrs)}（文字候选）</p><p>${esc(r.reason)}</p><a href="${url(i.url)}" target="_blank" rel="noopener">查看原商品 ↗</a><details ${r.bucket==='优先复核'?'open':''}><summary>客户 / 订单依据（${r.evidence_count} 条；最多展示8条）</summary>${r.evidence.map(e=>`<p><strong>${esc(e.customer_id)} · ${esc(e.kind)}</strong><br>${esc(e.date||'日期待核实')} · ${esc(e.strength)}</p><blockquote>${esc(e.text)}</blockquote><p>匹配：${esc(Object.values(e.matched).flat().join(' / '))}</p><p class="muted">${e.unmet_requirements.length?'未对应：'+esc(e.unmet_requirements.join(' / '))+'<br>':''}${esc(e.caveat)}<br>出处：${esc(e.source_path)}${e.message_id?' · 消息 '+esc(e.message_id):''}</p>`).join('')||'<p>未找到足够具体的证据。</p>'}</details><details><summary>风险 / 原规则 / 待确认</summary><p>${esc(i.agent_decision)}：${esc(i.agent_reason)}</p><ul>${r.missing.map(t=>'<li>'+esc(t)+'</li>').join('')}</ul><p>心理价：未给出；缺少可靠价格依据。</p></details></article>`}).join('')||'<p class="empty">本组暂无商品。</p>';}
for(let id of ['bucket','platform','search'])$(id).addEventListener('input',()=>{page=0;draw()});$('prev').onclick=()=>{page--;draw();window.scrollTo(0,0)};$('next').onclick=()=>{page++;draw();window.scrollTo(0,0)};draw();</script></html>'''
    (OUT/'打开初筛结果.html').write_text(page.replace('__DATA__', data))

if __name__ == '__main__': main()
