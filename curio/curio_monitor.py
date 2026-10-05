"""Collectors adapted from the local Curio monitor; no browser automation."""
import base64, html, re, subprocess, time, urllib.parse, json
from datetime import datetime, timezone, timedelta
import requests
KA_UA = "Curio/1.0"
def parse(it, search_name):
    price = it.get("currentBidPrice") or it.get("price") or {}
    return {
        "item_id": it["itemId"],
        "search": search_name,
        "title": it.get("title", ""),
        "price": float(price["value"]) if price.get("value") is not None else None,
        "currency": price.get("currency", "EUR"),
        "buying": ",".join(it.get("buyingOptions", [])),
        "url": it.get("itemWebUrl", ""),
        "image": (it.get("image") or {}).get("imageUrl", ""),
        "created": _ebay_date(it.get("itemCreationDate", "")),
        "ends": it.get("itemEndDate", ""),
        "condition": it.get("condition", ""),
        "location": "", "price_text": "", "platform": "ebay",
        "country": (it.get("itemLocation") or {}).get("country", ""),
    }

def _iso_utc(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")

def _ebay_date(v):
    """'2026-09-28T10:03:11.000Z' -> '2026-09-28T10:03:11+00:00'（和库里其他时间格式一致，方便排序）"""
    try:
        return _iso_utc(datetime.fromisoformat(v.replace("Z", "+00:00")))
    except (AttributeError, ValueError):
        return ""

def _ka_date(chunk):
    """Kleinanzeigen 卡片上的时间：'Heute, 14:05' / 'Gestern, 09:12' / '26.09.2026' → UTC ISO"""
    m = re.search(r'aditem-main--top--right[^>]*>([\s\S]{0,300}?)</div>', chunk)
    txt = _clean(re.sub(r"<[^>]+>", " ", m.group(1))) if m else _clean(re.sub(r"<[^>]+>", " ", chunk))
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("Europe/Berlin")
    except Exception:
        tz = timezone(timedelta(hours=2))
    now = datetime.now(tz)
    hm = re.search(r"(\d{1,2}):(\d{2})", txt)
    h, mi = (int(hm.group(1)), int(hm.group(2))) if hm else (12, 0)
    if "Heute" in txt:
        return _iso_utc(now.replace(hour=h, minute=mi, second=0, microsecond=0))
    if "Gestern" in txt:
        return _iso_utc((now - timedelta(days=1)).replace(hour=h, minute=mi, second=0, microsecond=0))
    d = re.search(r"(\d{2})\.(\d{2})\.(\d{4})", txt)
    if d:
        return _iso_utc(datetime(int(d.group(3)), int(d.group(2)), int(d.group(1)), h, mi, tzinfo=tz))
    return ""

def _clean(text):
    return re.sub(r"\s+", " ", html.unescape(text or "")).strip()

def _ka_price(text):
    """'1.250 € VB' -> 1250.0；'VB' / 'Zu verschenken' -> 0"""
    m = re.search(r"([\d.]+(?:,\d{1,2})?)\s*€", text or "")
    if not m:
        return 0.0 if "verschenken" in (text or "").lower() else None
    return float(m.group(1).replace(".", "").replace(",", "."))

def ka_curl(url, extra=()):
    cmd = ["curl", "-sS", "-L", "--compressed", "--max-time", "45",
           "-w", "\n__STATUS__:%{http_code} %{url_effective}",
           "-H", f"User-Agent: {KA_UA}",
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: de-DE,de;q=0.9,en;q=0.8", *extra, url]
    res = subprocess.run(cmd, capture_output=True)
    if res.returncode != 0:
        return 0, "", "", f"curl 失败: {(res.stderr or b'').decode(errors='ignore')[:200]}"
    body, _, tail = res.stdout.rpartition(b"\n__STATUS__:")
    parts = tail.decode(errors="ignore").strip().split(" ", 1)
    status = int(parts[0]) if parts and parts[0].isdigit() else 0
    final_url = parts[1] if len(parts) > 1 else url
    return status, body.decode("utf-8", errors="ignore"), final_url, None

def fetch_kleinanzeigen(keyword, page=1):
    """抓 Kleinanzeigen 搜索结果（按最新排序）。沿用 em watch 的做法：curl + HTML 解析。"""
    path = urllib.parse.quote(keyword.strip().replace(" ", "-"))
    if page <= 1:
        url = f"https://www.kleinanzeigen.de/s-{path}/k0?sortingField=SORTING_DATE"
    else:
        url = f"https://www.kleinanzeigen.de/s-seite:{page}/{path}/k0?sortingField=SORTING_DATE"
    status, raw, _, err = ka_curl(url)
    if err:
        return None, err
    if status >= 400 or status == 0:
        return None, f"被拦截 (HTTP {status})"
    if blocked(raw):
        return None, "人机验证，暂停两小时"
    if "data-adid=" not in raw:
        if re.search(r"Keine (?:Anzeigen|Ergebnisse)|keine Treffer",raw,re.I):return [], None
        return None, "搜索页结构变化，未能解析商品"

    chunks = re.split(r'<article[^>]*class="[^"]*aditem', raw, flags=re.I)[1:]
    if not chunks:
        chunks = [p for p in re.split(r'(?=<article\b[^>]*\bdata-adid=")', raw, flags=re.I)
                  if re.search(r'\bdata-adid="\d+"', p)]
    items = []
    for c in chunks:
        id_m = re.search(r'data-adid="(\d+)"', c)
        href_m = re.search(r'(?:data-href|href)="(/s-anzeige/[^"]+)"', c)
        if not id_m or not href_m:
            continue
        href = html.unescape(href_m.group(1)).split("?")[0]
        t = (re.search(r'class="[^"]*ellipsis[^"]*"[^>]*>([^<]+)<', c)
             or re.search(r'<h3[^>]*>[\s\S]*?<a[^>]+href="/s-anzeige/[^"]+"[^>]*>([^<]+)</a>', c))
        title = _clean(t.group(1)) if t else _clean(href.split("/s-anzeige/")[-1].split("/")[0].replace("-", " "))
        p = (re.search(r'aditem-main--middle--price-shipping--price[^>]*>\s*([^<]+?)\s*<', c)
             or re.search(r'text-title3\s+font-strong\s+text-secondary[^>]*>\s*([^<]+?)\s*<', c))
        price_text = _clean(p.group(1)) if p else ""
        img_m = re.search(r'data-src="(https://[^"]+)"|src="(https://img\.kleinanzeigen[^"]+)"', c)
        img = html.unescape(img_m.group(1) or img_m.group(2)) if img_m else ""
        img = re.sub(r"([?&]rule=)\$_2(?:\.AUTO)?", r"\1$_57.AUTO", img)  # 换成大图
        loc_m = (re.search(r'class="[^"]*aditem-main--top--left[^"]*"[^>]*>([\s\S]*?)</', c)
                 or re.search(r'data-title="locationOutline"[\s\S]{0,400}?<span>([^<]+)</span>', c))
        items.append({
            "item_id": f"ka:{id_m.group(1)}", "search": keyword, "title": title,
            "price": _ka_price(price_text), "currency": "EUR",
            "buying": "VB" if "VB" in price_text else "FIXED_PRICE",
            "url": "https://www.kleinanzeigen.de" + href, "image": img,
            "created": _ka_date(c), "ends": "", "condition": "",
            "location": _clean(re.sub(r"<[^>]+>", " ", loc_m.group(1))) if loc_m else "",
            "price_text": price_text, "platform": "kleinanzeigen", "country": "DE",
        })
    return (items, None) if items else (None, "搜索页结构变化，未能解析商品")

class CollectionError(Exception):
    """Sanitized error: response bodies, headers and credentials are never exposed."""

class EbayAPI:
    def __init__(self, root):
        from .config import credentials
        self.client_id,self.secret=credentials(root)
        self.session=requests.Session();self.token=None
    def _request(self,method,url,**kwargs):
        try:return self.session.request(method,url,timeout=30,**kwargs)
        except requests.RequestException:raise CollectionError('eBay 网络请求失败') from None
    def _authorize(self):
        r=self._request('POST','https://api.ebay.com/identity/v1/oauth2/token',
            auth=(self.client_id,self.secret),data={'grant_type':'client_credentials','scope':'https://api.ebay.com/oauth/api_scope'})
        if r.status_code!=200:raise CollectionError('eBay 授权失败，请检查本地凭据')
        try:self.token=r.json()['access_token']
        except (ValueError,KeyError):raise CollectionError('eBay 授权响应无效') from None
    def get(self,path,params=None):
        if not self.token:self._authorize()
        r=self._request('GET','https://api.ebay.com/buy/browse/v1/'+path,
            headers={'Authorization':'Bearer '+self.token,'X-EBAY-C-MARKETPLACE-ID':'EBAY_DE'},params=params)
        if r.status_code in (404,410):return None
        if r.status_code!=200:raise CollectionError('eBay 请求失败，HTTP '+str(r.status_code))
        try:return r.json()
        except ValueError:raise CollectionError('eBay 响应无效') from None
    def search(self,keyword,offset,limit):
        data=self.get('item_summary/search',{'q':keyword,'filter':'itemLocationCountry:DE,buyingOptions:{FIXED_PRICE|AUCTION}','sort':'newlyListed','limit':limit,'offset':offset})
        if data is None:raise CollectionError('eBay 搜索不可用')
        return data.get('itemSummaries',[])
    def detail(self,iid):return self.get('item/'+urllib.parse.quote(iid,safe=''))

def collect_ebay(store,api,config):
    """A global 1,000-item initial cap; later stop at the first previously known ID."""
    import json
    initial=store.get('ebay_initialized')!='1'
    checkpoint=store.get('ebay_incremental_known')
    known=set(json.loads(checkpoint)) if checkpoint else store.known()
    if not initial and not checkpoint:store.set('ebay_incremental_known',json.dumps(sorted(known)))
    progress=json.loads(store.get('ebay_backfill','{"keyword":0,"offset":0,"count":0}'))
    added=0
    for ki,keyword in enumerate(config['keywords']):
        if initial and ki<progress['keyword']:continue
        offset=progress['offset'] if initial and ki==progress['keyword'] else 0
        while offset<10000:
            limit=min(200,1000-progress['count']) if initial else 200
            if limit<=0:break
            rows=api.search(keyword,offset,limit)
            stopped=False
            for raw in rows[:limit]:
                if not initial and raw['itemId'] in known:
                    stopped=True;break
                if store.ingest(parse(raw,keyword),config)=='new':added+=1
            if initial:
                progress.update(keyword=ki,offset=offset+len(rows),count=progress['count']+len(rows))
                store.set('ebay_backfill',json.dumps(progress))
            if stopped or len(rows)<limit:break
            offset+=len(rows)
        if initial:
            progress.update(keyword=ki+1,offset=0)
            store.set('ebay_backfill',json.dumps(progress))
            if progress['count']>=1000:break
    store.set('ebay_initialized','1')
    with store.connect() as c:c.execute("DELETE FROM kv WHERE k='ebay_incremental_known'")
    return added

def blocked(raw):
    return bool(re.search(r'captcha|access denied|verify you are human|sicherheitsüberprüfung|robot check',raw,re.I))

def collect_ka(store,config):
    if time.time()<float(store.get('ka_pause_until','0')):
        store.set('ka_status','paused');return 0
    store.set('ka_status','running')
    checkpoint=store.get('ka_incremental_known')
    known=set(json.loads(checkpoint)) if checkpoint else store.known()
    if not checkpoint:store.set('ka_incremental_known',json.dumps(sorted(known)))
    added=0;reached_limit=False
    for keyword in config['keywords']:
        for page in range(1,21):
            rows,error=fetch_kleinanzeigen(keyword,page)
            if error:
                is_block='拦截' in error or '人机验证' in error
                if is_block:store.set('ka_pause_until',time.time()+7200)
                store.set('ka_status','paused' if is_block else 'error')
                store.set('ka_error',error)
                return added
            # Old featured ads must not hide new organic listings later on the page.
            if not rows:break
            has_unseen=False
            for row in rows:
                if row['item_id'] in known:continue
                has_unseen=True
                if store.ingest(row,config)=='new':added+=1
            if not has_unseen:break
            if page==20:reached_limit=True
            else:time.sleep(1)
    store.set('ka_status','page_limit' if reached_limit else 'ok')
    with store.connect() as c:
        c.execute("DELETE FROM kv WHERE k IN ('ka_incremental_known','ka_error')")
    return added

def check_existing(store,api,config):
    with store.connect() as c:
        rows=[dict(r) for r in c.execute("SELECT * FROM items WHERE status='active' ORDER BY checked_at LIMIT ?",(config['status_check_limit'],))]
    for row in rows:
        if row['platform']=='ebay':
            if api is None:continue
            detail=api.detail(row['item_id'])
            if detail is None or any(a.get('estimatedAvailabilityStatus')=='OUT_OF_STOCK' for a in detail.get('estimatedAvailabilities',[])):
                store.mark_gone(row['item_id']);continue
            row.update(parse(detail,''));row['description']=detail.get('shortDescription',row.get('description',''))
            store.ingest(row,config)
        else:
            if not config['kleinanzeigen_enabled'] or time.time()<float(store.get('ka_pause_until','0')):continue
            status,raw,url,error=ka_curl(row['url'])
            if status in (404,410):store.mark_gone(row['item_id']);continue
            if status in (403,429) or blocked(raw):store.set('ka_pause_until',time.time()+7200);continue
            if error or status!=200:continue
            if re.search(r'Anzeige ist nicht mehr verfügbar|Anzeige wurde gelöscht',raw,re.I):
                store.mark_gone(row['item_id']);continue
            match=re.search(r'id="viewad-price"[^>]*>([\s\S]*?)</',raw)
            if match:
                row['price_text']=_clean(re.sub('<[^>]+>',' ',match.group(1)))
                row['price']=_ka_price(row['price_text']);store.ingest(row,config)
            else:
                from .store import now
                with store.connect() as c:c.execute('UPDATE items SET checked_at=? WHERE item_id=?',(now(),row['item_id']))

def run(root):
    from .config import load
    from .store import Store,now
    import uuid
    store=Store(root/'data/curio.db');config=load(root);owner=uuid.uuid4().hex
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        lock=c.execute("SELECT v FROM kv WHERE k='collect_lock_until'").fetchone()
        if lock and float(lock[0])>time.time():return {'status':'already_running'}
        c.execute("INSERT OR REPLACE INTO kv VALUES('collect_lock_until',?)",(str(time.time()+3600),))
        c.execute("INSERT OR REPLACE INTO kv VALUES('collect_lock_owner',?)",(owner,))
    result={};api=None
    with store.connect() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS collection_runs(
          id INTEGER PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,result TEXT)''')
        run_id=c.execute('INSERT INTO collection_runs(started_at,status) VALUES(?,?)',(now(),'running')).lastrowid
    try:
        try:
            api=EbayAPI(root);result['ebay_new']=collect_ebay(store,api,config);result['ebay_status']='ok'
        except Exception:
            result['ebay_status']='error';result['ebay']='采集未完成：检查网络、API 权限或 .env'
        if config['kleinanzeigen_enabled']:
            try:
                result['kleinanzeigen_new']=collect_ka(store,config)
                result['kleinanzeigen_status']=store.get('ka_status')
                if store.get('ka_error'):result['kleinanzeigen_error']=store.get('ka_error')
            except Exception:result['kleinanzeigen_status']='error'
        else:result['kleinanzeigen_status']='disabled'
        try:check_existing(store,api,config)
        except Exception:result['status_checks']='本轮状态检查未完成，将在后续轮次重试'
        store.archive();store.set('last_collection',now())
        import json
        store.set('last_collection_result',json.dumps(result,ensure_ascii=False))
        with store.connect() as c:
            status='ok' if result.get('ebay_status')=='ok' and result.get('kleinanzeigen_status') in ('ok','disabled') and 'status_checks' not in result else 'partial'
            c.execute('UPDATE collection_runs SET finished_at=?,status=?,result=? WHERE id=?',
                      (now(),status,json.dumps(result,ensure_ascii=False),run_id))
        return result
    finally:
        with store.connect() as c:
            lock=c.execute("SELECT v FROM kv WHERE k='collect_lock_owner'").fetchone()
            if lock and lock[0]==owner:c.execute("DELETE FROM kv WHERE k IN ('collect_lock_until','collect_lock_owner')")

if __name__=='__main__':
    from .config import ROOT
    import json
    print(json.dumps(run(ROOT),ensure_ascii=False))
