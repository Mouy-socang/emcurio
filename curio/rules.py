import re
from . import judge

DAMAGE=re.compile(r'\b(defekt|beschädigt\w*|best[oö][sß]t\w*|chip(?:ped|s)?|crack(?:ed|s)?|broken|repariert\w*|restauriert\w*|haarriss\w*|abplatzer\w*)\b|破损|裂纹|缺口|修复',re.I)
NEGATED=re.compile(r'\b(?:keine?|ohne|no|without)\s+(?:(?:chips?|cracks?|beschädigungen|haarrisse|abplatzer)(?:\s*(?:,|und|or|and|oder)\s*)?)+',re.I)

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
    irrelevant=irrelevant_reason(item.get('title',''))
    if irrelevant:return dict(result,decision='skip',reason='硬过滤：'+irrelevant,fair='')
    text=' '.join(str(item.get(k,'') or '') for k in ('title','description','condition'))
    damaged=bool(DAMAGE.search(NEGATED.sub('',text)))
    if damaged:
        reason='破损或修复线索，默认不要'
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
