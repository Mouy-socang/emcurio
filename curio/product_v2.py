"""Evidence-linked V2 candidates and Chinese title previews, never automatic verification."""
import datetime as dt
import hashlib
import json
from pathlib import Path
import re
from .rules import damage_statements  # 规则归口（AD-15）；行为不变

ROOT=Path(__file__).resolve().parents[1]
FIELDS=json.loads((ROOT/'docs/product-fields-v2.json').read_text())['fields']
def match(p,t):return re.search(p,t or '',re.I)
def num(s):return float(s.replace(',','.'))

def seller_grade_claim(title, description):
    claims=[]
    pattern=r'(?<!\w)(?:([12])\s*(?:[.\-]|te[nrms]?)?\s*(?:wahl\b|w\.(?!\w))|(erste[nrms]?|zweite[nrms]?)\s+wahl\b)|([一二])等'
    for source,text in [('title',title),('description',description)]:
        for m in re.finditer(pattern,text,re.I):
            before=text[max(0,m.start()-25):m.start()]
            if re.search(r'\b(?:nicht|keine?|ohne)\s*$',before,re.I):continue
            grade=('first' if m[1]=='1' or (m[2] or '').lower().startswith('erste') or m[3]=='一' else 'second')
            claims.append(dict(value=grade,raw_text=m[0],source=source))
    values={c['value'] for c in claims}
    label={'first':'一等','second':'二等'}.get(next(iter(values)),'') if len(values)==1 else ('等级待核（一／二等均有记载）' if values else None)
    return dict(label=label,value=next(iter(values)) if len(values)==1 else 'unknown',claims=claims,source='卖家描述')

# Ordered, explicit language mappings. Values are extraction candidates, not authentication.
FORMS=[
 ('ashtray','personal','烟灰缸',r'aschenbecher'),('inkwell','writing','墨水台',r'tintenfass'),
 ('pipe_bowl','personal','烟斗头',r'pfeifenkopf'),('thimble','personal','顶针',r'fingerhut|thimble'),
 ('brooch','jewelry','胸针',r'brosche'),('ring','jewelry','戒指',r'\bring\b'),
 ('pendant','jewelry','吊坠',r'anhänger|\bkette\b'),('pen','writing','笔',r'kugelschreiber|roller pen'),
 ('egg_cup','tableware','蛋杯',r'eierbecher'),('candleholder','decorative','烛台',r'leuchter|kerzenhalter|kerzenständer'),
 ('sauce_boat','tableware','酱料船',r'sauciere'),('salt_cellar','tableware','盐碟',r'saliere|salziere'),
 ('sugar_bowl','tableware','糖罐',r'zuckerdose'),('tureen','tableware','汤盅',r'terrine'),
 ('lidded_box','decorative','盖盒',r'deckeldose|deckelsose|deckel\s+dose|dose.{0,12}mit deckel|dose -|bonboniere'),
 ('lidded_bowl','tableware','盖碗',r'deckelschale'),('jar','tableware','带盖罐',r'topf mit deckel'),
 ('tea_caddy','tableware','茶叶罐',r'teedose'),('box','decorative','盒',r'\bdose\b'),
 ('vase','decorative','花瓶',r'vasen?\b'),('carafe','tableware','酒瓶',r'karaffe'),
 ('plaque','writing','瓷板',r'plakette|bildplatte|gedenkplatte|porzellanplatte'),
 ('medal','decorative','纪念章',r'medaille'),('token','decorative','瓷币',r'notmünzen|notgeld|kriegsgeld'),
 ('tile','decorative','装饰瓷砖',r'fliese|kachel'),
 ('figurine','sculpture','塑像',r'figur|vogelgruppe|kakadu|tukan|seidenschwanz|jagdhund|vorstehhund|hentschelkind|komödiant|tubaspieler|clown|affe aus'),
 ('service','mixed','餐具套组',r'kaffee.?service|kaffeservice|tee.?service|speise.?service|speise service|essgeschirr|geschirr set|kaffee.set|rauchergarnitur|likörset|konvolut|service'),
 ('cup_set','tableware','杯组',r'gedeck|tassen?.{0,25}untertassen?'),
 ('cup','tableware','杯',r'(?<!unter)tasse|walzenkrug'),('saucer','tableware','杯托',r'untertasse'),
 ('coffee_pot','tableware','咖啡壶',r'kaffeekanne'),('teapot','tableware','茶壶',r'teekanne'),
 ('tray','tableware','托盘',r'tablett'),('cake_platter','tableware','蛋糕盘',r'tortenplatte|kuchenplatte'),
 ('serving_platter','tableware','餐用大盘',r'servierplatte|servierteller|\bplatte\b'),
 ('soup_plate','tableware','汤盘',r'suppenteller'),('dinner_plate','tableware','餐盘',r'speiseteller'),
 ('dessert_plate','tableware','点心盘',r'kuchenteller|dessert\s*teller|gebäckteller|brotteller'),
 ('wall_plate','decorative','挂盘',r'wandteller|zierteller|sammelteller|regimentsteller|märchenteller|jubiläumsteller'),
 ('plate','tableware','盘',r'teller'),('footed_bowl','tableware','高足碗',r'fußschale|tafelaufsatz'),
 ('bowl','tableware','碗',r'schale|schälchen|schüssel'),
]
PATTERNS=[
 ('蓝洋葱',r'zwiebelmuster'),('印度花',r'indisch'),('明龙',r'ming\s*drache'),
 ('宫廷龙',r'hofdrache'),('繁饰龙',r'reicher?\s+drache'),('龙纹',r'drache'),
 ('黄玫瑰',r'gelbe.?\s*rose'),('红玫瑰',r'rote.?\s*rose|meissener rose'),('黑玫瑰',r'schwarze.?\s*rose'),
 ('玫瑰',r'rose'),('葡萄藤',r'weinlaub|weillaub'),('散花',r'streubl'),
 ('花束',r'blumenbu(?:k|c)k?ett|blumenbouquet'),('蓝花',r'blaue\s+blume'),
 ('四号斜花',r'blume\s*4\s*schräg'),('德国花卉',r'deutsche\s+blume'),
 ('草地花卉',r'wiesenblumen'),('柿右卫门',r'kakiemon|kaki?emon'),
 ('稻草花',r'strohblume'),('雪团花',r'schneeball'),('花卉',r'blumen|blume|floral|bunte bume'),
]
SUBJECTS=[('金翅雀',r'erlenzeisig'),('山雀',r'meisen\b'),('凤头鹦鹉',r'kakadu'),('巨嘴鸟',r'tukan|pfefferfresser'),
 ('太平鸟',r'seidenschwanz'),('鹳与婴儿',r'storch mit baby'),('鹳',r'storch'),('公鸡',r'hahn'),
 ('驴',r'\besel\b'),('爱神',r'\bamor\b'),('猎犬',r'jagdhund|vorstehhund'),('锦鲤',r'\bkoi\b'),
 ('嬉戏的熊',r'spielende bären'),('嬉戏的猫',r'spielende katzen'),('弹曼陀林的喜剧演员',r'komödiant mit mandoline'),
 ('持蜡烛的幽灵',r'geist mit kerze'),('蒂尔恶作剧者',r'till eulenspiegel'),
 ('抱娃娃的儿童',r'hentschelkind mit puppe'),('持杯儿童',r'hentschelkind mit tasse'),
 ('吹大号的小丑',r'tubaspieler|clown mit tuba'),('拉手风琴的小丑',r'clown mit ziehharmonika'),
 ('猴乐队手摇风琴演奏者',r'affe.*drehorgelspielerin'),('猴乐队指挥乐谱架',r'notenpult.*affenkapelle'),
 ('小穆克',r'der kleine muck'),('和平战胜战争',r'der frieden besiegt den krieg'),
 ('威尼斯总督宫',r'dogenpalast'),('缪斯',r'musen'),('红醋栗',r'johannisbeeren'),
 ('岩石与鸟',r'fels.{0,8}vogel'),('水果',r'obst.{0,8}früchte'),('蝴蝶',r'schmetterling|schmettlinge'),
 ('昆虫',r'insekten?'),('花卉',r'blumenmotiv'),('双剑',r'schwerterdekor'),
 ('草莓盖钮',r'erdbeer.knauf'),('莫里茨堡',r'schloss moritzburg'),('皮尔尼茨宫',r'schloss pillnitz'),
 ('柏林750周年',r'750 jahre berlin'),('梅森200周年',r'200 jahre 1710'),('高斯',r'carl friedrich gauss'),
 ('约翰内斯·多伯施泰因',r'johannes dobberstein')]
COLORS=[('紫红',r'purpur'),('松石绿',r'türkis'),('钴蓝',r'kobalt|cobalt'),('绿',r'grün'),('红',r'\brot(?:e[rsnm]?)?\b|roter'),('黄',r'gelb'),('黑',r'schwarz'),('白',r'weiß|weiss'),('蓝',r'blau'),('彩色',r'bunt')]

def extract(item, detail=None):
    detail=detail or {};title=item['title'];description=detail.get('description') or item.get('description') or ''
    stamp=dt.datetime.now(dt.timezone.utc).isoformat();pid='CP-'+hashlib.sha256((item['platform']+':'+item['item_id']).encode()).hexdigest()[:20]
    p={f['field']:None for f in FIELDS}
    p.update(product_id=pid,raw_title=title,raw_description=description,source_types=['marketplace_listing'],review_status='unreviewed',created_at=item['first_seen'],updated_at=stamp,schema_version='2.0',external_refs=[dict(source_system=item['platform'],external_id=item['item_id'],source_location=item['url'])],factory_grade='unknown',decoration_origin='unknown',component_list_status='not_recorded',completeness_status='unknown',pairing_status='unknown',condition_inspection_status='not_inspected')
    facts=dict(p);ev=[dict(evidence_id=pid+'-title',kind='seller_title',text=title,url=item['url']),dict(evidence_id=pid+'-description',kind='seller_description',text=description,url=item['url'])];inferences=[];notes=[];warnings=[]
    def put(k,v,source='title',confidence='medium'):
        p[k]=v
        inferences.append(dict(product_id=pid,attribute_path=k,candidate_value=v,evidence_ids=[pid+'-'+source],confidence=confidence,model='curio-v2-text-rules-1',created_at=stamp,review_status='unreviewed',reviewed_at=None))
    aspects={x['name']:x['value'] for x in detail.get('aspects',[]) if x.get('name') and x.get('value')}
    for key,value in aspects.items():ev.append(dict(evidence_id=pid+'-aspect:'+key,kind='seller_aspect',text=value,url=item['url']))
    maker=None
    if match(r'teichert',title):maker='Teichert'
    elif match(r'sörnewitz|sornewitz',title):maker='Sörnewitz'
    elif match(r'E\.\s*M\.\s*Schmidt',title):maker='E. M. Schmidt'
    elif match(r'herend.*meissen|meißen und schönwald',title):notes.append('涉及多个品牌，整组制造者留空')
    elif match(r'meissen|meißen|meissner|meißner|meissener|meißener|\bmeisse\b',title):maker='Meissen'
    if maker:put('maker',maker)
    if aspects.get('Marke')=='Rosenthal' and maker=='Meissen':
        maker=None;put('maker',None);warnings.append('品牌冲突');notes.append('标题称 Meissen，但卖家品牌字段为 Rosenthal，制造者留空待核')
    if aspects.get('Marke')=='Saxonia Bing':
        maker='Saxonia / BING';put('maker',maker,'aspect:Marke');notes.append('Saxonia 瓷蛋与 BING 泰迪组合；Meissen 在此不能作为梅森瓷厂归属依据')
    typ=next((x for x in FORMS if match(x[3],title)),None)
    if not typ and aspects.get('Produktart'):typ=next((x for x in FORMS if match(x[3],aspects['Produktart'])),None)
    # No a priori inference that an unidentified porcelain object is a figurine or a set.
    cname='瓷器（器型待识别）'
    if typ:
        code,family,cname,_=typ;put('object_type',code);put('object_family',family)
        if code=='service':put('sale_unit_type','mixed_set');put('object_type','mixed_set')
    else:notes.append('缺少可靠器型信息，不能作为正式上架标题')
    if typ and typ[0]=='figurine' and match('notenpult',title):cname='乐谱架配件';put('object_type','music_stand');put('sale_unit_type','part')
    if match('teddy',title):cname='泰迪熊与瓷蛋组合';put('object_type','mixed_set');put('object_family','mixed');put('sale_unit_type','mixed_set');notes.append('混合材质组合，需核实瓷蛋品牌与出售范围')
    if match('streublumen set kaffeekanne tassen vase',title):cname='咖啡壶、杯与花瓶组合';put('object_type','mixed_set');put('sale_unit_type','mixed_set')
    if match('2 plaketten & 1 brosche',title):cname='瓷板与胸针组合';put('object_type','mixed_set');put('sale_unit_type','mixed_set')
    if match(r'schalen\s*/\s*untertassen',title):cname='碗或杯托（器型待核）';put('object_type',None)
    elif match(r'teller schale',title):cname='盘或碗（器型待核）';put('object_type',None)
    if typ and typ[0]=='cup_set':
        cname='杯碟' if match(r'tassen?.{0,25}untertassen?',title) else '杯组'
    uses=[]
    if match(r'mokka|mocca|espresso',title):uses=['摩卡咖啡']
    elif match(r'kaffee|kaffe[gs]',title):uses=['咖啡']
    if match(r'teetasse|teegedeck|teeservice|kaffee/tee',title):uses.append('茶')
    if uses:put('intended_uses',uses)
    if len(uses)==1 and cname in ('杯','杯碟','杯组','餐具套组'):cname=uses[0]+cname
    mats=[];mt=title+' '+aspects.get('Material','')
    for cn,pat in [('炻器',r'böttger.?steinzeug|steinzeug'),('陶器',r'steingut|keramik'),('铅水晶',r'bleikristall'),('瓷',r'porzellan|porcelain')]:
        if match(pat,mt):mats.append(cn)
    if '炻器' in mats:mats=[x for x in mats if x!='瓷']
    if mats:put('materials',mats,'title' if match('porzellan|porcelain|steinzeug|steingut|keramik|bleikristall',title) else 'aspect:Material')
    if aspects.get('Ursprungsland'):put('country_of_origin',aspects['Ursprungsland'],'aspect:Ursprungsland')
    # Dates in medals, model numbers and edition anniversaries are not production years.
    if typ and typ[0] not in ('medal','token','plaque','wall_plate'):
        m=match(r'(?<!\d)((?:17|18|19|20)\d{2})\s*[-–]\s*((?:17|18|19|20)\d{2})(?!\d)',title)
        a=match(r'\b(um|ca\.?)\s*((?:17|18|19|20)\d{2})(?!\d)',title)
        if m:put('date_start_year',int(m[1]));put('date_end_year',int(m[2]));put('date_label',m[1]+'–'+m[2]+'年')
        elif a:put('date_label','约'+a[2]+'年')
        elif match('knaufzeit',title):put('date_label','Knauf时期')
        elif match(r'pfeiffer?[- ]?(?:zeit|时期)|pfeiffer',title):put('date_start_year',1924);put('date_end_year',1934);put('date_label','1924–1934年')
        elif match('marcolini',title):put('date_label','马科利尼时期')
    grade=match(r'(?<!\d)([12])\s*[.\-]?\s*wahl|erste\s+wahl|zweite\s+wahl',title)
    if grade:notes.append('卖家等级陈述：'+grade[0]+'；未经核验，不写入厂家等级或中文标题')
    elif match(r'(?<!\d)([12])\s*[.\-]?\s*wahl',description):notes.append('卖家详情等级陈述：'+match(r'(?<!\d)([12])\s*[.\-]?\s*wahl',description)[0]+'；未经核验，不写入厂家等级或中文标题')
    marks=[]
    for pat,kind in [(r'\d+\s*schleifstriche','incision'),(r'schwertermarke|knaufschwerter','maker_mark')]:
        m=match(pat,title)
        if m:marks.append(dict(mark_id=pid+'-mark'+str(len(marks)),component_id=None,kind=kind,location=None,raw_text=m[0],observed_features=None,interpretation=None,verification_status='unreviewed',evidence_ids=[pid+'-title']))
    if marks:put('marks',marks)
    if aspects.get('Bodenmarke'):
        put('marks',(p['marks'] or [])+[dict(mark_id=pid+'-aspect-mark',component_id=None,kind='maker_mark',location='底部（卖家字段）',raw_text=aspects['Bodenmarke'],observed_features=None,interpretation=None,verification_status='unreviewed',evidence_ids=[pid+'-aspect:Bodenmarke'])],'aspect:Bodenmarke')
    pattern=next((cn for cn,pat in PATTERNS if match(pat,title)),None)
    colors=[cn for cn,pat in COLORS if match(pat,title)]
    if colors:put('colors',colors)
    if pattern:
        if pattern in ('印度花','明龙','宫廷龙','繁饰龙','龙纹'):
            lead=next((c for c in colors if c in ('紫红','松石绿','绿','红','黄','黑','蓝')),None)
            if lead:pattern=lead+pattern
        put('pattern_name',pattern)
    if match(r'indisch.{0,15}fels.{0,8}vogel',title):pattern='印度岩石鸟纹';put('pattern_name',pattern)
    subjects=[]
    for cn,pat in SUBJECTS:
        if match(pat,title):
            if cn=='鹳' and '鹳与婴儿' in subjects:continue
            if cn=='岩石与鸟' and pattern=='印度岩石鸟纹':continue
            subjects.append(cn)
    if subjects:put('subjects',subjects)
    model=match(r'\b([ABIJX])\s*-?\s*form\b',title)
    if model:put('model_name',model[1].upper()+'-Form')
    elif match('neuer ausschnitt',title):put('model_name','Neuer Ausschnitt')
    for name in ('Aquitania','Aquatinta','Krokusservice','Affenkapelle','Clownskapelle'):
        if match(name,title):put('series_name',name)
    modelnum=match(r'(?:modell\s*nummer|modellnr\.?|mod\.?|hnr)\s*([A-Z]?\s*\d+)',title)
    if modelnum:put('model_number',re.sub(r'\s+','',modelnum[1]))
    elif match(r'\b6065[35]\b',title):put('model_number',match(r'\b6065[35]\b',title)[0])
    styles=[cn for cn,pat in [('巴洛克',r'barock|barok'),('新艺术',r'jugendstil')] if match(pat,title)]
    if styles:put('styles',styles)
    forms=[cn for cn,pat in [('镂空',r'durchbruch'),('叶形',r'blattschale'),('贝壳形',r'muschelschale'),('波浪边',r'wellenrand|gewellter rand'),('椭圆',r'oval'),('方形',r'eckig'),('喇叭形',r'trompeten|trichtervase'),('窄颈',r'enghals'),('带足',r'füsschen|fußvase|mit henkel & fuß'),('蛇形柄',r'schlangenhenkel'),('双耳',r'kratervase'),('多瓣',r'mehrpassig')] if match(pat,title)]
    if forms:put('form_styles',forms)
    if match(r'relief',title):put('has_relief',True)
    if match(r'goldrand',title):put('has_gilding',True);put('has_gilded_rim',True)
    elif match(r'goldstaff|vergold|goldverzierung|goldrelief|mit gold|goldpunkten',title):put('has_gilding',True)
    if match(r'handbemalt',title):put('decoration_techniques',['手绘'])
    if match(r'goldgräser',title):put('gilding_motifs',['草纹'])
    if match(r'schwarzer fonds',title):put('ground_color','黑')
    # Preserve ambiguous sizes rather than assigning an unlabelled length to height or diameter.
    measurements=re.findall(r'\d+(?:[.,]\d+)?(?:\s*(?:cm)?\s*[x×]\s*\d+(?:[.,]\d+)?){0,2}\s*(?:cm|mm)',title,re.I)
    if measurements:put('measurement_notes','原文尺寸：'+'；'.join(measurements)+'；未注明维度者待核')
    for field,pat in [('height_cm',r'(?:höhe|hoch|\bH\.?\s*[:=]?)\s*(?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*cm'),('diameter_cm',r'(?:durchmesser|durchm\.?|[Øø⌀])\s*(?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*cm')]:
        m=match(pat,title)
        if m:put(field,num(m[1]))
    m=match(r'(\d+(?:[.,]\d+)?)\s*cm\s*(?:hoch|höhe|\bH\b)',title)
    if m:put('height_cm',num(m[1]))
    # Explicitly labelled seller dimensions can enrich single objects from details.
    if typ and typ[0] not in ('service','cup_set') and not match(r'\bset\b|konvolut|\d+\s*(?:stück|teile)',title):
        for field,pat in [('height_cm',r'(?:höhe|\bH\.)\s*[:=]?\s*(?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*cm'),('diameter_cm',r'(?:durchmesser|[Øø⌀])\s*[:=]?\s*(?:ca\.?\s*)?(\d+(?:[.,]\d+)?)\s*cm')]:
            m=match(pat,description)
            if m and p[field] is None:put(field,num(m[1]),'description')
    count=match(r'(\d+)\s*(?:[- ]?(?:tlg\.?|teilig|teile|stück|stk\.?))',title)
    if count:put('sale_piece_count',int(count[1]));put('sale_unit_type','set' if p['object_type']!='mixed_set' else 'mixed_set')
    persons=match(r'(\d+)\s*personen',title)
    if persons:put('service_count',int(persons[1]))
    # Only explicitly enumerated members count; "2-tlg Gedeck" alone is insufficient.
    members=[]
    for t,pat in [('cup',r'(?<!\d)(\d+)\s*(?:kaffee|espresso)?tassen'),('saucer',r'(?<!\d)(\d+)\s*untertassen')]:
        m=match(pat,title)
        if m:members.append((t,int(m[1])))
    if len(members)==2:
        put('components',[dict(component_id=pid+'-'+t,type=t,quantity=n,children=None,evidence_ids=[pid+'-title']) for t,n in members]);put('component_list_status','complete');put('sale_unit_type','set');put('sale_piece_count',sum(n for _,n in members));put('physical_piece_count',sum(n for _,n in members))
    elif typ and typ[0]=='cup_set' and match(r'\btasse\s+mit\s+untertasse\b',title) and not match(r'\b\d+\s*x|\b\d+\s+meissen',title):
        put('components',[dict(component_id=pid+'-'+t,type=t,quantity=1,children=None,evidence_ids=[pid+'-title']) for t in ('cup','saucer')]);put('component_list_status','complete');put('sale_unit_type','set');put('sale_piece_count',2);put('physical_piece_count',2)
    if match(r'1v\.2|1\.\s*wahl/vase|1\.wahl/vase',title):put('sale_unit_type','single');notes.append('原文按单只花瓶出售，不按两只成对计数')
    lidtext=re.sub(r'(?:höhe|hoch|gemessen|maße)\s*(?:[:=]|ca\.?)?\s*(?:\d+(?:[.,]\d+)?\s*cm\s*)?ohne deckel','',title+' '+description,flags=re.I)
    if match(r'ohne deckel|deckel fehlt|deckel fehlt',lidtext):put('completeness_status','incomplete','description');warnings.append('缺盖')
    # Negative mentions and generic disclaimers must not become damage allegations.
    damage=[]
    for cn,s,source in damage_statements(title,description):damage.append(cn);notes.append('品相原文：'+s)
    if damage:
        put('defect_types',damage,'description' if description else 'title');warnings+=damage
        if '修复' in damage:put('has_restoration',True,'description' if description else 'title')
        if '磕口' in damage:put('has_chips',True,'description' if description else 'title')
    if detail.get('condition'):notes.append('平台品相陈述：'+detail['condition'])
    if warnings:put('condition_notes','卖家文字线索：'+'、'.join(warnings)+'；须逐图复核','description' if description else 'title')
    # Enrich only explicitly labelled seller fields; retain conflicts rather than overwriting them.
    for field,key in [('height_cm','Höhe'),('width_cm','Breite'),('diameter_cm','Durchmesser')]:
        if p[field] is None and match(r'^\s*(?:ca\.?\s*)?\d+(?:[.,]\d+)?\s*cm\s*$',aspects.get(key,'')):
            put(field,num(match(r'\d+(?:[.,]\d+)?',aspects[key])[0]),'aspect:'+key)
    if p['object_type'] not in ('mixed_set','cup_set') and not p['components']:
        m=match(r'(\d+(?:[.,]\d+)?)\s*cm\s*hoch',description)
        if m and p['height_cm'] is None:put('height_cm',num(m[1]),'description')
    if p['sale_unit_type'] in ('set','mixed_set') and p['components'] is None:
        members=[]
        types=[('cup',r'(?:kaffee|mocca|mokka|espresso)?tassen?'),('saucer',r'untertassen?'),('plate',r'(?:kuchen|speise|supp(?:en)?|dessert)?teller'),('coffee_pot',r'kaffeekannen?'),('sugar_bowl',r'zuckerdosen?'),('creamer',r'sahnekrug|milchkännchen')]
        for ty,component_pattern in types:
            for m in re.finditer(r'(?<![\d.,])([1-9]\d?)\s*(?:x\s*)?('+component_pattern+r')\b',description,re.I):
                if any(c['type']==ty and c['quantity']==int(m[1]) for c in members):continue
                members.append(dict(component_id=pid+'-descc'+str(len(members)),type=ty,quantity=int(m[1]),children=None,notes='卖家原文：'+m[0],evidence_ids=[pid+'-description']))
        if members:put('components',members,'description');put('component_list_status','partial','description');notes.append('组件仅登记描述中明确提及的数量，未确认已穷尽全部部件，物理件数留空')
    if not p['date_label'] and aspects.get('Herstellungsjahr'):
        yr=aspects['Herstellungsjahr'];m=match(r'^((?:17|18|19|20)\d{2})(?:\s*[-–]\s*((?:17|18|19|20)\d{2}))?$',yr)
        if m:put('date_start_year',int(m[1]),'aspect:Herstellungsjahr');put('date_end_year',int(m[2] or m[1]),'aspect:Herstellungsjahr');put('date_label',(m[1]+'–'+m[2] if m[2] else m[1])+'年','aspect:Herstellungsjahr')
    # Specific, reviewed mappings of today's ambiguous seller descriptions.
    if 'schönwald' in title.lower() and 'zehn Teilen' in description:
        put('object_type','mixed_set','description');put('object_family','mixed','description');put('sale_unit_type','mixed_set','description');put('pairing_status','mixed','description');put('sale_piece_count',10,'description');put('component_list_status','complete','description');put('physical_piece_count',10,'description');put('pattern_name','散花','description');pattern='散花';cname='梅森与申瓦尔德杯、杯托及点心盘组合';warnings.append('混配')
        members=[('Meissen','cup',2),('Meissen','saucer',1),('Meissen','dessert_plate',1),('Schönwald','cup',1),('Schönwald','saucer',2),('Schönwald','dessert_plate',3)]
        put('components',[dict(component_id=pid+'-'+str(n),type=t,maker=mk,quantity=q,children=None,evidence_ids=[pid+'-description']) for n,(mk,t,q) in enumerate(members)],'description')
    if item['item_id']=='ka:3533311358':
        cname='小壶、酱料船与花瓶组合';put('object_type','mixed_set','description');put('object_family','mixed','description');put('sale_unit_type','mixed_set','description');put('sale_piece_count',3,'description');put('component_list_status','partial','description')
        put('components',[dict(component_id=pid+'-'+t,type=t,quantity=1,children=None,evidence_ids=[pid+'-description']) for t in ('pot','sauce_boat','vase')],'description')
        notes.append('卖家列出三件主体，壶盖等可分离部件未核验，物理件数留空')
    if item['item_id']=='ka:3532646541' or ('Prunk Moccatasse' in title and 'Untertasse' in description):
        cname='摩卡咖啡杯碟' if 'Moccatasse' in title else '杯碟';put('object_type','cup_set','description');put('sale_unit_type','set','description');put('sale_piece_count',2,'description');put('component_list_status','complete','description');put('physical_piece_count',2,'description')
        put('components',[dict(component_id=pid+'-'+t,type=t,quantity=1,children=None,evidence_ids=[pid+'-description']) for t in ('cup','saucer')],'description')
        # Measurements of the cup alone must not be presented as whole-set dimensions.
        if 'Moccatasse' in title:
            put('height_cm',None);put('diameter_cm',None);put('measurement_notes','卖家：杯高约5厘米、口径约7.6厘米；杯托直径约12.7厘米','description')
            p['components'][0]['measurements']=[dict(dimension='height',value=5,unit='cm',is_approximate=True),dict(dimension='diameter',value=7.6,unit='cm',is_approximate=True)];p['components'][1]['measurements']=[dict(dimension='diameter',value=12.7,unit='cm',is_approximate=True)]
            put('components',p['components'],'description')
    if item['item_id']=='v1|237110855918|0':
        warnings.append('数量冲突：标题6件、详情6杯6托');put('sale_piece_count',None);put('physical_piece_count',None);put('component_list_status','partial','description');notes.append('出售数量存在冲突，不能将其作为已确认的6件套或12件套')
        cname='咖啡杯碟组合'
    if item['item_id']=='ka:3533151401':
        cname='盘（多款可单卖，出售范围待核）';put('object_type','plate','description');put('object_family','tableware','description');notes.append('详情说明可单卖；不把图片中全部商品视为一套')
    if item['item_id']=='ka:2502031242':warnings.append('手指及头发处小缺损');notes.append('卖家称 Etagere，描述同时出现手指及头发，实际器型需看图确认')
    brand={'Meissen':'梅森','Teichert':'泰希特','Sörnewitz':'瑟讷维茨','E. M. Schmidt':'施密特','Saxonia / BING':'萨克索尼亚／宾氏'}.get(maker,'品牌待核')
    decor=''.join(dict.fromkeys(([pattern] if pattern else [])+subjects))
    distinctive=[c for c in colors if c in ('钴蓝','松石绿','紫红') and c not in decor]
    if distinctive:decor=''.join(distinctive)+'色'+decor
    if p['gilding_motifs']:decor+='描金草纹'
    elif p['has_gilded_rim']:decor+='金边'
    elif p['has_gilding']:decor+='描金'
    if forms:decor+=''.join(forms)
    if p['has_relief']:decor+='浮雕'
    if p['model_name']:decor+=p['model_name']
    size=[]
    if p['height_cm']:size.append('高'+format(p['height_cm'],'g')+'厘米')
    if p['diameter_cm']:size.append('直径'+format(p['diameter_cm'],'g')+'厘米')
    if not size and measurements:size.append('尺寸'+measurements[0].replace('cm','厘米').replace('mm','毫米').replace(',','.')+'（维度待核）')
    if p['physical_piece_count'] and p['components']:
        names={'cup':'杯','saucer':'托','dessert_plate':'点心盘'}
        if len(p['components'])<=3:size.append('、'.join(str(c['quantity'])+names.get(c['type'],c['type']) for c in p['components']))
        else:size.append(str(p['sale_piece_count'])+'件')
    elif p['sale_piece_count']:size.append(str(p['sale_piece_count'])+'件（组成待核）')
    if p['service_count']:size.append(str(p['service_count'])+'人份')
    if p['model_number']:size.append('型号'+p['model_number'])
    grade_claim=seller_grade_claim(title,description)
    if grade_claim['value']!='unknown':
        put('factory_grade',grade_claim['value'],grade_claim['claims'][0]['source'])
    # User's 2026-10-06 correction: borrow seller grades, lead with condition disclosures.
    notes=[n for n in notes if not n.startswith('卖家等级陈述：') and not n.startswith('卖家详情等级陈述：')]
    if grade_claim['label']:notes.append('厂家等级沿用卖家原文：'+grade_claim['label']+'；不是当前品相评分')
    front=[w for w in warnings if w in ('破损','修复','磕口','裂纹','需修理','金彩磨损','缺盖') or '缺损' in w]
    tail=[w for w in warnings if w not in front]
    preview=('【'+'、'.join(front)+'】' if front else '')+''.join(x for x in [p['date_label'],brand,grade_claim['label'],decor,cname] if x)
    if size:preview+='，'+'，'.join(size)
    if tail:preview+='，'+'、'.join(tail)
    p['product_title']=None # Only an owner-confirmed title belongs to Product.
    notes.append('中文标题为卖家资料生成的待核预览；未经图片、底款和品相核验，不是正式上架标题')
    return dict(product=facts,candidate_product=p,seller_grade=grade_claim,object_label_zh=cname,title_preview_zh=preview,title_status='据卖家描述整理',title_ready_for_listing=False,evidence=ev,inferences=inferences,notes=list(dict.fromkeys(notes)),warnings=warnings,field_count=len(p),vocabulary_version='curio-local-v2-candidate-1')
