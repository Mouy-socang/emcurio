"""Small read-only live check. Never saves or prints credentials/tokens."""
import json,socket,sys
from datetime import datetime,timezone
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import requests
from curio.config import ROOT,credentials

def verify():
    source=ROOT if (ROOT/'.env').exists() else Path.home()/'Desktop/curio'
    result={'checked_at':datetime.now(timezone.utc).isoformat(),'source':str(source/'.env'),'checks':[]}
    def record(stage,**fields):
        result['checks'].append(dict(stage=stage,**fields))
    try:client,secret=credentials(source)
    except ValueError:
        record('credentials',status='missing');return result
    record('credentials',status='present')
    try:
        addresses=sorted({v[4][0] for v in socket.getaddrinfo('api.ebay.com',443,type=socket.SOCK_STREAM)})
        record('dns',status='ok',addresses=addresses)
    except OSError:
        record('dns',status='failed',message='当前运行环境无法解析 api.ebay.com；尚未验证凭据。');return result
    session=requests.Session()
    try:
        response=session.post('https://api.ebay.com/identity/v1/oauth2/token',auth=(client,secret),
            data={'grant_type':'client_credentials','scope':'https://api.ebay.com/oauth/api_scope'},timeout=20,allow_redirects=False)
        record('oauth',http_status=response.status_code)
        if response.status_code!=200:return result
        token=response.json().get('access_token')
        if not token:
            record('oauth',status='missing_token');return result
        response=session.get('https://api.ebay.com/buy/browse/v1/item_summary/search',
            headers={'Authorization':'Bearer '+token,'X-EBAY-C-MARKETPLACE-ID':'EBAY_DE'},
            params={'q':'Meissen','filter':'itemLocationCountry:DE,buyingOptions:{FIXED_PRICE|AUCTION}',
                    'sort':'newlyListed','limit':3,'offset':0},timeout=20,allow_redirects=False)
        record('browse',http_status=response.status_code)
        data=response.json()
        if response.status_code==200:
            record('result',status='success',returned=len(data.get('itemSummaries',[])),
                   message='eBay 授权和 Browse 商品搜索均成功。')
        else:
            record('result',status='api_error',error_ids=[e['errorId'] for e in data.get('errors',[]) if isinstance(e.get('errorId'),int)])
    except requests.RequestException:
        record('network',status='request_failed',message='请求失败，未输出认证信息或服务端原始响应。')
    except (ValueError,KeyError,TypeError):record('response',status='invalid_response')
    finally:session.close()
    return result

if __name__=='__main__':
    print(json.dumps(verify(),ensure_ascii=False,indent=2))
