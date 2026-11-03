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

FETCH_OK=('ok','disabled','not_due')

def run(root,clock=time.time):
    """launchd 每 5 分钟唤醒一次；调度器决定本轮是否抓新货、是否做在售检查（AD-27）。

    两者都不到期时直接结束，不写 collection_runs。"""
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
        if not do_fetch and not do_check:
            return {'status':'idle',**sched.summary()}
        result={'fetch_ran':do_fetch,'status_check_ran':do_check}
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
        # CAP-4：抓取与在售检查之后，在 AI 预算内给新货打分；失败只记 ai_status，不影响其它结果。
        from . import ai
        result.update(ai.run(store,root,config,clock))
        store.archive();store.set('last_collection',now())
        result['api_usage']=budget.snapshot();result.update(sched.summary())
        store.set('last_collection_result',json.dumps(result,ensure_ascii=False))
        with store.connect() as c:
            ok=all(result.get(p+'_status') in FETCH_OK for p in ('ebay','kleinanzeigen','etsy'))
            summary=result.get('status_check_summary',{})
            status='ok' if ok and 'status_checks' not in result and not summary.get('errors') and not summary.get('etsy_error') else 'partial'
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
