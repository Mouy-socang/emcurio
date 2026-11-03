import re
from . import judge

def _match(p,t):return re.search(p,t or '',re.I)

# 品相陈述（product_v2.extract 用，输出保持原样）：类别、正则。
DAMAGE_PATTERNS=[
    ('破损',r'\bdefekt\b|\bbeschädigung\w*\b|\bbeschädigt\w*\b'),
    ('修复',r'\brestauriert\w*\b|\brepariert\w*\b|\bgeklebt\b'),
    ('磕口',r'\babplatzer\w*\b|\bbestoßen\w*\b|\bbestossen\w*\b|\bchips?\b|\bmacken\b|\bfehlstelle\b|\babsplitterung\w*\b|\bflohbisse?\b'),
    ('裂纹',r'\bhaarriss\w*\b|\brisse?\b'),
    ('金彩磨损',r'goldabrieb|gold berieben|abrieb an den goldrändern'),
]
# 自动移出用：在品相陈述词之外再并入英文与原 DAMAGE 词（defekt*、best[oö][sß]t*、chipped、crack、broken、repaired、restored、中文）。
REMOVAL_PATTERNS=[
    ('破损',DAMAGE_PATTERNS[0][1]+r'|\bdefekt\w*|\bbroken\b|破损'),
    ('修复',DAMAGE_PATTERNS[1][1]+r'|\brepaired\b|\brestored\b|修复'),
    ('磕口',DAMAGE_PATTERNS[2][1]+r'|\bchipped\b|\bbest[oö][sß]t\w*|缺口'),
    ('裂纹',DAMAGE_PATTERNS[3][1]+r'|\bcrack(?:ed|s)?\b|裂纹'),
    DAMAGE_PATTERNS[4],
]
# 只按这些类别自动移出；金彩磨损只记录。
REMOVAL_LABELS=('破损','修复','磕口','裂纹','需修理')
_NEG=r'\b(?:keine\w*|kein\w*|ohne|nicht|nirgends|weder|no|without)\b'
_CLAUSE=r'[.!?。！？]|\b(?:aber|allerdings|jedoch|nur|außer|ausser|but|except)\b'

def _normalize(text):
    # Keep abbreviations inside the same clause, e.g. "keine Risse bzw. Chips".
    normalized=re.sub(r'\b(bzw|bspw|ca|z|B|s|evtl)\.',r'\1',text or '',flags=re.I)
    return re.sub(r'o\.\s*ä\.', 'oder ähnliche',normalized,flags=re.I)

def _statements(title,description,patterns):
    """分句否定判断（品相陈述用）：返回 [(类别, 原文附近片段, 来源)]，每类只记第一处。"""
    found=[]
    for source,text in [('title',title),('description',description)]:
        normalized=_normalize(text)
        for cn,pat in patterns:
            for m in re.finditer(pat,normalized,re.I):
                left=normalized[max(0,m.start()-220):m.start()]
                right=normalized[m.end():m.end()+90]
                clause=re.split(r'[.!?]|\b(?:aber|allerdings|jedoch|nur|außer|ausser)\b',left,flags=re.I)[-1]
                if _match(_NEG,clause):continue
                if _match(r'^\s*(?:ist|sind|wurde)?\s*nicht\b',right):continue
                if _match(r'karton|verpackung|ovp',clause):continue
                if _match(r'garantie|gewährleistung|haftung|versand|rücknahme',clause):continue
                label='需修理' if cn=='修复' and _match(r'müsste|muss|müssten|sollte',clause) else cn
                if label not in [x[0] for x in found]:
                    found.append((label,normalized[max(0,m.start()-80):m.end()+90],source))
    return found

def _phrase(text,m):
    """命中所在的短句（逗号、句号、转折词之间），用作理由里的原文片段。"""
    left=re.split(r'[,;:()\n，；：]|'+_CLAUSE,text[max(0,m.start()-120):m.start()],flags=re.I)[-1]
    right=re.split(r'[,;:()\n，；：]|'+_CLAUSE,text[m.end():m.end()+120],flags=re.I)[0]
    phrase=' '.join((left+m.group(0)+right).split())
    return phrase if len(phrase)<=80 else phrase[:79]+'…'

def damage_statements(title, description):
    """品相陈述（原 product_v2.damage_statements，行为不变）：[(类别, 原文片段, 来源)]。"""
    return _statements(title,description,DAMAGE_PATTERNS)

# ---- 自动移出的否定判断：作用范围是命中所在的逗号/分号短句。
# 另认英文 not/never/free of、nichts、卖家漏空格的 "istnicht"。
_NEG_REMOVAL=r'\b(?:keine\w*|kein\w*|ohne|nichts?|nirgends|weder|no|without|not|never|free\s+of|(?:ist|sind|war|waren)nicht)\b'
# 命中词右边紧跟的后置否定："Beschädigungen: keine"、"Risse: nein"、"ist nicht"。
_POSTFIX_NEG=r'^\s*[:=–-]?\s*(?:keine?|nein|none|no)\b|^\s*(?:ist|sind|wurde)?\s*nicht\b'
_PACKAGING=r'karton|verpackung|ovp|garantie|gewährleistung|haftung|versand|rücknahme'
_PHRASE_SPLIT=r'[,;，；]'
# 否定可跨逗号延续的“纯破损词列表”：破损词、连接词与少量修饰词之外不能有别的词。
_LIST_TERM=re.compile('|'.join(p for _,p in REMOVAL_PATTERNS)+r'|\babplatz\w*|\bsprünge?\b|\bsprung\w*|\bkratzer\w*|\w*abrieb\w*|'
                      r'\bbestoss\w*|\bbestoß\w*|\bbr[uü]ch\w*|\bsch[aä]den\b|\bmängel\w*|\bschönheitsfehler\w*|\bdamage\w*|\bcracks?\b|'
                      r'\bsplits?\b|\bverfärbung\w*|\bflecken\b',re.I)
_FILLER=re.compile(r'\b(?:und|oder|or|and|nor|bzw|sowie|noch|auch|ähnlich\w*|sonstig\w*|ander\w*|sichtbar\w*|nennenswert\w*|'
                   r'weiter\w*|other|any|visible)\b',re.I)

def _list_only(piece):
    rest=_FILLER.sub(' ',_LIST_TERM.sub(' ',piece))
    return not re.sub(r'[\W\d_]','',rest)

def _negated(left,right):
    """命中是否被否定（自动移出用）。left/right 为同一分句内命中左右的文字。"""
    pieces=re.split(_PHRASE_SPLIT,left)
    own=pieces[-1]
    if _match(_NEG_REMOVAL,own) or _match(_POSTFIX_NEG,right):return True
    if not _list_only(own):return False
    # 否定只经纯破损词列表跨逗号延续，如 "keine Chips, Risse oder Abplatzer"。
    for piece in reversed(pieces[:-1]):
        negs=list(re.finditer(_NEG_REMOVAL,piece,re.I))
        if negs:return bool(_LIST_TERM.search(piece[negs[-1].end():]))
        if not _list_only(piece):return False
    return False

def _removal_findings(title,description):
    """自动移出用：[(类别, 命中所在短句)]，按出现来源与类别顺序，每类只记第一处。"""
    found=[]
    for text in (title,description):
        normalized=_normalize(text)
        for cn,pat in REMOVAL_PATTERNS:
            for m in re.finditer(pat,normalized,re.I):
                left=re.split(_CLAUSE,normalized[max(0,m.start()-220):m.start()],flags=re.I)[-1]
                right=re.split(_CLAUSE,normalized[m.end():m.end()+90],flags=re.I)[0]
                if _negated(left,right):continue
                # 包装/保修/运输：本短句命中左侧，及右侧紧邻几个字（"Beschädigung am Karton"）。
                own=re.split(_PHRASE_SPLIT,left)[-1]+m.group(0)+re.split(_PHRASE_SPLIT,right)[0][:20]
                if _match(_PACKAGING,own):continue
                label='需修理' if cn=='修复' and _match(r'müsste|muss|müssten|sollte',left) else cn
                if label not in [x[0] for x in found]:found.append((label,_phrase(normalized,m)))
    return found

def damage_reason(item):
    """自动移出用的破损/修复判断：命中返回理由（类别与原文片段），否则 None。
    否定与包装/保修/运输排除只看命中所在的逗号/分号短句（否定可经纯破损词列表跨逗号延续）；
    后置否定（"Risse: nein"）也算；金彩磨损不移出。"""
    description='. '.join(str(item.get(k,'') or '') for k in ('description','condition') if item.get(k))
    for label,phrase in _removal_findings(item.get('title',''),description):
        if label in REMOVAL_LABELS:
            return '破损或修复：%s（"%s"）'%(label,phrase)
    return None

# Additional unambiguous off-topic matches from actual Kleinanzeigen results.
# Keep these in the collection rule layer; legacy judge callers remain unchanged.
OFF_TOPIC = [
    (r'\b(?:baugrundst[uü]ck\w*|grundst[uü]ck\w*|eigentumswohnung\w*|einfamilienhaus|mehrfamilienhaus|reihenhaus|doppelhaush[aä]lfte|mietwohnung\w*)\b', '房产/土地'),
    (r'\b(?:terrassenplatten|pflastersteine|bodenfliesen|wandfliesen|betonplatten)\b', '建材/地砖'),
    (r'\b(?:gutschein|geschenkkarte)\b', '代金券/非瓷器'),
]

def irrelevant_reason(title):
    existing=judge.hard_filter(title or '')
    if existing:return existing
    for pattern,reason in OFF_TOPIC:
        if re.search(pattern,title or '',re.I):return reason
    return None

def evaluate(item,config):
    result=judge.judge(item.get('title',''),item.get('listed_price'),item.get('price_text',''),use_legacy_prices=False)
    for word in config.get('blocked_words', []):
        for field in ('title', 'description'):
            if word.casefold() in str(item.get(field, '') or '').casefold():
                return dict(result, decision='skip', reason='屏蔽词：'+word+'（'+('标题' if field=='title' else '描述')+'）', fair='')
    irrelevant=irrelevant_reason(item.get('title',''))
    if irrelevant:return dict(result,decision='skip',reason='硬过滤：'+irrelevant,fair='')
    text=' '.join(str(item.get(k,'') or '') for k in ('title','description','condition'))
    reason=damage_reason(item)
    if reason:
        price=item.get('listed_price');cap=config.get('cheap_damaged_max')
        # The configured exception uses EUR listing price, never current unknown price.
        if cap is not None and price is not None and item.get('currency')=='EUR' and price<cap:
            return dict(result,decision='probe',reason='破损件标价低于已配置阈值，需询市',fair='')
        return dict(result,decision='skip',reason=reason,fair='')
    for rule in config['slow_sellers']:
        if re.search(rule['pattern'],text,re.I):return dict(result,decision='skip',reason=rule['reason'],fair='')
    for rule in config['price_rules']:
        if re.search(rule['pattern'],text,re.I) and item.get('currency')==rule['currency']:
            p=item.get(rule['basis'])
            if p is not None and p>rule['max_price']:
                return dict(result,decision='skip',reason=rule['reason'],fair=str(rule['max_price']))
    return result

def standard_name(item,config):
    """CAP-16：标准名称的唯一对外入口（命名逻辑在 judge.standard_name，其它模块不得复制）。"""
    return judge.standard_name(item.get('title',''),item.get('description',''),config)

def kb_fingerprint(config):
    """当前知识库指纹（只 stat，不重读未变化的 CSV）；与 std_names.kb_fingerprint 比对决定是否重算。"""
    return judge.kb_vocab((config or {}).get('em_kb_path'))[2]

def kb_status(config):
    """当前知识库状态：ok / missing / partial:…（重判脚本据此决定是否刷新标准名称）。"""
    return judge.kb_vocab((config or {}).get('em_kb_path'))[1]
