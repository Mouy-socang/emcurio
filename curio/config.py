"""Local configuration. Credentials never enter config objects or database."""
from pathlib import Path
import math
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_AI_REQUESTS_PER_DAY = 100

def load(root=ROOT):
    root = Path(root)
    settings = yaml.safe_load((root/'config/settings.yaml').read_text()) or {}
    defaults = dict(keywords=['Meissen'],auction_fee_rate=0.0,cheap_damaged_max=None,
                    daily_budget=0,kleinanzeigen_enabled=True,status_check_limit=20,pool_check_hours=6)
    defaults.update(settings)
    # AD-27：每平台每日接口调用预算；缺省项用默认值，未知平台或非法数值直接报错。
    from .schedule import DEFAULT_BUDGET
    budget=settings.get('api_budget')
    if budget is None:budget={}
    if not isinstance(budget,dict):raise ValueError('api_budget 必须为 平台: 次数 的映射')
    unknown=set(budget)-set(DEFAULT_BUDGET)
    if unknown:raise ValueError('api_budget 含未知平台：'+','.join(sorted(map(str,unknown))))
    for platform,value in budget.items():
        if type(value) is not int or value<0:raise ValueError('api_budget.'+platform+' 必须为非负整数')
    defaults['api_budget']={**DEFAULT_BUDGET,**budget}
    # CAP-4 / AD-20：AI 打分每日请求上限（独立于平台预算，按柏林日期计数）。
    ai=settings.get('ai_budget')
    if ai is None:ai={}
    if not isinstance(ai,dict):raise ValueError('ai_budget 必须为映射')
    unknown=set(ai)-{'requests_per_day'}
    if unknown:raise ValueError('ai_budget 含未知键：'+','.join(sorted(map(str,unknown))))
    per_day=ai.get('requests_per_day',DEFAULT_AI_REQUESTS_PER_DAY)
    if type(per_day) is not int or per_day<0:raise ValueError('ai_budget.requests_per_day 必须为非负整数')
    defaults['ai_budget']={'requests_per_day':per_day}
    if not isinstance(defaults['keywords'],list) or not defaults['keywords'] or any(not isinstance(k,str) or not k.strip() for k in defaults['keywords']):
        raise ValueError('keywords 必须为非空文字列表')
    for key in ('auction_fee_rate','daily_budget','cheap_damaged_max'):
        value=defaults[key]
        if key=='cheap_damaged_max' and value is None:continue
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError(key+' 必须为非负数')
    if type(defaults['status_check_limit']) is not int or not 0<=defaults['status_check_limit']<=300:
        raise ValueError('status_check_limit 必须为 0–300')
    # 故事 26：商品池（留下、在售）的货距上次检查满这么多小时才在普通轮转之前优先再查。
    if type(defaults['pool_check_hours']) is not int or not 1<=defaults['pool_check_hours']<=48:
        raise ValueError('pool_check_hours 必须为 1–48 的整数')
    for name in ('price_rules','slow_sellers'):
        value=yaml.safe_load((root/'config'/f'{name}.yaml').read_text()) or {}
        rules=value.get('rules',[])
        if not isinstance(rules,list):raise ValueError(name+' rules 必须为列表')
        for rule in rules:
            re.compile(rule['pattern'],re.I)
            if not rule.get('reason'):raise ValueError('规则缺少 reason')
            if name=='price_rules':
                if rule.get('basis') not in ('listed_price','total_price'):raise ValueError('价格规则需选择 basis')
                cap=rule.get('max_price')
                if isinstance(cap,bool) or not isinstance(cap,(int,float)) or not math.isfinite(cap) or cap<0:raise ValueError('max_price 无效')
                if not rule.get('currency'):raise ValueError('价格规则缺少币种')
        defaults[name]=rules
    # CAP-16：EM 西洋瓷器知识库路径（只读词表来源）；展开 ~，不存在时不报错（标准名称记 kb_status=missing）。
    from .judge import DEFAULT_KB_PATH
    kb=defaults.get('em_kb_path') or DEFAULT_KB_PATH
    if not isinstance(kb,str) or not kb.strip():raise ValueError('em_kb_path 必须为文字路径')
    defaults['em_kb_path']=str(Path(kb.strip()).expanduser())
    # CAP-10：预估费用档位（config/fee_tiers.yaml，数据；推断在 judge.fee_tier）。
    defaults['fee_tiers']=fee_tiers(root)
    # CAP-14：GoodCustomer 资料库与 gcdb.py 路径（展开 ~；不存在时不报错，06 客人提示找不到资料库）。
    defaults['goodcustomer']=goodcustomer(settings.get('goodcustomer'))
    # 故事 30：发布快照版本（1 = snapshot.v1；2 = snapshot.v2，带拍卖榜）。服务器部署能读 v2 之前保持 1。
    defaults['publish']=publish_settings(settings.get('publish'))
    # 故事 32：拍卖行名单与采集设置。
    defaults['auction_houses']=auction_houses(settings.get('auction_houses'))
    defaults['auction_house_bot']=auction_house_bot(settings.get('auction_house_bot'))
    return defaults

SNAPSHOT_VERSIONS=(1,2)

def publish_settings(value):
    """校验 settings.yaml 的 publish 段：{snapshot_version: 1 或 2（缺省 1）, allow_auction_house: 布尔（缺省 false）}。"""
    if value is None:value={}
    if not isinstance(value,dict):raise ValueError('publish 必须为映射（snapshot_version）')
    unknown=set(value)-{'snapshot_version','allow_auction_house'}
    if unknown:raise ValueError('publish 含未知键：'+','.join(sorted(map(str,unknown))))
    version=value.get('snapshot_version',1)
    if type(version) is not int or version not in SNAPSHOT_VERSIONS:
        raise ValueError('publish.snapshot_version 必须为 1 或 2')
    allow=value.get('allow_auction_house',False)
    if type(allow) is not bool:raise ValueError('publish.allow_auction_house 必须为 true 或 false')
    return dict(snapshot_version=version,allow_auction_house=allow)

def _publish_section(root):
    try:settings=yaml.safe_load((Path(root)/'config/settings.yaml').read_text()) or {}
    except FileNotFoundError:settings={}
    return publish_settings(settings.get('publish') if isinstance(settings,dict) else None)

def allow_auction_house(root=ROOT):
    """故事 32：拍卖行的货是否允许上拍卖榜（publish.allow_auction_house；缺省 false）。只读这一段。"""
    return _publish_section(root)['allow_auction_house']

# ---- 故事 32：拍卖行名单 ----

# skip（故事 34）：不值得看——瓷器少、停业或没有自己的网站；与条款结论（allowed / ask / forbidden）分开。
HOUSE_STATUSES=('allowed','ask','forbidden','skip')
# 条款禁止程序采集的平台：名单里出现时不得标为 allowed（双保险，代码也从不访问它们）。
NEVER_VISIT=frozenset({'lottissimo','the_saleroom','invaluable','liveauctioneers','catawiki'})
_HOUSE_KEY=re.compile(r'[a-z][a-z0-9_]{0,30}')
_HOUSE_FIELDS={'key','name','site','status','terms','adapter','enabled','interval','max_requests'}
HOUSE_MAX_REQUESTS=40   # 每家每次的请求上限缺省值（故事 34：名单里可按家改，1–200）

def auction_houses(value):
    """校验 auction_houses 名单：列表，每项 {key, name（中文显示名）, site, status, terms（条款结论一句）, adapter, enabled, interval?,
    max_requests?（每次最多请求数，1–200，缺省 40）}。"""
    if value is None:return []
    if not isinstance(value,list):raise ValueError('auction_houses 必须为列表')
    out,seen=[],set()
    for n,h in enumerate(value,1):
        where='auction_houses 第 %d 项'%n
        if not isinstance(h,dict):raise ValueError(where+' 必须为映射')
        unknown=set(h)-_HOUSE_FIELDS
        if unknown:raise ValueError(where+' 含未知键：'+','.join(sorted(map(str,unknown))))
        key=h.get('key')
        if not isinstance(key,str) or not _HOUSE_KEY.fullmatch(key):raise ValueError(where+' 的 key 无效')
        if key in seen:raise ValueError('auction_houses 里 key 重复：'+key)
        seen.add(key)
        for f in ('name','terms'):
            if not isinstance(h.get(f),str) or not h[f].strip():raise ValueError(where+' 缺少 '+f)
        site=h.get('site','')
        if not isinstance(site,str) or (site and not site.startswith('https://')):raise ValueError(where+' 的 site 必须为 https 网址或空')
        status=h.get('status')
        if status not in HOUSE_STATUSES:raise ValueError(where+' 的 status 必须为 allowed / ask / forbidden / skip')
        if key in NEVER_VISIT and status=='allowed':raise ValueError(key+' 的条款禁止程序采集，不能标为 allowed')
        for f in ('adapter','enabled'):
            if type(h.get(f,False)) is not bool:raise ValueError(where+' 的 '+f+' 必须为 true 或 false')
        interval=h.get('interval',5)
        if isinstance(interval,bool) or not isinstance(interval,(int,float)) or not math.isfinite(interval) or interval<5:
            raise ValueError(where+' 的 interval 必须为不小于 5 的秒数')
        cap=h.get('max_requests',HOUSE_MAX_REQUESTS)
        if type(cap) is not int or not 1<=cap<=200:raise ValueError(where+' 的 max_requests 必须为 1–200 的整数')
        out.append(dict(key=key,name=h['name'].strip(),site=site,status=status,terms=h['terms'].strip(),
                        adapter=h.get('adapter',False),enabled=h.get('enabled',False),interval=float(interval),max_requests=cap))
    return out

def auction_house_bot(value):
    """校验 auction_house_bot：{contact: 文字（附在 User-Agent 后，可空）, window_days: 1–365（缺省 90）}。"""
    if value is None:value={}
    if not isinstance(value,dict):raise ValueError('auction_house_bot 必须为映射')
    unknown=set(value)-{'contact','window_days'}
    if unknown:raise ValueError('auction_house_bot 含未知键：'+','.join(sorted(map(str,unknown))))
    contact=value.get('contact') or ''
    if not isinstance(contact,str) or len(contact)>200 or re.search(r'[\x00-\x1f]',contact):raise ValueError('auction_house_bot.contact 无效')
    try:contact.encode('latin-1')
    except UnicodeEncodeError:raise ValueError('auction_house_bot.contact 只能用拉丁字母（写进 HTTP 请求头）') from None
    days=value.get('window_days',90)
    if type(days) is not int or not 1<=days<=365:raise ValueError('auction_house_bot.window_days 必须为 1–365 的整数')
    return dict(contact=contact.strip(),window_days=days)

_HOUSES_CACHE={}

def auction_house_settings(root=ROOT):
    """只读 settings.yaml 的拍卖行两段：{houses: 校验后的名单, bot: auction_house_bot}（按文件修改时间缓存）。名单有误时抛 ValueError。"""
    path=Path(root)/'config/settings.yaml'
    try:st=path.stat();mtime=(st.st_mtime_ns,st.st_size)
    except OSError:return dict(houses=[],bot=auction_house_bot(None))
    hit=_HOUSES_CACHE.get(str(path))
    if hit and hit[0]==mtime:return hit[1]
    settings=yaml.safe_load(path.read_text()) or {}
    if not isinstance(settings,dict):settings={}
    out=dict(houses=auction_houses(settings.get('auction_houses')),bot=auction_house_bot(settings.get('auction_house_bot')))
    _HOUSES_CACHE[str(path)]=(mtime,out)
    return out

def auction_house_names(root=ROOT):
    """拍卖行 key → 中文显示名；名单读不到或有误时空 dict。"""
    try:return {h['key']:h['name'] for h in auction_house_settings(root)['houses']}
    except Exception:return {}

def snapshot_version(root=ROOT):
    """发布快照版本（config/settings.yaml 的 publish.snapshot_version；缺省 1）。只读这一段，不做 load() 的全部校验。"""
    return _publish_section(root)['snapshot_version']

DEFAULT_GC_DB='~/Desktop/goodcustomerDB'
DEFAULT_GCDB='~/Desktop/goodcustomer-plugin/skills/gc-schema/scripts/gcdb.py'

def goodcustomer(value):
    """校验 settings.yaml 的 goodcustomer 段：{db, gcdb} 两个文字路径（缺省用默认值），返回展开 ~ 后的绝对路径。"""
    if value is None:value={}
    if not isinstance(value,dict):raise ValueError('goodcustomer 必须为映射（db、gcdb）')
    unknown=set(value)-{'db','gcdb'}
    if unknown:raise ValueError('goodcustomer 含未知键：'+','.join(sorted(map(str,unknown))))
    out={}
    for key,default in (('db',DEFAULT_GC_DB),('gcdb',DEFAULT_GCDB)):
        path=value.get(key,default)
        if not isinstance(path,str) or not path.strip():raise ValueError('goodcustomer.'+key+' 必须为文字路径')
        out[key]=str(Path(path.strip()).expanduser())
    return out

FEE_TIER_KEYS=('A','B','C','D')
_FEE_CACHE={}

def fees_for(root=ROOT):
    """按调用方的 root 读费用档位：root 下有 config/fee_tiers.yaml 就用它，否则用仓库里的那份
    （测试与后台的 root 常是只放 .env、图片的临时目录）。"""
    return fee_tiers(root if (Path(root)/'config/fee_tiers.yaml').is_file() else ROOT)

def fee_tiers(root=ROOT):
    """读取并校验 config/fee_tiers.yaml（按文件修改时间缓存）。返回 dict(tiers, cups, hard_to_pack, fragile)，
    词表已编译成正则（小写、去变音后匹配）。格式不对时报错（中文）。"""
    path=Path(root)/'config/fee_tiers.yaml'
    st=path.stat()
    key=(str(path),st.st_mtime_ns,st.st_size)
    if key in _FEE_CACHE:return _FEE_CACHE[key]
    raw=yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw,dict):raise ValueError('fee_tiers.yaml 必须为映射')
    tiers=raw.get('tiers')
    if not isinstance(tiers,dict) or set(tiers)!=set(FEE_TIER_KEYS):raise ValueError('fee_tiers.tiers 必须恰好有 A、B、C、D 四档')
    out={'tiers':{}}
    prev=0
    for k in FEE_TIER_KEYS:
        t=tiers[k]
        if not isinstance(t,dict):raise ValueError('fee_tiers.tiers.'+k+' 必须为映射')
        amount,label,cap=t.get('amount'),t.get('label'),t.get('max_cm')
        if type(amount) is not int or not 0<=amount<=10000:raise ValueError('fee_tiers.tiers.'+k+'.amount 必须为 0–10000 的整数欧元')
        if not isinstance(label,str) or not label.strip():raise ValueError('fee_tiers.tiers.'+k+'.label 不能为空')
        if isinstance(cap,bool) or not isinstance(cap,(int,float)) or not math.isfinite(cap) or cap<=0:
            raise ValueError('fee_tiers.tiers.'+k+'.max_cm 必须为正数')
        if k!='A':
            if cap<=prev:raise ValueError('fee_tiers：B、C、D 的 max_cm 必须递增')
            prev=cap
        out['tiers'][k]={'amount':amount,'label':label.strip(),'max_cm':float(cap)}
    from .judge import _norm
    for group,need_ids in (('cups',True),('hard_to_pack',True),('fragile',False)):
        g=raw.get(group) or {}
        if not isinstance(g,dict):raise ValueError('fee_tiers.'+group+' 必须为映射')
        ids=g.get('function_ids') or []
        words=g.get('words') or []
        if not isinstance(ids,list) or any(not isinstance(x,str) or not x.strip() for x in ids):
            raise ValueError('fee_tiers.'+group+'.function_ids 必须为文字列表')
        if not need_ids and ids:raise ValueError('fee_tiers.'+group+' 不按功能 ID 判断')
        if not isinstance(words,list) or any(not isinstance(x,str) or not x.strip() for x in words):
            raise ValueError('fee_tiers.'+group+'.words 必须为文字列表')
        zh=g.get('zh_words') or []
        if not isinstance(zh,list) or any(not isinstance(x,str) or not x.strip() for x in zh):
            raise ValueError('fee_tiers.'+group+'.zh_words 必须为文字列表')
        if not need_ids and zh:raise ValueError('fee_tiers.'+group+' 不按标准名称判断')
        try:rx=re.compile('(?:'+'|'.join(_norm(w) for w in words)+r')(?!\w)') if words else None
        except re.error as e:raise ValueError('fee_tiers.'+group+'.words 正则无效：'+str(e)) from None
        try:zrx=re.compile('|'.join(zh)) if zh else None
        except re.error as e:raise ValueError('fee_tiers.'+group+'.zh_words 正则无效：'+str(e)) from None
        out[group]={'function_ids':frozenset(x.strip() for x in ids),'rx':rx,'zh':zrx}
    out['root']=str(Path(root))
    if len(_FEE_CACHE)>4:_FEE_CACHE.clear()
    _FEE_CACHE[key]=out
    return out

_LEARN_CACHE={}

def learning_for(root=ROOT):
    """按调用方的 root 读规律归纳参数：root 下有 config/learning.yaml 就用它，否则用仓库里的那份。"""
    return learning(root if (Path(root)/'config/learning.yaml').is_file() else ROOT)

def learning(root=ROOT):
    """读取并校验 config/learning.yaml（故事 17，按文件修改时间缓存）。
    返回 dict(min_count, min_ratio, keep_bonus, price_bands={currency, edges})。格式不对时报错（中文）。"""
    path=Path(root)/'config/learning.yaml'
    st=path.stat()
    key=(str(path),st.st_mtime_ns,st.st_size)
    if key in _LEARN_CACHE:return _LEARN_CACHE[key]
    raw=yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw,dict):raise ValueError('learning.yaml 必须为映射')
    unknown=set(raw)-{'min_count','min_ratio','keep_bonus','price_bands'}
    if unknown:raise ValueError('learning.yaml 含未知键：'+','.join(sorted(map(str,unknown))))
    count=raw.get('min_count',5)
    if type(count) is not int or not 1<=count<=1000:raise ValueError('learning.min_count 必须为 1–1000 的整数')
    ratio=raw.get('min_ratio',0.8)
    if isinstance(ratio,bool) or not isinstance(ratio,(int,float)) or not 0<ratio<=1:raise ValueError('learning.min_ratio 必须在 0–1 之间')
    bonus=raw.get('keep_bonus',10)
    if type(bonus) is not int or not 0<=bonus<=50:raise ValueError('learning.keep_bonus 必须为 0–50 的整数')
    bands=raw.get('price_bands',{})
    if not isinstance(bands,dict):raise ValueError('learning.price_bands 必须为映射（currency、edges）')
    if not set(bands)<={'currency','edges'}:raise ValueError('learning.price_bands 只能有 currency、edges')
    currency=bands.get('currency','EUR')
    if not isinstance(currency,str) or not re.fullmatch(r'[A-Z]{3}',currency):raise ValueError('learning.price_bands.currency 必须为三位大写币种')
    edges=bands.get('edges',[])
    if not isinstance(edges,list) or any(isinstance(x,bool) or not isinstance(x,(int,float)) or not math.isfinite(x) or x<=0 for x in edges):
        raise ValueError('learning.price_bands.edges 必须为正数列表')
    if any(b<=a for a,b in zip(edges,edges[1:])):raise ValueError('learning.price_bands.edges 必须从小到大')
    out={'min_count':count,'min_ratio':float(ratio),'keep_bonus':bonus,'price_bands':{'currency':currency,'edges':[float(x) for x in edges]}}
    if len(_LEARN_CACHE)>4:_LEARN_CACHE.clear()
    _LEARN_CACHE[key]=out
    return out

_MATCH_CACHE={}
MATCH_GRADES=('need','explicit','inferred')
MATCH_DIMS=('brand','pattern','form')

def match_rules_for(root=ROOT):
    """按调用方的 root 读匹配词表：root 下有 config/matching.yaml 就用它，否则用仓库里的那份。"""
    return match_rules(root if (Path(root)/'config/matching.yaml').is_file() else ROOT)

def _words(value,where):
    if not isinstance(value,list) or any(not isinstance(x,str) or not x.strip() for x in value):
        raise ValueError(where+' 必须为文字列表')
    return [x.strip() for x in value]

def match_rules(root=ROOT):
    """读取并校验 config/matching.yaml（故事 16，按文件修改时间缓存）。返回已规范化（小写、去变音）的词表：
    grades{need,explicit,inferred: {label, weight}}、detail_required（须再对上图案或器型的分级）、need_recent_days（04 只用近这么多天的需求）、sources、dim_of{GC 维度: brand/pattern/form}、
    brands{规范化品牌: [别名…]}、negations、negations_before、patterns[{ids, terms, brands}]、forms{组名: [词…]}。格式不对时报错（中文）。"""
    path=Path(root)/'config/matching.yaml'
    st=path.stat()
    key=(str(path),st.st_mtime_ns,st.st_size)
    if key in _MATCH_CACHE:return _MATCH_CACHE[key]
    from .judge import _norm
    raw=yaml.safe_load(path.read_text()) or {}
    if not isinstance(raw,dict):raise ValueError('matching.yaml 必须为映射')
    grades=raw.get('grades')
    if not isinstance(grades,dict) or set(grades)!=set(MATCH_GRADES):raise ValueError('matching.grades 必须恰好有 need、explicit、inferred')
    out={'grades':{}}
    for k in MATCH_GRADES:
        g=grades[k]
        if not isinstance(g,dict) or not isinstance(g.get('label'),str) or not g['label'].strip():raise ValueError('matching.grades.'+k+'.label 不能为空')
        if type(g.get('weight')) is not int:raise ValueError('matching.grades.'+k+'.weight 必须为整数')
        out['grades'][k]={'label':g['label'].strip(),'weight':g['weight']}
    w=[out['grades'][k]['weight'] for k in MATCH_GRADES]
    if not w[0]>w[1]>w[2]:raise ValueError('matching.grades：明说想要 > 明说喜欢 > 推算口味（weight 递减）')
    sources=raw.get('sources') or {}
    if not isinstance(sources,dict) or any(not isinstance(v,str) for v in sources.values()):raise ValueError('matching.sources 必须为 来源: 中文 的映射')
    out['sources']={str(k):v for k,v in sources.items()}
    dims=raw.get('dimensions')
    if dims is None:raise ValueError('matching.yaml 缺少 dimensions（brand、pattern、form 各对照哪些 GoodCustomer 维度）')
    if not isinstance(dims,dict) or not set(dims)<=set(MATCH_DIMS):raise ValueError('matching.dimensions 只能有 brand、pattern、form')
    out['dim_of']={}
    for d,names in dims.items():
        for n in _words(names,'matching.dimensions.'+d):
            if n in out['dim_of']:raise ValueError('matching.dimensions：'+n+' 重复')
            out['dim_of'][n]=d
    brands=raw.get('brands') or {}
    if not isinstance(brands,dict):raise ValueError('matching.brands 必须为 品牌: [别名…] 的映射')
    out['brands']={}
    for b,v in brands.items():
        if not isinstance(b,str) or not b.strip():raise ValueError('matching.brands 的品牌名不能为空')
        if _norm(b) in out['brands']:raise ValueError('matching.brands：'+b+' 与另一个品牌名规范化后相同')
        out['brands'][_norm(b)]=[_norm(a) for a in _words(v or [],'matching.brands.'+b)]
    dr=raw.get('detail_required',[])
    if not isinstance(dr,list) or any(x not in MATCH_GRADES for x in dr):raise ValueError('matching.detail_required 只能列 need、explicit、inferred')
    out['detail_required']=frozenset(dr)
    days=raw.get('need_recent_days',365)
    if type(days) is not int or days<1:raise ValueError('matching.need_recent_days 必须为正整数（天）')
    out['need_recent_days']=days
    out['negations']=[_norm(x) for x in _words(raw.get('negations') or [],'matching.negations')]
    out['negations_before']=[_norm(x) for x in _words(raw.get('negations_before') or [],'matching.negations_before')]
    out['exclusions_before']=[_norm(x) for x in _words(raw.get('exclusions_before') or [],'matching.exclusions_before')]
    pats=raw.get('patterns') or []
    if not isinstance(pats,list):raise ValueError('matching.patterns 必须为列表')
    out['patterns']=[]
    for n,p in enumerate(pats):
        where='matching.patterns 第 %d 项'%(n+1)
        if not isinstance(p,dict) or not set(p)<={'ids','terms','brands'}:raise ValueError(where+'格式不对')
        terms=[_norm(t) for t in _words(p.get('terms'),where+' terms')]
        if not terms:raise ValueError(where+'缺少 terms')
        scope=_words(p.get('brands') or [],where+' brands')
        unknown=[b for b in scope if _norm(b) not in out['brands']]
        if unknown:raise ValueError(where+' brands 不在 matching.brands 里：'+'、'.join(unknown))
        out['patterns'].append({'ids':frozenset(_words(p.get('ids') or [],where+' ids')),'terms':terms,
                                'brands':frozenset(_norm(b) for b in scope)})
    forms=raw.get('forms') or {}
    if not isinstance(forms,dict):raise ValueError('matching.forms 必须为 组名: [词…] 的映射')
    out['forms']={str(k):[_norm(t) for t in _words(v,'matching.forms.'+str(k))] for k,v in forms.items()}
    out['root']=str(Path(root))
    if len(_MATCH_CACHE)>4:_MATCH_CACHE.clear()
    _MATCH_CACHE[key]=out
    return out

_PERSONALIZE_CACHE={}
PERSONALIZE_NUMBERS=('open','dwell_per_second','dwell_cap_seconds','half_life_days','max_score')
PERSONALIZE_SAME=('brand','pattern','form')

def personalize_rules_for(root=ROOT):
    """按调用方的 root 读千人千面参数：root 下有 config/personalize.yaml 就用它，否则用仓库里的那份。"""
    return personalize_rules(root if (Path(root)/'config/personalize.yaml').is_file() else ROOT)

def personalize_rules(root=ROOT):
    """读取并校验 config/personalize.yaml（故事 18，按文件修改时间缓存）。返回
    {open, dwell_per_second, dwell_cap_seconds, half_life_days, max_score, same{brand, pattern, form}}（都是非负数，
    半衰期 > 0；max_score 须小于同一 root 下 matching.yaml 分级权重的最小级差，含推算口味与无匹配之差）。
    格式不对时报错（中文）。"""
    path=Path(root)/'config/personalize.yaml'
    st=path.stat()
    key=(str(path),st.st_mtime_ns,st.st_size)
    if key in _PERSONALIZE_CACHE:return _PERSONALIZE_CACHE[key]
    raw=yaml.safe_load(path.read_text(encoding='utf-8')) or {}
    browse=raw.get('browse') if isinstance(raw,dict) else None
    if not isinstance(browse,dict):raise ValueError('personalize.browse 必须为映射')
    unknown=set(browse)-set(PERSONALIZE_NUMBERS)-{'same'}
    if unknown:raise ValueError('personalize.browse 含未知键：'+','.join(sorted(map(str,unknown))))
    def num(value,where):
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError(where+' 必须为非负数')
        return float(value)
    out={k:num(browse.get(k),'personalize.browse.'+k) for k in PERSONALIZE_NUMBERS}
    if out['half_life_days']<=0:raise ValueError('personalize.browse.half_life_days 必须大于 0')
    same=browse.get('same')
    if not isinstance(same,dict) or set(same)!=set(PERSONALIZE_SAME):
        raise ValueError('personalize.browse.same 必须恰好有 brand、pattern、form')
    out['same']={k:num(same[k],'personalize.browse.same.'+k) for k in PERSONALIZE_SAME}
    w=[match_rules_for(root)['grades'][k]['weight'] for k in MATCH_GRADES]+[0]
    step=min(a-b for a,b in zip(w,w[1:]))
    if not out['max_score']<step:
        raise ValueError(f'personalize.browse.max_score 必须小于分级权重的最小级差（{step}）')
    _PERSONALIZE_CACHE.clear();_PERSONALIZE_CACHE[key]=out
    return out

def _env(root,keys):
    """只读项目根目录 .env 中指定的键；不读进程环境变量，避免被系统环境悄悄覆盖。"""
    values={}
    try:lines=(Path(root)/'.env').read_text().splitlines()
    except OSError:return None
    for line in lines:
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        k,v=line.split('=',1)
        if k.strip() in keys:values[k.strip()]=v.strip().strip('\"\'')
    return values

def credentials(root=ROOT):
    # Read only .env: environment variables cannot silently override this account.
    values=_env(root,('EBAY_CLIENT_ID','EBAY_CLIENT_SECRET'))
    if values is None:raise ValueError('请填写项目根目录 .env')
    if not all(values.get(k) for k in ('EBAY_CLIENT_ID','EBAY_CLIENT_SECRET')):
        raise ValueError('.env 缺少 eBay 凭据')
    return values['EBAY_CLIENT_ID'],values['EBAY_CLIENT_SECRET']

def etsy_credentials(root=ROOT):
    """Etsy 只在 .env 填了 ETSY_API_KEY 时启用；返回 (keystring, shared_secret 或空串)，未启用返回 None。"""
    values=_env(root,('ETSY_API_KEY','ETSY_SHARED_SECRET')) or {}
    if not values.get('ETSY_API_KEY'):return None
    return values['ETSY_API_KEY'],values.get('ETSY_SHARED_SECRET','')

def anthropic_key(root=ROOT):
    """AI 打分只在 .env 填了 ANTHROPIC_API_KEY 时启用；未填返回 None（ai_status: disabled）。"""
    values=_env(root,('ANTHROPIC_API_KEY',)) or {}
    return values.get('ANTHROPIC_API_KEY') or None

# ---- 故事 27：综合评分参数（config/scoring.yaml，方案 C）----
_SCORING_CACHE={}
SCORING_WEIGHTS=('momo','value','trend','customer')
SCORING_TREND=('brand','pattern','function')
# 可用 scripts/score_compare.py --set 覆盖的参数名（扁平）：四个权重、trend_brand / trend_pattern / trend_function 与下列标量。
SCORING_SCALARS=('customer_k','customer_max','smoothing','ai_share','min_peers','half_life_days','absolute','trend_scale')
# 故事 28：曝光衰减参数（scoring.yaml 的 exposure 段）；score_compare.py 用 --set exposure.per_day=… 覆盖。
SCORING_EXPOSURE=('per_day','max','hint_days')

def scoring_rules_for(root=ROOT):
    """按调用方的 root 读综合评分参数：root 下有 config/scoring.yaml 就用它，否则用仓库里的那份。"""
    return scoring_rules(root if (Path(root)/'config/scoring.yaml').is_file() else ROOT)

def _nonneg(value,where,integer=False,positive=False):
    if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
        raise ValueError(where+' 必须为非负数')
    if integer and type(value) is not int:raise ValueError(where+' 必须为整数')
    if positive and value<=0:raise ValueError(where+' 必须大于 0')
    return value

def scoring_validate(raw):
    """校验综合评分参数（dict，同 scoring.yaml 的结构），返回规范化的副本。格式不对时报错（中文）。"""
    if not isinstance(raw,dict):raise ValueError('scoring.yaml 必须为映射')
    unknown=set(raw)-{'weights','trend_weights','price_edges','exposure',*SCORING_SCALARS}
    if unknown:raise ValueError('scoring.yaml 含未知键：'+','.join(sorted(map(str,unknown))))
    w=raw.get('weights')
    if not isinstance(w,dict) or set(w)!=set(SCORING_WEIGHTS):raise ValueError('scoring.weights 必须恰好有 momo、value、trend、customer')
    weights={k:float(_nonneg(w[k],'scoring.weights.'+k)) for k in SCORING_WEIGHTS}
    if sum(weights.values())<=0:raise ValueError('scoring.weights 之和必须大于 0')
    t=raw.get('trend_weights')
    if not isinstance(t,dict) or set(t)!=set(SCORING_TREND):raise ValueError('scoring.trend_weights 必须恰好有 brand、pattern、function')
    edges=raw.get('price_edges')
    if not isinstance(edges,list) or not edges or any(isinstance(x,bool) or not isinstance(x,(int,float)) or x<=0 for x in edges) \
            or any(b<=a for a,b in zip(edges,edges[1:])):
        raise ValueError('scoring.price_edges 必须为递增的正数列表')
    out=dict(weights=weights,trend_weights={k:float(_nonneg(t[k],'scoring.trend_weights.'+k)) for k in SCORING_TREND},
             price_edges=[float(x) for x in edges])
    out['customer_k']=float(_nonneg(raw.get('customer_k'),'scoring.customer_k',positive=True))
    out['customer_max']=float(_nonneg(raw.get('customer_max'),'scoring.customer_max'))
    if out['customer_max']>weights['momo']:raise ValueError('scoring.customer_max 不能大于口味权重')
    out['smoothing']=float(_nonneg(raw.get('smoothing'),'scoring.smoothing'))
    share=_nonneg(raw.get('ai_share'),'scoring.ai_share')
    if share>1:raise ValueError('scoring.ai_share 必须为 0–1')
    out['ai_share']=float(share)
    out['min_peers']=_nonneg(raw.get('min_peers'),'scoring.min_peers',integer=True,positive=True)
    out['half_life_days']=float(_nonneg(raw.get('half_life_days'),'scoring.half_life_days',positive=True))
    flag=raw.get('absolute')
    if flag not in (0,1) or isinstance(flag,bool):raise ValueError('scoring.absolute 必须为 0（排名百分位）或 1（绝对刻度）')
    out['absolute']=int(flag)
    out['trend_scale']=float(_nonneg(raw.get('trend_scale'),'scoring.trend_scale',positive=True))
    e=raw.get('exposure')
    if not isinstance(e,dict) or set(e)!=set(SCORING_EXPOSURE):raise ValueError('scoring.exposure 必须恰好有 per_day、max、hint_days')
    hint=_nonneg(e['hint_days'],'scoring.exposure.hint_days',integer=True)
    if hint<1:raise ValueError('scoring.exposure.hint_days 必须为不小于 1 的整数（天）')
    out['exposure']=dict(per_day=float(_nonneg(e['per_day'],'scoring.exposure.per_day')),
                         max=float(_nonneg(e['max'],'scoring.exposure.max')),hint_days=hint)
    return out

def scoring_rules(root=ROOT):
    """读取并校验 config/scoring.yaml（故事 27，按文件修改时间缓存）。返回 scoring_validate 的结果。"""
    path=Path(root)/'config/scoring.yaml'
    st=path.stat()
    key=(str(path),st.st_mtime_ns,st.st_size)
    if key in _SCORING_CACHE:return _SCORING_CACHE[key]
    out=scoring_validate(yaml.safe_load(path.read_text()) or {})
    _SCORING_CACHE.clear();_SCORING_CACHE[key]=out
    return out

def scoring_override(params,sets):
    """在已校验的参数上套用扁平覆盖 {名: 数}（名见 SCORING_WEIGHTS、trend_*、SCORING_SCALARS、exposure.*），返回新的已校验参数。
    未知参数名报错。"""
    import copy
    raw=copy.deepcopy(params)
    for name,value in sets.items():
        if name in SCORING_WEIGHTS:raw['weights'][name]=value
        elif name.startswith('trend_') and name[6:] in SCORING_TREND:raw['trend_weights'][name[6:]]=value
        elif name in SCORING_SCALARS:raw[name]=int(value) if name in ('min_peers','absolute') and float(value).is_integer() else value
        elif name.startswith('exposure.') and name[9:] in SCORING_EXPOSURE:
            raw['exposure'][name[9:]]=int(value) if name=='exposure.hint_days' and float(value).is_integer() else value
        else:raise ValueError('未知参数：'+name)
    return scoring_validate(raw)
