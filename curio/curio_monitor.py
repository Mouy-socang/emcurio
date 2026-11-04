"""Collectors adapted from the local Curio monitor; no browser automation."""
import base64, html, logging, os, re, subprocess, time, urllib.parse, json
from pathlib import Path
from datetime import datetime, timezone, timedelta
import requests
from .errors import CollectionError
KA_UA = "Curio/1.0"
log = logging.getLogger(__name__)
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


class EbayAPI:
    def __init__(self,root,budget=None,store=None):
        from .config import credentials
        self.client_id,self.secret=credentials(root)
        self.session=requests.Session();self.token=None
        # AD-27：每个外部请求（含 OAuth）先计入当日预算；purpose 为 fetch（新货）或 check（在售检查）。
        self.budget=budget;self.purpose='fetch'
        # OAuth 令牌缓存在 data/ebay_oauth.json（gitignore，权限 0600），到期前 60 秒内复用，避免每次唤醒都换令牌。
        # 凭据类数据不进数据库（spine）：只在传入 store 时启用缓存，并清掉旧版写在 kv 里的 ebay_oauth。
        self.cache_path=Path(root)/'data/ebay_oauth.json' if store is not None else None;self._cached=False
        if store is not None:
            with store.connect() as c:c.execute("DELETE FROM kv WHERE k='ebay_oauth'")
            try:
                cache=json.loads(self.cache_path.read_text())
                if cache.get('token') and float(cache.get('expires_at',0))-60>time.time():
                    self.token=cache['token'];self._cached=True
            except (OSError,ValueError,TypeError,AttributeError):pass
    def _request(self,method,url,**kwargs):
        if self.budget is not None:self.budget.spend('ebay',self.purpose)
        try:return self.session.request(method,url,timeout=30,**kwargs)
        except requests.RequestException:raise CollectionError('eBay 网络请求失败') from None
    def _authorize(self):
        r=self._request('POST','https://api.ebay.com/identity/v1/oauth2/token',
            auth=(self.client_id,self.secret),data={'grant_type':'client_credentials','scope':'https://api.ebay.com/oauth/api_scope'})
        if r.status_code!=200:raise CollectionError('eBay 授权失败，请检查本地凭据')
        try:
            data=r.json();self.token=data['access_token'];expires=float(data.get('expires_in',7200))
        except (ValueError,KeyError,TypeError):raise CollectionError('eBay 授权响应无效') from None
        self._cached=False
        if self.cache_path is not None:
            self.cache_path.parent.mkdir(parents=True,exist_ok=True)
            fd=os.open(self.cache_path,os.O_WRONLY|os.O_CREAT|os.O_TRUNC,0o600)
            os.fchmod(fd,0o600)
            with os.fdopen(fd,'w') as f:json.dump({'token':self.token,'expires_at':time.time()+expires},f)
    def get(self,path,params=None):
        if not self.token:self._authorize()
        r=self._request('GET','https://api.ebay.com/buy/browse/v1/'+path,
            headers={'Authorization':'Bearer '+self.token,'X-EBAY-C-MARKETPLACE-ID':'EBAY_DE'},params=params)
        if r.status_code==401 and self._cached:
            # 缓存的令牌被提前作废：换一次新令牌再试。
            self._authorize()
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

def collect_ka(store,config,budget=None):
    from .schedule import BudgetExhausted
    if time.time()<float(store.get('ka_pause_until','0')):
        store.set('ka_status','paused');return 0
    if budget is not None and not budget.available('kleinanzeigen','fetch'):
        store.set('ka_status','budget_exhausted');return 0
    store.set('ka_status','running')
    checkpoint=store.get('ka_incremental_known')
    known=set(json.loads(checkpoint)) if checkpoint else store.known()
    if not checkpoint:store.set('ka_incremental_known',json.dumps(sorted(known)))
    added=0;reached_limit=False
    for keyword in config['keywords']:
        for page in range(1,21):
            if budget is not None:
                # 用完即停：保留本轮增量边界（ka_incremental_known），明天接着抓。
                try:budget.spend('kleinanzeigen','fetch')
                except BudgetExhausted:
                    store.set('ka_status','budget_exhausted');return added
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

def _ensure_check_events(store):
    with store.connect() as c:
        c.executescript("""CREATE TABLE IF NOT EXISTS status_check_events(
          id INTEGER PRIMARY KEY,item_id TEXT,platform TEXT,checked_at TEXT,outcome TEXT,evidence TEXT);
          CREATE INDEX IF NOT EXISTS status_check_item ON status_check_events(item_id,id);""")

def _record_check(store,iid,platform,outcome,evidence):
    from .store import now
    with store.connect() as c:c.execute('INSERT INTO status_check_events(item_id,platform,checked_at,outcome,evidence) VALUES(?,?,?,?,?)',(iid,platform,now(),outcome,evidence))

def _apply_etsy(store,row,outcome,evidence,listing,config):
    """把一件 Etsy 货的批量判定落库：下架标 gone，在售刷新价格。落库失败时改记 error、保持原状态。"""
    from .etsy import parse as etsy_parse
    try:
        if outcome=='gone':store.mark_gone(row['item_id'])
        elif outcome=='active':store.ingest(etsy_parse(listing,''),config)
    except Exception:outcome,evidence='error','status update failed; state retained'
    return outcome,evidence

KA_SOLD=re.compile(r'<button\b[^>]*\bdisabled\b[^>]*\bid="viewad-contact-button"|<button\b[^>]*\bid="viewad-contact-button"[^>]*\bdisabled\b')
def _check_one(store,row,api,config,budget=None):
    """单平台（eBay / Kleinanzeigen）判定一件货当前是否在售，并落库（下架 mark_gone、在售刷新价格）。
    返回 (outcome, evidence)：active / gone / paused / error。预算用完时抛 BudgetExhausted（未发出请求、不应写记录）。
    批量检查与单件复查共用。"""
    from .store import now
    from .schedule import BudgetExhausted
    platform=row['platform']
    outcome='error';evidence='status_unverified'
    try:
        if platform=='ebay':
            detail=api.detail(row['item_id'])
            if detail is None:outcome='gone';evidence='API HTTP 404/410'
            elif any(a.get('estimatedAvailabilityStatus')=='OUT_OF_STOCK' for a in detail.get('estimatedAvailabilities',[])):outcome='gone';evidence='API OUT_OF_STOCK; sale unknown'
            else:
                row.update(parse(detail,''));row['description']=detail.get('shortDescription',row.get('description',''))
                store.ingest(row,config);outcome='active';evidence='API detail available; no out-of-stock marker'
        else:
            if budget is not None:budget.spend('kleinanzeigen','check')
            status,raw,url,error=ka_curl(row['url'])
            if status in (403,429) or blocked(raw):
                store.set('ka_pause_until',time.time()+7200);outcome='paused';evidence='HTTP challenge/rate limit'
            elif error or status not in (200,404,410):evidence='network/HTTP failure; state retained'
            elif status in (404,410):outcome='gone';evidence='HTTP '+str(status)
            elif re.search(r'Anzeige ist nicht mehr verfügbar|Anzeige wurde gelöscht',raw,re.I):outcome='gone';evidence='explicit unavailable/deleted text; sale unknown'
            elif '/s-anzeige/' not in url or row['item_id'].removeprefix('ka:') not in url:evidence='redirect; original listing not verified'
            # 卖掉但没删的广告照样 HTTP 200，只是"发消息"按钮被禁用（2026-11-04 CO-0360）。
            elif KA_SOLD.search(raw):outcome='gone';evidence='contact button disabled; sold'
            else:
                match=re.search(r'id="viewad-price"[^>]*>([\s\S]*?)</',raw)
                if match:
                    row['price_text']=_clean(re.sub('<[^>]+>',' ',match.group(1)));row['price']=_ka_price(row['price_text']);store.ingest(row,config)
                else:
                    with store.connect() as c:c.execute('UPDATE items SET checked_at=? WHERE item_id=?',(now(),row['item_id']))
                outcome='active';evidence='original listing HTTP 200; no unavailable marker'
        if outcome=='gone':store.mark_gone(row['item_id'])
    except BudgetExhausted:raise
    except Exception:
        log.exception('在售判定失败：%s',row.get('item_id'))
        outcome='error';evidence='detail request failed; state retained'
    return outcome,evidence

def recheck_item(store,item_id,config,root=None,api=None,etsy=None,budget=None):
    """复查（CAP-2）：按平台即时确认这一件当前是否在售，计入当日在售检查预算（purpose=check）。
    与批量检查同一判定与落库：下架 mark_gone，并写一条 status_check_events。
    返回 dict(outcome, evidence)，outcome 为 active / gone，其余（unknown / error / paused / budget）表示暂时无法确认。
    预算用完、平台暂停或未启用时不发请求、不写记录。"""
    from .config import ROOT,etsy_credentials
    from .schedule import Budget,BudgetExhausted
    root=root or ROOT
    with store.connect() as c:row=c.execute('SELECT * FROM items WHERE item_id=?',(item_id,)).fetchone()
    if not row:raise ValueError('商品不存在')
    row=dict(row);platform=row['platform']
    if platform not in ('ebay','kleinanzeigen','etsy'):return dict(outcome='unknown',evidence='unsupported platform')
    if budget is None:budget=Budget(store,config['api_budget'])
    _ensure_check_events(store)
    if not budget.available(platform,'check'):return dict(outcome='budget',evidence='check budget exhausted')
    try:
        if platform=='etsy':
            from .etsy import EtsyAPI,judge_listings,auth_backoff_active
            if etsy is None:
                keys=etsy_credentials(root)
                if not keys or auth_backoff_active(store):return dict(outcome='unknown',evidence='Etsy unavailable')
                etsy=EtsyAPI(*keys,budget=budget,store=store)
            etsy.purpose='check'
            verdicts,error=judge_listings(etsy,[item_id])
            if isinstance(error,BudgetExhausted):return dict(outcome='budget',evidence='check budget exhausted')
            outcome,evidence,listing=verdicts[item_id]
            if outcome=='skipped':return dict(outcome='error',evidence='Etsy request failed')
            outcome,evidence=_apply_etsy(store,row,outcome,evidence,listing,config)
        else:
            if platform=='kleinanzeigen' and (not config['kleinanzeigen_enabled'] or time.time()<float(store.get('ka_pause_until','0'))):
                return dict(outcome='paused',evidence='Kleinanzeigen paused or disabled')
            if platform=='ebay':
                if api is None:api=EbayAPI(root,budget,store)
                api.purpose='check'
            outcome,evidence=_check_one(store,row,api,config,budget)
    except BudgetExhausted:return dict(outcome='budget',evidence='check budget exhausted')
    except Exception:
        log.exception('复查失败：%s',item_id)
        return dict(outcome='error',evidence='recheck failed; state retained')
    _record_check(store,item_id,platform,outcome,evidence)
    return dict(outcome=outcome,evidence=evidence)

def check_existing(store,api,config,budget=None,etsy=None):
    """Rotate independently of search ingestion; retain failures as unknown.

    每轮上限 status_check_limit 在还有检查预算的平台间平分；有预算时每平台本轮份额再不超过
    "剩余检查预算 ÷ 到柏林午夜还剩的检查轮数"（至少 1），让检查份额撑满一整天。Etsy 按 100 个编号一批查询。
    预算用完而没发出的检查不写检查记录、不算错误，计入 budget_skipped。单件判定见 _check_one（复查共用）。"""
    from .schedule import BudgetExhausted,check_rounds_left
    from .etsy import BATCH
    summary=dict(attempted=0,active=0,gone=0,errors=0,paused=0,unknown=0,budget_skipped=0)
    exhausted=set()
    for client in (api,etsy):
        if client is not None:client.purpose='check'
    def event(iid,platform,outcome,evidence):
        summary['errors' if outcome=='error' else outcome]+=1
        _record_check(store,iid,platform,outcome,evidence)
    _ensure_check_events(store)
    with store.connect() as c:
        # Search result ingestion updates checked_at even when it never read the detail.
        # A separate attempt clock prevents those fresh search hits from starving.
        rows=[dict(r) for r in c.execute("""SELECT i.* FROM items i WHERE i.status='active'
          ORDER BY COALESCE((SELECT max(e.checked_at) FROM status_check_events e WHERE e.item_id=i.item_id),'') ASC,
          CASE WHEN i.agent_decision IN ('want','probe') AND NOT EXISTS(SELECT 1 FROM market_decisions m WHERE m.item_id=i.item_id AND m.irrelevant=1) THEN 0 ELSE 1 END,
          i.checked_at,i.item_id""")]
    ka_usable=config['kleinanzeigen_enabled'] and time.time()>=float(store.get('ka_pause_until','0'))
    candidates=[p for p,ok in (('ebay',api is not None),('kleinanzeigen',ka_usable),('etsy',etsy is not None)) if ok]
    if budget is not None:exhausted.update(p for p in candidates if not budget.available(p,'check'))
    usable=[p for p in candidates if p not in exhausted]
    limit=config['status_check_limit'];base=max(1,-(-limit//max(1,len(usable))))
    share={}
    for p in usable:
        share[p]=base
        if budget is not None:
            per_round=max(1,budget.check_remaining(p)//check_rounds_left(budget.clock()))
            share[p]=min(base,per_round*BATCH if p=='etsy' else per_round)
    used={};started=time.monotonic()
    if 'etsy' in share and limit>0:
        try:
            from .etsy import judge_listings
            picks=[r for r in rows if r['platform']=='etsy'][:min(share['etsy'],limit)]
            verdicts,error=judge_listings(etsy,[r['item_id'] for r in picks]) if picks else ({},None)
            if isinstance(error,BudgetExhausted):exhausted.add('etsy')
            elif error is not None:summary['etsy_error']=str(error)
            for row in picks:
                outcome,evidence,listing=verdicts[row['item_id']]
                if outcome=='skipped':
                    # 所在批次没查成：不写检查记录，保持原状态与轮换位置。
                    if isinstance(error,BudgetExhausted):summary['budget_skipped']+=1
                    continue
                summary['attempted']+=1;used['etsy']=used.get('etsy',0)+1
                outcome,evidence=_apply_etsy(store,row,outcome,evidence,listing,config)
                event(row['item_id'],'etsy',outcome,evidence)
        except Exception:summary['etsy_error']='Etsy 在售检查未完成，将在后续轮次重试'
    for row in rows:
        if summary['attempted']>=limit or time.monotonic()-started>=240:break
        platform=row['platform']
        if platform=='etsy' or platform in exhausted or platform not in share:continue
        if used.get(platform,0)>=share[platform]:continue
        if platform=='kleinanzeigen' and time.time()<float(store.get('ka_pause_until','0')):continue
        summary['attempted']+=1;used[platform]=used.get(platform,0)+1
        try:outcome,evidence=_check_one(store,row,api,config,budget)
        except BudgetExhausted:
            # 预算中途用完：这件没查，不写记录、不算错误，本平台本轮其余货也不再查。
            exhausted.add(platform);summary['attempted']-=1;used[platform]-=1;summary['budget_skipped']+=1
            continue
        event(row['item_id'],platform,outcome,evidence)
    if exhausted:summary['budget_exhausted']=sorted(exhausted)
    with store.connect() as c:summary['remaining_active']=c.execute("SELECT count(*) FROM items WHERE status='active'").fetchone()[0]
    return summary

PUBLISHED_PLATFORMS=('ebay','etsy')     # Kleinanzeigen 的货不会上线（FR-6），不查
PUBLISHED_SECONDS=240

def published_rows(store):
    """当前可能在线的货（已送达 + 待送达快照里、未隐藏、仍在售），按平台分组；最久没查的在前。"""
    from .publish import _has_table
    out={p:[] for p in PUBLISHED_PLATFORMS}
    _ensure_check_events(store)
    with store.connect() as c:
        if not _has_table(c,'published'):return out
        for r in c.execute("""SELECT i.* FROM published p JOIN items i ON i.item_id=p.item_id
            WHERE p.hidden=0 AND i.status='active' AND i.platform IN ('ebay','etsy')
            ORDER BY COALESCE((SELECT max(e.checked_at) FROM status_check_events e WHERE e.item_id=i.item_id),'') ASC,
            p.code"""):
            out[r['platform']].append(dict(r))
    return out

def check_published(store,api,config,budget=None,etsy=None,rows=None,sched=None,monotonic=time.monotonic):
    """已发布货下架检查（CAP-2、AD-10、AD-27）：只查当前可能在线的货，计入当天在售检查预算（purpose=check），
    在普通在售检查之前占用、不受每轮份额限制。单件判定复用 _check_one / Etsy 批量判定（下架 mark_gone，
    同一事务入发件箱隐藏）。某平台本轮全部判定完成（预算未耗尽、无请求失败、未超时）才记该平台最近成功时间；
    没有已发布货的平台视为"无需检查"、同样记为成功。返回计数摘要（published_check）。"""
    from .schedule import BudgetExhausted,Scheduler
    sched=sched or Scheduler(store)
    rows=published_rows(store) if rows is None else rows
    summary=dict(items=sum(len(v) for v in rows.values()),attempted=0,active=0,gone=0,errors=0,unknown=0,paused=0,
                 budget_skipped=0,platforms={})
    started=monotonic()
    _ensure_check_events(store)
    def event(row,outcome,evidence):
        summary['attempted']+=1
        summary['errors' if outcome=='error' else outcome]+=1
        _record_check(store,row['item_id'],row['platform'],outcome,evidence)
    for platform in PUBLISHED_PLATFORMS:
        todo=rows.get(platform,[]);ok=True;note='ok'
        client=api if platform=='ebay' else etsy
        if not todo:note='nothing_published'
        elif client is None:ok=False;note='unavailable'
        elif budget is not None and not budget.available(platform,'check'):
            ok=False;note='budget_exhausted';summary['budget_skipped']+=len(todo)
        elif platform=='etsy':
            # 按 100 个编号一批，每批之前检查时限；失败、预算用完、未返回（unknown）都不算本轮成功。
            from .etsy import judge_listings,BATCH
            etsy.purpose='check'
            for start in range(0,len(todo),BATCH):
                batch=todo[start:start+BATCH]
                if monotonic()-started>=PUBLISHED_SECONDS:
                    ok=False;note='timeout';break
                try:verdicts,error=judge_listings(etsy,[r['item_id'] for r in batch])
                except Exception:
                    log.exception('已发布货检查（Etsy）失败');verdicts,error={r['item_id']:('skipped','',None) for r in batch},'error'
                if error is not None:
                    ok=False;note='budget_exhausted' if isinstance(error,BudgetExhausted) else 'error'
                for row in batch:
                    outcome,evidence,listing=verdicts[row['item_id']]
                    if outcome=='skipped':
                        ok=False
                        if isinstance(error,BudgetExhausted):summary['budget_skipped']+=1
                        continue
                    outcome,evidence=_apply_etsy(store,row,outcome,evidence,listing,config)
                    if outcome=='error':ok=False;note='error'
                    elif outcome=='unknown':ok=False;note='unknown' if note=='ok' else note
                    event(row,outcome,evidence)
                if isinstance(error,BudgetExhausted):
                    summary['budget_skipped']+=len(todo)-start-len(batch);break
        else:
            api.purpose='check'
            for n,row in enumerate(todo):
                if monotonic()-started>=PUBLISHED_SECONDS:
                    ok=False;note='timeout';break
                try:outcome,evidence=_check_one(store,row,api,config,budget)
                except BudgetExhausted:
                    ok=False;note='budget_exhausted';summary['budget_skipped']+=len(todo)-n;break
                if outcome in ('error','paused'):ok=False;note='error'
                event(row,outcome,evidence)
        if ok:sched.record_published_ok(platform)
        summary['platforms'][platform]=dict(items=len(todo),ok=ok,status=note)
    return summary

POOL_PLATFORMS=('ebay','kleinanzeigen','etsy')
POOL_MIN=5              # 每轮每平台商品池检查份额至少 5 件（仍受预算）
POOL_SECONDS=240
ARCHIVE_SECONDS=90      # 每轮商品池存档（快照）限时，未完成的下轮继续
ARCHIVE_ATTEMPTS=5      # 一件货存档最多尝试几轮（平台已删图的已下架货不会无休止重试）

def pool_rows(store,hours,clock=time.time):
    """商品池（拣选留下）里仍在售、距上次确定判定（status_check_events 最近一条 active / gone）≥ hours 小时的
    eBay / Kleinanzeigen / Etsy 货，最久没查的在前（从没查成过的最前）；失败、暂停、无法确认的记录不算，下轮即重试。
    已在已发布货检查里的货（published 未隐藏的 eBay / Etsy 货）不在这里。"""
    from .publish import _has_table
    _ensure_check_events(store)
    cutoff=_iso_utc(datetime.fromtimestamp(clock()-hours*3600,timezone.utc))
    with store.connect() as c:
        if not _has_table(c,'triage'):return []
        pub=("AND NOT EXISTS(SELECT 1 FROM published p WHERE p.item_id=i.item_id AND p.hidden=0 AND i.platform IN ('ebay','etsy'))"
             if _has_table(c,'published') else '')
        rows=[dict(r) for r in c.execute(f"""SELECT * FROM (SELECT i.*,
              COALESCE((SELECT max(e.checked_at) FROM status_check_events e WHERE e.item_id=i.item_id
                        AND e.outcome IN ('active','gone')),'') pool_last_check
              FROM triage t JOIN items i ON i.item_id=t.item_id
              WHERE t.state='kept' AND i.status='active' AND i.platform IN ('ebay','kleinanzeigen','etsy') {pub})
            WHERE pool_last_check<? ORDER BY pool_last_check ASC,item_id""",(cutoff,))]
    for r in rows:r.pop('pool_last_check',None)
    return rows

def _ka_usable(store,config,clock=time.time):
    return bool(config['kleinanzeigen_enabled']) and clock()>=float(store.get('ka_pause_until','0'))

def pool_platform_ok(store,platform,config,budget,clock=time.time,ebay=True,etsy=True):
    """本轮能否查该平台的商品池货：客户端可用（eBay 凭据、Etsy 启用且未鉴权退避、Kleinanzeigen 启用且未暂停）且还有检查预算。"""
    if platform=='ebay':ok=ebay
    elif platform=='etsy':ok=etsy
    elif platform=='kleinanzeigen':ok=_ka_usable(store,config,clock)
    else:ok=False
    return bool(ok) and (budget is None or budget.available(platform,'check'))

def _pool_share(budget,platform):
    """本轮商品池检查份额：剩余在售检查预算 ÷ 到柏林午夜还剩的检查轮数 的一半，至少 POOL_MIN 件（Etsy 按 100 件一次请求折算）。"""
    from .schedule import check_rounds_left
    from .etsy import BATCH
    if budget is None:return None
    n=max(POOL_MIN,budget.check_remaining(platform)//check_rounds_left(budget.clock())//2)
    return n*BATCH if platform=='etsy' else n

def check_pool(store,api,config,budget=None,etsy=None,rows=None,clock=time.time,monotonic=time.monotonic,root=None):
    """商品池优先在售检查（故事 26）：普通在售轮转之前，先查商品池里到期（pool_rows）的货，计入在售检查预算（purpose=check）。
    每平台本轮最多 _pool_share 件，其余留给普通轮转、下轮再查；预算用完即停（计 budget_skipped，不算错误）；
    超时或 Kleinanzeigen 中途被拦而没查的计 deferred。份额内：attempted + budget_skipped + deferred + 未返回的 Etsy 货 = 选中件数。
    判定与落库复用 _check_one / Etsy 批量判定；每件写一条 status_check_events。暂停、无法确认、请求失败都计 errors。
    判下架的货若还没有含本机图片的快照，立即做一次商品池存档（root 给出时；同样限时、受尝试次数上限）。
    返回 dict(attempted, active, gone, errors, budget_skipped, deferred, due)。"""
    from .schedule import BudgetExhausted
    rows=pool_rows(store,config['pool_check_hours'],clock) if rows is None else rows
    summary=dict(attempted=0,active=0,gone=0,errors=0,budget_skipped=0,deferred=0,due=len(rows))
    started=monotonic();gone=[]
    _ensure_check_events(store)
    def event(row,outcome,evidence):
        summary['attempted']+=1
        summary[outcome if outcome in ('active','gone') else 'errors']+=1
        _record_check(store,row['item_id'],row['platform'],outcome,evidence)
        if outcome=='gone':gone.append(row)
    for platform in POOL_PLATFORMS:
        todo=[r for r in rows if r['platform']==platform]
        if not todo:continue
        if platform=='ebay' and api is None:continue
        if platform=='etsy' and etsy is None:continue
        if platform=='kleinanzeigen' and not _ka_usable(store,config,clock):continue
        share=_pool_share(budget,platform)
        picks=todo if share is None else todo[:share]
        if platform=='etsy':
            from .etsy import judge_listings,BATCH
            etsy.purpose='check'
            for start in range(0,len(picks),BATCH):
                batch=picks[start:start+BATCH]
                if monotonic()-started>=POOL_SECONDS:
                    summary['deferred']+=len(picks)-start;break
                if budget is not None and not budget.available('etsy','check'):
                    summary['budget_skipped']+=len(picks)-start;break
                try:verdicts,error=judge_listings(etsy,[r['item_id'] for r in batch])
                except Exception:
                    log.exception('商品池检查（Etsy）失败');verdicts,error={r['item_id']:('skipped','',None) for r in batch},'error'
                for row in batch:
                    verdict=verdicts.get(row['item_id'])
                    if verdict is None:
                        summary['errors']+=1;continue     # 批量结果里漏了这件：不写记录，下轮重试
                    outcome,evidence,listing=verdict
                    if outcome=='skipped':
                        if isinstance(error,BudgetExhausted):summary['budget_skipped']+=1
                        else:summary['errors']+=1
                        continue
                    outcome,evidence=_apply_etsy(store,row,outcome,evidence,listing,config)
                    event(row,outcome,evidence)
                if isinstance(error,BudgetExhausted):
                    summary['budget_skipped']+=len(picks)-start-len(batch);break
            continue
        if platform=='ebay':api.purpose='check'
        for n,row in enumerate(picks):
            if monotonic()-started>=POOL_SECONDS:
                summary['deferred']+=len(picks)-n;break
            try:outcome,evidence=_check_one(store,row,api,config,budget)
            except BudgetExhausted:
                summary['budget_skipped']+=len(picks)-n;break
            event(row,outcome,evidence)
            if outcome=='paused':        # Kleinanzeigen 拦截：暂停两小时，本轮其余不再查
                summary['deferred']+=len(picks)-n-1;break
    if root is not None and gone:
        archive_started=monotonic()
        for row in gone:
            if monotonic()-archive_started>=ARCHIVE_SECONDS:break
            try:_archive_gone(store,row['item_id'],root,api,config,budget,clock)
            except Exception:log.exception('下架存档失败：%s',row['item_id'])
    return summary

def _ensure_archive_table(store):
    with store.connect() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS pool_archive(item_id TEXT PRIMARY KEY,attempts INTEGER DEFAULT 0,
          done INTEGER DEFAULT 0,last_at TEXT,last_error TEXT)""")

def _archive_state(c,iid):
    row=c.execute('SELECT attempts,done FROM pool_archive WHERE item_id=?',(iid,)).fetchone()
    return (row['attempts'] or 0,row['done'] or 0) if row else (0,0)

def _archive_one(store,item,root,api,budget):
    """为一件商品池的货做一次商品池存档（vision.snapshot，图片上限 12），记尝试次数。
    完成返回 True、没完成返回 False；预算用完而没做返回 None（不计尝试）。"""
    from . import vision
    from .store import now
    from .schedule import BudgetExhausted
    vision.init(store);_ensure_archive_table(store)
    err=None
    try:
        _,data=vision.snapshot(store,item,root,api=api,budget=budget,max_images=vision.POOL_IMAGES,pool=True)
        if not data.get('images'):err='；'.join(data.get('acquisition_errors') or [])[:200] or '没有可下载的图片'
        elif (data.get('pool_archive') or {}).get('download_failures'):err='；'.join(data.get('acquisition_errors') or [])[:200]
    except BudgetExhausted:return None
    except Exception as e:
        log.exception('商品池存档失败：%s',item['item_id']);err='存档失败：'+type(e).__name__
    with store.connect() as c:done=vision.pool_archived(c,item['item_id'],root)
    if not done and err is None:err='详情未取到，下轮重试'
    with store.connect() as c:
        c.execute("""INSERT INTO pool_archive(item_id,attempts,done,last_at,last_error) VALUES(?,1,?,?,?)
          ON CONFLICT(item_id) DO UPDATE SET attempts=attempts+1,done=excluded.done,last_at=excluded.last_at,last_error=excluded.last_error""",
                  (item['item_id'],int(done),now(),None if done else err))
    return done

def _archive_usable(store,platform,api,config,budget,clock=time.time):
    if platform not in ('ebay','kleinanzeigen'):return False     # Etsy 的货不做存档（Etsy 目前停用）
    return pool_platform_ok(store,platform,config,budget,clock,ebay=api is not None)

def _archive_gone(store,iid,root,api,config,budget,clock=time.time):
    """池检查判下架：还没有含本机图片的快照时立即存档一次（拿不到新图时 snapshot 沿用已有照片）；
    与 archive_pool 同样受尝试次数上限、已完成的不再做。"""
    from . import vision
    vision.init(store);_ensure_archive_table(store)
    with store.connect() as c:
        attempts,done=_archive_state(c,iid)
        if done or attempts>=ARCHIVE_ATTEMPTS or vision.has_local_snapshot(c,iid,root):return False
        item=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
    if item is None or not _archive_usable(store,item['platform'],api,config,budget,clock):return False
    if api is not None:api.purpose='check'
    return _archive_one(store,dict(item),root,api,budget)

def archive_pool(store,root,api,config,budget=None,monotonic=time.monotonic,clock=time.time):
    """商品池存档（故事 26）：为留下的 eBay / Kleinanzeigen 货（含已下架的）各做一次商品池快照——详情、描述、价格存档、
    最多 12 张图片下载到 data/porcelain/images（后台与发布随之用本机图，store.item_images 本就优先本机图）。Etsy 的货不存档。
    最近留下的先做；每轮限时 ARCHIVE_SECONDS，未完成的下轮继续；没成功（含有图片下载失败）的下轮重试，最多 ARCHIVE_ATTEMPTS 轮。
    详情请求计入在售检查预算；预算用完、平台暂停时跳过、不计尝试。返回 dict(attempted, archived, errors, pending)。"""
    from . import vision
    from .store import now
    vision.init(store);_ensure_archive_table(store)
    with store.connect() as c:
        rows=[dict(r) for r in c.execute("""SELECT i.* FROM triage t JOIN items i ON i.item_id=t.item_id
            LEFT JOIN pool_archive a ON a.item_id=i.item_id
            WHERE t.state='kept' AND i.platform IN ('ebay','kleinanzeigen') AND COALESCE(a.done,0)=0 AND COALESCE(a.attempts,0)<?
            ORDER BY t.updated_at DESC,i.item_id""",(ARCHIVE_ATTEMPTS,))]
    summary=dict(attempted=0,archived=0,errors=0,pending=len(rows))
    if api is not None:api.purpose='check'
    started=monotonic()
    for item in rows:
        if monotonic()-started>=ARCHIVE_SECONDS:break
        if not _archive_usable(store,item['platform'],api,config,budget,clock):continue
        with store.connect() as c:
            if vision.pool_archived(c,item['item_id'],root):
                c.execute("INSERT INTO pool_archive(item_id,attempts,done,last_at) VALUES(?,0,1,?) ON CONFLICT(item_id) DO UPDATE SET done=1",
                          (item['item_id'],now()))
                summary['pending']-=1;continue
        done=_archive_one(store,item,root,api,budget)
        if done is None:continue
        summary['attempted']+=1
        if done:summary['archived']+=1;summary['pending']-=1
        else:summary['errors']+=1
    return summary

AUCTION_ENDED_GRACE_HOURS=1
AUCTION_ENDED_EVIDENCE='auction ended (ends passed)'

def close_ended_auctions(store,clock=time.time,done=None):
    """故事 20：结束的拍卖判下架。纯拍卖（triage.PURE_AUCTION；已"转为普通货"的不算，交给普通在售检查）、在售、
    items.ends 早于现在 AUCTION_ENDED_GRACE_HOURS 小时以上的货，经 store.mark_gone 判下架（已发布的同一事务入队隐藏）；
    实际改为下架的每件写一条 status_check_events（evidence 'auction ended (ends passed)'）。不发任何请求。
    返回实际判下架的行（含拣选状态）；done 给出时逐件追加到它，中途出错时调用方仍拿得到已改的部分。"""
    from . import triage
    at=datetime.fromtimestamp(clock(),timezone.utc).isoformat(timespec='seconds')
    with store.connect() as c:
        rows=[dict(r) for r in c.execute("""SELECT i.item_id,i.platform,t.state FROM items i LEFT JOIN triage t ON t.item_id=i.item_id
            WHERE i.status='active' AND """+triage.PURE_AUCTION+""" AND julianday(NULLIF(i.ends,'')) IS NOT NULL
              AND julianday(NULLIF(i.ends,'')) < julianday(?) - ?/24.0 ORDER BY i.item_id""",(at,AUCTION_ENDED_GRACE_HOURS))]
    done=[] if done is None else done
    if not rows:return done
    _ensure_check_events(store)
    for row in rows:
        if not store.mark_gone_changed(row['item_id']):continue
        done.append(row)
        _record_check(store,row['item_id'],row['platform'],'gone',AUCTION_ENDED_EVIDENCE)
    return done

def archive_ended_auctions(store,rows,root,api,config,budget,clock=time.time,monotonic=time.monotonic):
    """结束的拍卖里商品池（留下）的货：与池检查判下架同样立即存档（_archive_gone，限时、受尝试次数上限）。"""
    started=monotonic()
    for row in rows:
        if row.get('state')!='kept':continue
        if monotonic()-started>=ARCHIVE_SECONDS:break
        try:_archive_gone(store,row['item_id'],root,api,config,budget,clock)
        except Exception:log.exception('下架存档失败：%s',row['item_id'])

FETCH_OK=('ok','disabled','not_due')

def run(root,clock=time.time):
    """launchd 每 5 分钟唤醒一次；调度器决定本轮是否抓新货、是否做已发布货下架检查、是否做普通在售检查（AD-27）。

    都不到期时只做一次同步（发件箱 + 心跳）就结束，不写 collection_runs。"""
    from .config import load,etsy_credentials
    from .store import Store,now
    from .schedule import Scheduler,Budget,BudgetExhausted
    from .etsy import EtsyAPI,EtsyAuthError,collect_etsy,auth_backoff_active,AUTH_BACKOFF_MESSAGE
    import uuid
    store=Store(root/'data/curio.db');config=load(root);owner=uuid.uuid4().hex
    with store.connect() as c:
        c.execute('BEGIN IMMEDIATE')
        lock=c.execute("SELECT v FROM kv WHERE k='collect_lock_until'").fetchone()
        if lock and float(lock[0])>time.time():return {'status':'already_running'}
        c.execute("INSERT OR REPLACE INTO kv VALUES('collect_lock_until',?)",(str(time.time()+3600),))
        c.execute("INSERT OR REPLACE INTO kv VALUES('collect_lock_owner',?)",(owner,))
    try:
        sched=Scheduler(store,clock);budget=Budget(store,config['api_budget'],clock)
        do_fetch=sched.fetch_due();do_check=sched.check_due()
        # 已发布货检查每次唤醒都判断是否到期（独立节拍）；没有可能在线的货时直接记为成功，不算本轮有工作。
        do_pub=sched.published_check_due();pub_rows=None
        if do_pub:
            pub_rows=published_rows(store)
            if not any(pub_rows.values()):
                sched.mark_published_check_started()
                for p in PUBLISHED_PLATFORMS:sched.record_published_ok(p)
                do_pub=False
        # 故事 23：当天第一轮（柏林 05:00 之后）封存系统预测；已封存不覆盖，失败只记结果字段、不影响采集。
        # 空闲轮在这里封存；有工作的轮次在抓取入库与打分之后封存（让当天第一批新货进预测）。
        from .agreement import seal_for_run
        # 故事 26：商品池里有到期（距上次检查满 pool_check_hours）的货，本轮就要查，不论抓取与普通检查是否到期。
        # 只算本轮查得了的平台（有客户端、未暂停、还有检查预算）的到期货，免得查不了的货让每轮都不空闲。
        try:
            from .config import credentials
            try:credentials(root);ebay_ok=True
            except Exception:ebay_ok=False
            etsy_ok=bool(etsy_credentials(root)) and not auth_backoff_active(store)
            pool_due=[r for r in pool_rows(store,config['pool_check_hours'],clock)
                      if pool_platform_ok(store,r['platform'],config,budget,clock,ebay_ok,etsy_ok)]
        except Exception:
            log.exception('商品池到期货读取失败');pool_due=[]
        do_pool=bool(pool_due)
        # 故事 20：结束的拍卖（结束时间已过 1 小时以上）判下架；每轮都做（空闲轮也做），不发请求，失败只记结果字段。
        ended=[]
        try:
            close_ended_auctions(store,clock,ended);auction_result={'auction_ended':len(ended)}
        except Exception:
            log.exception('结束拍卖判下架失败')
            auction_result={'auction_ended':len(ended),'auction_ended_error':'结束拍卖判下架未完成，将在后续轮次重试'}
        if not do_fetch and not do_check and not do_pub and not do_pool:
            # 池里的结束拍卖照常立即存档（故事 26 的下架即快照）：只在有这样的货时才建 eBay 客户端。
            if any(r.get('state')=='kept' for r in ended):
                try:archive_ended_auctions(store,ended,root,EbayAPI(root,budget,store),config,budget,clock)
                except Exception:log.exception('结束拍卖存档未完成')
            # 空闲轮也发心跳（AD-10）：同步模块每轮都把各平台最近成功时间发给服务器。
            from . import sync,learning
            # CAP-8：每轮归纳一次待审规律（只改 05 计数，不打断）。
            # 故事 27：封存之前重算全部货的综合分（失败只记结果字段）。
            from .scoring import refresh_for_run
            composite=refresh_for_run(store,root,clock())
            predicted=seal_for_run(store,clock())
            return {'status':'idle',**sched.summary(),**auction_result,**learning.run(store,root),**sync.run(store,root),**composite,**predicted}
        result={'fetch_ran':do_fetch,'status_check_ran':do_check,'published_check_ran':do_pub,'pool_check_ran':do_pool,**auction_result}
        with store.connect() as c:
            c.execute('''CREATE TABLE IF NOT EXISTS collection_runs(
              id INTEGER PRIMARY KEY,started_at TEXT,finished_at TEXT,status TEXT,result TEXT)''')
            run_id=c.execute('INSERT INTO collection_runs(started_at,status) VALUES(?,?)',(now(),'running')).lastrowid
        try:api=EbayAPI(root,budget,store)
        except Exception:api=None
        etsy_keys=etsy_credentials(root)
        etsy=EtsyAPI(*etsy_keys,budget=budget,store=store) if etsy_keys else None
        # 鉴权失败后退避 1 小时：期间 Etsy 不发任何请求（含在售检查）。
        etsy_backoff=etsy is not None and auth_backoff_active(store)
        if do_pub:
            # 优先级：复查 > 已发布货下架检查 > 新货抓取 > 普通在售检查（AD-27）：先于抓取占用在售检查预算。
            sched.mark_published_check_started()
            pub_etsy=None if etsy is None or etsy_backoff else etsy
            try:result['published_check']=check_published(store,api,config,budget,pub_etsy,pub_rows,sched)
            except Exception:
                log.exception('已发布货检查未完成');result['published_check_error']='已发布货检查未完成，将在后续轮次重试'
            # 容量：已发布件数 × 每天轮数超过在售检查份额时在结果里写警告，日志每天记一次。
            from .schedule import published_capacity,published_check_interval,launchd_interval,berlin_date
            warn=published_capacity(budget,{p:len(v) for p,v in pub_rows.items()},published_check_interval(launchd_interval()))
            if warn:
                result['published_check_warning']=warn
                today=berlin_date(clock())
                if store.get('published_capacity_warned')!=today:
                    store.set('published_capacity_warned',today);log.warning('已发布货检查容量不足：%s','；'.join(warn))
        if ended:
            try:archive_ended_auctions(store,ended,root,api,config,budget,clock)
            except Exception:log.exception('结束拍卖存档未完成')
        if do_pool:
            # 优先级：复查 > 已发布货检查 > 商品池检查 > 新货抓取 > 普通在售检查（故事 26）。
            pool_etsy=None if etsy is None or etsy_backoff else etsy
            try:result['pool_check']=check_pool(store,api,config,budget,pool_etsy,pool_due,clock,root=root)
            except Exception:
                log.exception('商品池检查未完成');result['pool_check_error']='商品池检查未完成，将在后续轮次重试'
        if do_fetch:
            sched.mark_fetch_started();fetch_started=now()
            if api is None:
                result['ebay_status']='error';result['ebay']='采集未完成：检查网络、API 权限或 .env'
            elif not budget.available('ebay','fetch'):result['ebay_status']='budget_exhausted'
            else:
                try:api.purpose='fetch';collect_ebay(store,api,config);result['ebay_status']='ok'
                except BudgetExhausted:result['ebay_status']='budget_exhausted'
                except Exception:
                    result['ebay_status']='error';result['ebay']='采集未完成：检查网络、API 权限或 .env'
            if config['kleinanzeigen_enabled']:
                try:
                    collect_ka(store,config,budget)
                    result['kleinanzeigen_status']=store.get('ka_status')
                    if store.get('ka_error'):result['kleinanzeigen_error']=store.get('ka_error')
                except Exception:result['kleinanzeigen_status']='error'
            else:result['kleinanzeigen_status']='disabled'
            if etsy is None:result['etsy_status']='disabled'
            elif etsy_backoff:result['etsy_status']='error';result['etsy']=AUTH_BACKOFF_MESSAGE
            elif not budget.available('etsy','fetch'):result['etsy_status']='budget_exhausted'
            else:
                try:etsy.purpose='fetch';collect_etsy(store,etsy,config);result['etsy_status']='ok'
                except BudgetExhausted:result['etsy_status']='budget_exhausted'
                except EtsyAuthError:result['etsy_status']='error';result['etsy']=AUTH_BACKOFF_MESSAGE
                except CollectionError as error:result['etsy_status']='error';result['etsy']=str(error)
                except Exception:result['etsy_status']='error';result['etsy']='Etsy 采集未完成，将在后续轮次重试'
            # 以入库时间计新货：采集中途失败或预算用完时，已入库的新货同样计入。
            with store.connect() as c:
                counts=dict(c.execute('SELECT platform,count(*) FROM items WHERE first_seen>=? GROUP BY platform',(fetch_started,)).fetchall())
            for platform in ('ebay','kleinanzeigen','etsy'):result[platform+'_new']=counts.get(platform,0)
            result['new_total']=sum(result[p+'_new'] for p in ('ebay','kleinanzeigen','etsy'))
            # 没有任何平台成功（都出错、暂停或预算用完）时不调间隔，免得把故障当成"没上新"。
            if any(result.get(p+'_status')=='ok' for p in ('ebay','kleinanzeigen','etsy')):
                sched.record_fetch(result['new_total'])
        else:
            result['ebay_status']='not_due' if api is not None else 'error'
            if api is None:result['ebay']='缺少 eBay 凭据：检查 .env'
            result['kleinanzeigen_status']='not_due' if config['kleinanzeigen_enabled'] else 'disabled'
            result['etsy_status']='disabled' if etsy is None else 'error' if etsy_backoff else 'not_due'
            if etsy_backoff:result['etsy']=AUTH_BACKOFF_MESSAGE
        if do_check:
            sched.mark_check_started()
            check_etsy=None if etsy is None or auth_backoff_active(store) else etsy
            try:result['status_check_summary']=check_existing(store,api,config,budget,check_etsy)
            except Exception:result['status_checks']='本轮状态检查未完成，将在后续轮次重试'
        # 故事 26：商品池存档（进池的货做一次快照、图片存到本机），限时，未完成的下轮继续；失败只记结果字段。
        try:result['pool_archive']=archive_pool(store,root,api,config,budget,clock=clock)
        except Exception:
            log.exception('商品池存档未完成');result['pool_archive_error']='商品池存档未完成，将在后续轮次重试'
        # CAP-4：抓取与在售检查之后，在 AI 预算内给新货打分；失败只记 ai_status，不影响其它结果。
        from . import ai
        result.update(ai.run(store,root,config,clock))
        # 故事 27：AI 打分之后、封存之前重算全部货的综合分（含 backlog、自动移出、已处理）；失败只记结果字段。
        from .scoring import refresh_for_run
        result.update(refresh_for_run(store,root,clock()))
        result.update(seal_for_run(store,clock()))
        # AD-14：每天最多成功取一次 ECB 人民币参考汇率（失败沿用上次的值与日期），发布时随快照上线。
        from . import fx
        result.update(fx.run(store,clock))
        # CAP-8（故事 17）：从墨墨的人工拣选归纳待审规律；失败只记 learning_status，不影响其它结果。
        from . import learning
        result.update(learning.run(store,root))
        # AD-23：限时 30 秒把发件箱按序推到服务器；未配置服务器时 sync_status: disabled，失败下轮从同一条重试。
        from . import sync
        result.update(sync.run(store,root))
        store.archive();store.set('last_collection',now())
        result['api_usage']=budget.snapshot();result.update(sched.summary())
        store.set('last_collection_result',json.dumps(result,ensure_ascii=False))
        with store.connect() as c:
            ok=all(result.get(p+'_status') in FETCH_OK for p in ('ebay','kleinanzeigen','etsy'))
            summary=result.get('status_check_summary',{})
            pub=result.get('published_check',{})
            pub_ok='published_check_error' not in result and not pub.get('errors')
            pool=result.get('pool_check',{})
            pub_ok=pub_ok and 'pool_check_error' not in result and not pool.get('errors')
            status='ok' if ok and pub_ok and 'status_checks' not in result and not summary.get('errors') and not summary.get('etsy_error') else 'partial'
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
