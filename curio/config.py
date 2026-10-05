"""Local configuration. Credentials never enter config objects or database."""
from pathlib import Path
import math
import re
import yaml

ROOT = Path(__file__).resolve().parents[1]

def load(root=ROOT):
    root = Path(root)
    settings = yaml.safe_load((root/'config/settings.yaml').read_text()) or {}
    defaults = dict(keywords=['Meissen'],auction_fee_rate=0.0,cheap_damaged_max=None,
                    daily_budget=0,kleinanzeigen_enabled=True,status_check_limit=20)
    defaults.update(settings)
    if not isinstance(defaults['keywords'],list) or not defaults['keywords'] or any(not isinstance(k,str) or not k.strip() for k in defaults['keywords']):
        raise ValueError('keywords 必须为非空文字列表')
    for key in ('auction_fee_rate','daily_budget','cheap_damaged_max'):
        value=defaults[key]
        if key=='cheap_damaged_max' and value is None:continue
        if isinstance(value,bool) or not isinstance(value,(int,float)) or not math.isfinite(value) or value<0:
            raise ValueError(key+' 必须为非负数')
    if type(defaults['status_check_limit']) is not int or not 0<=defaults['status_check_limit']<=300:
        raise ValueError('status_check_limit 必须为 0–300')
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
    return defaults

def credentials(root=ROOT):
    # Read only .env: environment variables cannot silently override this account.
    values={}
    try:lines=(Path(root)/'.env').read_text().splitlines()
    except OSError:raise ValueError('请填写项目根目录 .env') from None
    for line in lines:
        line=line.strip()
        if not line or line.startswith('#') or '=' not in line:continue
        k,v=line.split('=',1)
        if k.strip() in ('EBAY_CLIENT_ID','EBAY_CLIENT_SECRET'):values[k.strip()]=v.strip().strip('\"\'')
    if not all(values.get(k) for k in ('EBAY_CLIENT_ID','EBAY_CLIENT_SECRET')):
        raise ValueError('.env 缺少 eBay 凭据')
    return values['EBAY_CLIENT_ID'],values['EBAY_CLIENT_SECRET']
