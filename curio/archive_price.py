"""Immutable archive-time currency conversion; never invent sale prices."""
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from zoneinfo import ZoneInfo
import json
import xml.etree.ElementTree as ET
import requests
URL='https://www.ecb.europa.eu/stats/eurofxref/eurofxref-hist-90d.xml'
def archive_price(amount,currency,root,archive_date=None):
 day=archive_date or datetime.now(ZoneInfo('Europe/Berlin')).date().isoformat()
 result={'original_amount':amount,'original_currency':currency,'archive_date':day,'price_kind':'listed','amount_cny':None,'status':'pending_rate'}
 try:value=Decimal(str(amount))
 except InvalidOperation:return dict(result,status='missing_price')
 if not value.is_finite() or value<0:return dict(result,status='missing_price')
 if currency=='CNY':return dict(result,amount_cny=str(value.quantize(Decimal('.01'))),status='converted',rate='1',rate_date=day,rate_source='original_currency')
 folder=root/'data/porcelain/fx';folder.mkdir(parents=True,exist_ok=True);cache=folder/(day+'.json')
 try:
  if cache.exists():rates=json.loads(cache.read_text())
  else:
   response=requests.get(URL,timeout=(10,20));response.raise_for_status()
   dates=[]
   for cube in ET.fromstring(response.content).iter():
    date=cube.get('time')
    if date and date<=day:dates.append((date,{x.get('currency'):x.get('rate') for x in cube if x.get('currency')}))
   date,values=max(dates);rates={'date':date,'values':dict(values,EUR='1'),'source':URL}
   cache.write_text(json.dumps(rates))
  cny=Decimal(rates['values']['CNY']);original=Decimal(rates['values'][currency]);rate=cny/original
  return dict(result,amount_cny=str((value*rate).quantize(Decimal('.01'),rounding=ROUND_HALF_UP)),rate=str(rate),eur_cny=str(cny),rate_date=rates['date'],rate_source=rates['source'],status='converted' if rates['date']==day else 'converted_latest_published')
 except (requests.RequestException,ValueError,KeyError,InvalidOperation,ET.ParseError):return result
