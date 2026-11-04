"""本机后台服务（只绑定本机；手机经 tailscale serve 访问，AD-4）。打开页面不触发采集或模型调用。

`/` 为后台一页拣选（CAP-6，curio/triage.html + triage.js）：01 新货、02 商品池与 03 榜（故事 11，ranking.js）、
04 匹配（故事 16，curio/matching.py：按客人汇总、确认 / 不合适、复制单件页链接）、
05 待审核（故事 17，curio/learning.py：从人工拣选归纳的规律，确认 / 否决）、
06 客人（故事 14，curio/guests.py：账号关联、停用、重置密码、邀请码）、07 符合度（故事 23，curio/agreement.py：只读）；
旧审核页在 `/old`。`/preview/…` 为发布前预览（AD-28）：Mac 用客人页同一份模板渲染候选快照，只在后台可见，
页内只引用本机地址（CSP 同客人页），不上传、不入发件箱、不记录行为；发布只从预览横条发起。"""
import argparse,html as htmlmod,ipaddress,json,logging,re,secrets,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from .config import ROOT,load
from .store import Store

LOCAL_HOSTS=('127.0.0.1','localhost')
log=logging.getLogger(__name__)

def _hostname(value):
    """Host 头去掉端口后的小写主机名（不接受 IPv6 字面量）。"""
    value=(value or '').strip().lower()
    if not value or '[' in value or value.count(':')>1:return ''
    name,_,port=value.partition(':')
    if port and not port.isdigit():return ''
    return name

def host_allowed(value):
    """防 DNS 重绑定：只服务本机名（127.0.0.1 / localhost）或 tailnet 名（*.ts.net，tailscale serve）。"""
    name=_hostname(value)
    return name in LOCAL_HOSTS or (name.endswith('.ts.net') and len(name)>len('.ts.net'))

GUEST_CSP=("default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; form-action 'self'; "
           "frame-ancestors 'none'; base-uri 'none'")
PREVIEW_IMAGE=re.compile(r'[0-9a-f]{16}\.jpg')
PREVIEW_ITEM=re.compile(r'/item/CO-[0-9]{4,8}')

def _preview_names(payload):
    return {n for it in payload['items'] for im in it['images'] for n in (im['name'],im['thumb'])}

def preview_shell(page,token,rank):
    """在客人页 HTML 里加上预览横条（反色，置顶）：返回修改 / 发布。"""
    bar=('<div class="pvbar" id="pvbar" role="region" aria-label="预览" data-token="%s" data-rank="%s">'
         '<span class="pvt">预览 · 客人看到的样子</span><a class="pvlnk" href="/#rank">返回修改</a>'
         '<button class="pvbtn" id="pvPublish" type="button">发布</button></div>'
         '<p class="pvmsg" id="pvMsg" role="alert" hidden></p>')%(htmlmod.escape(token),htmlmod.escape(json.dumps(rank)))
    head='<link rel="stylesheet" href="/preview/bar.css"><script src="/preview/bar.js" defer></script></head>'
    page=page.replace('</head>',head,1)
    return page.replace('<body>','<body class="pv">'+bar,1)

def message_page(title,text,status=200):
    return ('<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
            '<title>%s · 后台</title><link rel="stylesheet" href="/preview/bar.css"></head><body class="pvempty"><main><h1>%s</h1><p>%s</p>'
            '<p><a href="/#rank">回到 03 R12</a></p></main></body></html>')%(htmlmod.escape(title),htmlmod.escape(title),htmlmod.escape(text))

def server(store,host='127.0.0.1',port=8788,root=ROOT,fetch=None,flush=None,gc=None,pull=None,gcdb_run=None):
    """root：.env 与本机已下载图片所在（测试传临时目录）；fetch：卖家图片下载函数（测试注入）；
    flush：发布入队后在后台线程触发一次同步（默认 sync.flush(store, root)）。
    gc：GoodCustomer 资料库与 gcdb.py 路径（默认读 config/settings.yaml 的 goodcustomer；测试传临时库）；
    pull：打开 06 客人时拉取账号名单（默认 sync.pull_accounts(store, root)）；gcdb_run：调用 gcdb 的函数（测试注入）。"""
    if host not in LOCAL_HOSTS:raise ValueError('后台只监听本机地址（AD-4）；手机请经 tailscale serve 访问')
    from . import ranking,publish,sync,guests,matching,learning,agreement
    if flush is None:flush=lambda:sync.flush(store,root)
    if pull is None:pull=lambda:sync.pull_accounts(store,root,seconds=5)
    def gc_settings():
        if gc is not None:return gc
        try:return load()['goodcustomer']
        except Exception:
            log.exception('读取 goodcustomer 设置失败');return None
    # 当前预览的候选快照：payload、rank、names、fp（内容指纹）、at、pages（渲染结果缓存）。只在内存，不落库、不上传。
    # gen：每次作废加一；生成中途被作废的预览不保存。build_lock：同一时刻只生成一个预览。
    preview={};state={'gen':0}
    preview_lock=threading.Lock();build_lock=threading.Lock()
    def drop_preview():
        with preview_lock:state['gen']+=1;preview.clear()
    # 发布请求（request_id）进行中时，同一 request_id 的重试等第一次的结果。
    inflight={};flight_out={};flight_lock=threading.Lock()
    kick_lock=threading.Lock();kicking={'running':False,'again':False}
    def kick():
        """即时隐藏（AD-5、CAP-2）：在后台线程触发一次限时 sync.flush。已有一次在跑时记下"再来一次"，
        当前这次结束后再 flush 一次（期间新入队的隐藏不会被漏掉）。"""
        if flush is None:return
        with kick_lock:
            if kicking['running']:kicking['again']=True;return
            kicking['running']=True
        def work():
            while True:
                try:flush()
                except Exception:log.exception('即时同步失败（下一轮采集会重试）')
                with kick_lock:
                    if not kicking['again']:kicking['running']=False;return
                    kicking['again']=False
        threading.Thread(target=work,daemon=True).start()
    from . import market
    market.init(store)
    from .archive_review import init as init_archive_review
    init_archive_review(store)
    token=secrets.token_urlsafe(32)
    visual_lock=threading.Lock()
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,data,kind='application/json; charset=utf-8'):
            body=(json.dumps(data,ensure_ascii=False) if kind.startswith('application/json') else data).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
        def send_raw(self,status,body,kind,headers=None):
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            for k,v in (headers or {}).items():self.send_header(k,v)
            self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
        def preview_get(self,url):
            """/preview/…：客人页同一份模板渲染的候选快照（只读内存里的预览），以及它引用的静态资源与本机转码图片。"""
            path=url.path[len('/preview'):] or '/'
            if path=='/bar.css':return self.send_raw(200,(ROOT/'curio/preview-bar.css').read_bytes(),'text/css; charset=utf-8',{'Cache-Control':'no-store'})
            if path=='/bar.js':return self.send_raw(200,(ROOT/'curio/preview-bar.js').read_bytes(),'application/javascript; charset=utf-8',{'Cache-Control':'no-store'})
            if path=='/static/guest.css':
                # 同一份客人页样式；站内绝对地址 url(/…) 换到预览路由。
                css=re.sub(r"url\((['\"]?)/(?!/)",r"url(\1/preview/",(ranking.SERVER_DIR/'static/guest.css').read_text())
                return self.send_raw(200,css.encode(),'text/css; charset=utf-8',{'Cache-Control':'no-store'})
            if path=='/static/guest.js':
                # 同一份客人页脚本；续载接口换到预览路由。找不到要换的地址时明确失败，不悄悄连到错误接口。
                js=(ranking.SERVER_DIR/'static/guest.js').read_text()
                if "'/api/pool?'" not in js:
                    return self.send_raw(503,b'/* preview: guest.js api path not found */','application/javascript; charset=utf-8',{'Cache-Control':'no-store'})
                return self.send_raw(200,js.replace("'/api/pool?'","'/preview/api/pool?'").encode(),'application/javascript; charset=utf-8',{'Cache-Control':'no-store'})
            with preview_lock:snap=dict(preview)
            if path.startswith('/img/'):
                name=path[len('/img/'):]
                if not snap or not PREVIEW_IMAGE.fullmatch(name) or name not in snap['names']:return self.reply(404,{'error':'图片不存在'})
                f=sync.image_dir(store)/name
                if not f.is_file():return self.reply(404,{'error':'图片不存在'})
                return self.send_raw(200,f.read_bytes(),'image/jpeg',{'Cache-Control':'no-store'})
            if not snap:
                return self.send_raw(200,message_page('预览已失效','排序有变化或后台重启过，请回到 03 R12 重新点"预览"。').encode(),'text/html; charset=utf-8',{'Cache-Control':'no-store','Content-Security-Policy':GUEST_CSP})
            if path not in ('/','/legal','/api/pool') and not PREVIEW_ITEM.fullmatch(path):
                return self.send_raw(404,message_page('页面不存在','预览里没有这个页面。',404).encode(),'text/html; charset=utf-8',{'Cache-Control':'no-store','Content-Security-Policy':GUEST_CSP})
            key=(path,url.query)
            out=snap['pages'].get(key)
            if out is None:
                try:out=ranking.render_preview(snap['payload'],path,url.query,snap['at'])
                except ranking.PreviewError as e:
                    return self.send_raw(503,message_page('预览不可用',str(e),503).encode(),'text/html; charset=utf-8',{'Cache-Control':'no-store','Content-Security-Policy':GUEST_CSP})
                with preview_lock:
                    if state['gen']==snap['gen'] and len(snap['pages'])<200:snap['pages'][key]=out
            if out['type']=='json':return self.reply(out['status'],out['body'])
            page=preview_shell(out['body'],token,snap['rank'])
            return self.send_raw(out['status'],page.encode(),'text/html; charset=utf-8',{'Cache-Control':'no-store','Content-Security-Policy':GUEST_CSP,'X-Frame-Options':'DENY','Referrer-Policy':'same-origin'})
        def _forwarded_host(self):
            """只在对端为本机（tailscale serve 反代）且值为 *.ts.net 时采用 X-Forwarded-Host（逗号列表取第一个）。"""
            raw=self.headers.get('X-Forwarded-Host')
            try:loopback=ipaddress.ip_address(self.client_address[0]).is_loopback
            except ValueError:loopback=False
            if not raw or not loopback:return None
            first=raw.split(',')[0].strip()
            return first if _hostname(first).endswith('.ts.net') and host_allowed(first) else None
        def _host_ok(self):
            if host_allowed(self.headers.get('Host')):return True
            self.reply(403,{'error':'主机名不被允许'});return False
        def make_preview(self,data):
            """生成候选快照（必要时先转码图片），留在内存供 /preview/ 渲染。榜须与界面所见一致。"""
            want=data.get('rank')
            if not isinstance(want,list) or not all(isinstance(x,str) for x in want):return self.reply(400,{'error':'R12 须为货号列表'})
            with build_lock:
                with preview_lock:gen=state['gen']
                with store.connect() as c:current=publish.rank(c)
                if want!=current:return self.reply(409,{'error':'排序已在别处改动，请刷新本页'})
                try:payload,failures=publish.candidate(store,root,fetch)
                except publish.PublishError as e:return self.reply(400,{'error':str(e)})
                with preview_lock:
                    if state['gen']!=gen:return self.reply(409,{'error':'预览期间内容有变化，请重新预览'})
                    preview.clear()
                    preview.update(payload=payload,rank=payload['rank'],names=_preview_names(payload),fp=publish.payload_fingerprint(payload),
                                   at=sync.utc_now(),gen=gen,pages={})
            no_images=[it['code'] for it in payload['items'] if not it['images']]
            return self.reply(200,{'url':'/preview/','rank':payload['rank'],'pool':len(payload['pool']),'image_failures':failures,'no_images':no_images})
        def publish_from_preview(self,data):
            """只从预览横条发布：须有当前预览、且榜与内容（指纹）与预览所见一致；调用故事 10 发布模块（发布锁、入队；
            request_id 的结果与快照同一事务记下），再后台 flush 一次。同一 request_id 进行中的重试等第一次的结果。"""
            rid=data['request_id'];want=data['rank']
            if not isinstance(rid,str) or not 1<=len(rid)<=100:raise ValueError('请求编号无效')
            if not isinstance(want,list) or not all(isinstance(x,str) for x in want):raise ValueError('R12 须为货号列表')
            with store.connect() as c:
                prev=c.execute('SELECT * FROM triage_requests WHERE request_id=?',(rid,)).fetchone()
            if prev:
                if prev['payload']!=publish.request_payload(want):raise ValueError('重复请求内容不同')
                out=json.loads(prev['result']);out['orders']=out.get('orders') or 0      # 故事 18 之前的结果没有 orders
                return self.reply(200,out)
            with flight_lock:
                ev=inflight.get(rid);owner=ev is None
                if owner:ev=inflight[rid]=threading.Event()
            if not owner:
                ev.wait(900)
                with flight_lock:out=flight_out.get(rid)
                return self.reply(*(out or (409,{'error':'另一个发布正在进行，请稍后再试'})))
            out=(500,{'error':'未发布，请重试'})
            try:out=self._publish_once(rid,want)
            finally:
                with flight_lock:
                    if len(flight_out)>200:flight_out.clear()
                    flight_out[rid]=out;inflight.pop(rid,None)
                ev.set()
            return self.reply(*out)
        def _publish_once(self,rid,want):
            with preview_lock:
                snap=dict(preview) if preview else None
                if snap and snap['rank']==want:state['gen']+=1;preview.clear()     # 发布前即作废预览
            if snap is None:return 409,{'error':'请先在 03 R12 点"预览"，再从预览里发布'}
            if snap['rank']!=want:return 409,{'error':'排序在预览之后有变化，请重新预览后再发布'}
            try:r=publish.publish(store,root,fetch=fetch,expect_rank=want,expect_fingerprint=snap['fp'],request_id=rid,gc=gc_settings())
            except publish.PublishBusy as e:return 409,{'error':str(e)+'；请回到 03 R12 重新预览'}
            except publish.PublishStale as e:return 409,{'error':str(e)}
            except publish.PublishError as e:return 400,{'error':str(e)}
            except Exception:
                log.exception('发布失败')
                return 500,{'error':'未发布，请重试'}
            result={k:r.get(k) for k in publish.REPLY_KEYS}
            result['orders']=result.get('orders') or 0      # 故事 18 之前的发布结果没有 orders
            if not r.get('replayed'):
                def run_flush():
                    try:flush()
                    except Exception:log.exception('发布后同步失败（下一轮采集会重试）')
                threading.Thread(target=run_flush,daemon=True).start()
            return 200,result
        def owner_events(self):
            """POST /api/owner-events：墨墨在 01 的行为事件（≤100 条/批）。坏的单条丢弃并计数，不整批失败。"""
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=150000:raise ValueError('请求过大或为空')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('请求格式无效')
            except (ValueError,TypeError,RecursionError):      # RecursionError：嵌套过深的 JSON
                data=None
            sent=self.headers.get('X-Curio-Token') or (data or {}).get('token')
            if not isinstance(sent,str) or not secrets.compare_digest(sent.encode(),token.encode()):return self.reply(403,{'error':'请刷新本机审核页后重试'})
            if data is None:return self.reply(400,{'error':'请求格式无效'})
            try:return self.reply(200,store.record_owner_events(data.get('events')))
            except ValueError as e:return self.reply(400,{'error':str(e)})
            except Exception:
                log.exception('行为事件写入失败')
                return self.reply(500,{'error':'未保存，请重试'})
        def _words(self):
            with store.connect() as c:return c.execute("SELECT word FROM blocked_words ORDER BY created_at").fetchall()
        def do_GET(self):
            if not self._host_ok():return
            try:return self._get()
            except Exception:
                log.exception('后台读取失败：%s',self.path)
                return self.reply(500,{'error':'读取失败，请重试'})
        def _get(self):
            url=urlparse(self.path)
            if url.path=='/archive-review':return self.reply(200,(ROOT/'curio/archive-review.html').read_text().replace('__TOKEN__',token),'text/html; charset=utf-8')
            if url.path=='/api/archive-classifications':
                from .archive_review import page
                return self.reply(200,page(store))
            if url.path=='/market':return self.reply(200,(ROOT/'curio/market.html').read_text().replace('__TOKEN__',token),'text/html; charset=utf-8')
            if url.path.startswith('/api/market/'):
                try:
                    action=url.path.rsplit('/',1)[-1];q=parse_qs(url.query)
                    if action=='items':return self.reply(200,market.query(q))
                    if action=='options':return self.reply(200,market.options())
                    if action=='detail':return self.reply(200,market.detail(q))
                except ValueError as e:return self.reply(400,{'error':str(e)})
            if url.path=='/':
                page=(ROOT/'curio/triage.html').read_text().replace('__TOKEN__',token)
                return self.reply(200,page,'text/html; charset=utf-8')
            if url.path=='/triage.js':
                return self.reply(200,(ROOT/'curio/triage.js').read_text(),'application/javascript; charset=utf-8')
            if url.path=='/ranking.js':
                return self.reply(200,(ROOT/'curio/ranking.js').read_text(),'application/javascript; charset=utf-8')
            if url.path=='/api/pool':return self.reply(200,ranking.pool(store,root))
            if url.path=='/api/guests':
                # 06 客人：refresh=1（打开分区时）先拉一次账号名单；失败只记下，照常返回上次的名单。
                q=parse_qs(url.query);pulled=None
                if q.get('refresh',[''])[0]=='1':
                    try:pulled=pull()
                    except Exception:
                        log.exception('账号名单拉取失败');pulled={'accounts_status':'error'}
                data=guests.view(store,gc_settings())
                if pulled is not None:data['pull']=pulled
                return self.reply(200,data)
            if url.path=='/api/match':
                # 04 匹配（故事 16）：只读 GoodCustomer；资料库不在时 available=false，界面提示，不报错。
                try:return self.reply(200,matching.view(store,gc_settings(),root))
                except FileNotFoundError:return self.reply(400,{'error':'找不到 config/matching.yaml（匹配词表）'})
                except ValueError as e:return self.reply(400,{'error':'匹配词表有误：'+str(e)})
            if url.path=='/api/rules':
                # 05 待审核（故事 17）：refresh=1（打开 05 时）先归纳一次；只返回待审条目与计数。
                return self.reply(200,learning.view(store,root,parse_qs(url.query).get('refresh',[''])[0]=='1'))
            if url.path=='/api/guests/search':
                try:return self.reply(200,{'results':guests.search(gc_settings(),parse_qs(url.query).get('q',[''])[0])})
                except guests.GuestError as e:return self.reply(400,{'error':str(e)})
            if url.path=='/api/rank':return self.reply(200,ranking.rank_state(store,root))
            if url.path in ('/api/agreement','/api/agreement/history'):
                # 07 符合度（故事 23）：只读；往日结果首次计算后固定。
                q=parse_qs(url.query)
                try:
                    if url.path=='/api/agreement':return self.reply(200,agreement.view(store,q.get('day',[''])[0] or None,root))
                    try:days=int(q.get('days',['30'])[0])
                    except ValueError:raise ValueError('天数须为 1–%d'%agreement.HISTORY_MAX) from None
                    return self.reply(200,agreement.history(store,days,root))
                except ValueError as e:return self.reply(400,{'error':str(e) or '参数无效'})
            if url.path=='/preview':
                self.send_response(303);self.send_header('Location','/preview/');self.send_header('Content-Length','0');self.end_headers();return
            if url.path.startswith('/preview/'):return self.preview_get(url)
            if url.path.startswith('/api/triage'):
                try:
                    q=parse_qs(url.query)
                    num=lambda k,d:int(q.get(k,[str(d)])[0])
                    if url.path=='/api/triage':return self.reply(200,store.triage_state(num('limit',50),num('offset',0)))
                    # 故事 22：01 瀑布流（捞回的最前，其余按上架时间；游标续载，卡片不带分数）。
                    sess=q.get('session',[''])[0] or None
                    if url.path=='/api/triage/feed':
                        # 故事 23：墨墨先于采集打开 01 时，首个（不带游标的）feed 请求封存当天的系统预测：同样只在柏林 05:00 之后，
                        # 带本次打开的 session（本次里刚看到的货不被排除）；已封存则只读一次。
                        if not q.get('cursor',[''])[0]:
                            try:agreement.seal_for_feed(store,sess)
                            except Exception:log.exception('封存当天预测失败')
                        return self.reply(200,store.triage_feed(num('limit',40),q.get('cursor',[''])[0] or None,sess))
                    # 「看过未选」：别的打开里看过、没留下也没放回的货；页头计数（今日已留下 · 商品池共）。
                    if url.path=='/api/triage/seen':return self.reply(200,store.seen_unpicked(num('limit',40),q.get('cursor',[''])[0] or None,sess))
                    if url.path=='/api/triage/header':return self.reply(200,store.feed_header())
                    if url.path=='/api/triage/auto-removed':return self.reply(200,store.auto_removed_list(num('limit',50),num('offset',0)))
                    if url.path=='/api/triage/item':
                        iid=q.get('id',[''])[0];d=store.item_detail(iid,root)
                        d['rank_reason']=ranking.detail_rank_reason(store,iid)      # 能否加入榜（榜满由界面判断）
                        return self.reply(200,d)
                except ValueError as e:return self.reply(400,{'error':str(e) or '参数无效'})
            if url.path=='/old':
                page=(ROOT/'curio/review.html').read_text().replace('__TOKEN__',token).replace('__EDITOR_JS__',(ROOT/'curio/review-editor.js').read_text())
                return self.reply(200,page,'text/html; charset=utf-8')
            if url.path=='/api/visual':
                from .vision import page
                return self.reply(200,page(store))
            if url.path.startswith('/archive/images/'):
                import re
                name=url.path.rsplit('/',1)[-1]
                if not re.fullmatch(r'[a-f0-9]{64}\.(jpg|png|webp)',name):return self.reply(404,{'error':'图片不存在'})
                path=ROOT/'data/porcelain/images'/name
                if not path.is_file():return self.reply(404,{'error':'图片不存在'})
                body=path.read_bytes();self.send_response(200);self.send_header('Content-Type',{'jpg':'image/jpeg','png':'image/png','webp':'image/webp'}[path.suffix[1:]]);self.send_header('Content-Length',str(len(body)));self.end_headers();self.wfile.write(body);return
            if url.path=='/api/items':
                try:
                    q=parse_qs(url.query);result=store.page(q.get('tab',['pending'])[0],int(q.get('page',['1'])[0]))
                    result['blocked_words']=[r[0] for r in self._words()]
                    result['import_note']=store.get('desktop_import_note','')
                    result['last_collection']=store.get('last_collection','尚未采集')
                    result['collection_result']=store.get('last_collection_result','')
                    return self.reply(200,result)
                except ValueError:return self.reply(400,{'error':'分页参数无效'})
            self.reply(404,{'error':'页面不存在'})
        def do_POST(self):
            if not self._host_ok():return
            origin=self.headers.get('Origin')
            # tailscale serve 反代时浏览器的 Host 可能出现在 X-Forwarded-Host（只信本机对端给的 *.ts.net）。
            hosts={h.lower() for h in (self.headers.get('Host'),self._forwarded_host()) if h}
            if origin and urlparse(origin).netloc.lower() not in hosts:return self.reply(403,{'error':'请刷新本机审核页后重试'})
            # 故事 22：行为上报在页面离开时用 sendBeacon 补发（不能带自定义请求头），令牌也可放在请求体的 token 字段。
            if self.path=='/api/owner-events':return self.owner_events()
            if self.headers.get('X-Curio-Token')!=token:return self.reply(403,{'error':'请刷新本机审核页后重试'})
            if self.path not in ('/api/rank','/api/pool/action','/api/rank/preview','/api/rank/publish','/api/triage/action','/api/triage/comment','/api/triage/alt','/api/triage/fee','/api/triage/recheck','/api/guests/link','/api/guests/command','/api/guests/invite','/api/match/decide','/api/rules/decide','/api/review','/api/block','/api/visual/confirm','/api/visual/run','/api/market/decision','/api/archive-classifications/review'):return self.reply(404,{'error':'接口不存在'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=150000:raise ValueError('请求过大或为空')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('请求格式无效')
                if self.path=='/api/rank':
                    result=ranking.save_rank(store,data['codes'],data['request_id']);drop_preview()
                    return self.reply(200,dict(ranking.rank_state(store,root),saved=result['rank']))
                if self.path=='/api/pool/action':
                    result=ranking.pool_action(store,data['action'],data['item_id'],data['request_id'])
                    drop_preview()
                    if result.get('hidden_queued') and not result.get('replayed'):kick()
                    return self.reply(200,result)
                if self.path=='/api/rank/preview':return self.make_preview(data)
                if self.path=='/api/rank/publish':return self.publish_from_preview(data)
                if self.path=='/api/triage/action':
                    result=store.triage_act(data['action'],data.get('item_id'),data['request_id']);drop_preview()
                    return self.reply(200,result)
                # 点评、替代文字、拣选动作都可能改变预览内容：作废当前预览。
                if self.path=='/api/triage/comment':
                    result=store.save_comment(data['item_id'],data['text']);drop_preview();return self.reply(200,result)
                if self.path=='/api/triage/alt':
                    result=store.save_alt(data['item_id'],data['index'],data['text']);drop_preview();return self.reply(200,result)
                if self.path=='/api/triage/fee':
                    # CAP-10：手动改费用档位（tier 为 null 即恢复自动）；改档后预览作废、榜显示有未发布的修改。
                    result=store.save_fee(data['item_id'],data['tier'],data['request_id'],root);drop_preview();return self.reply(200,result)
                if self.path=='/api/triage/recheck':
                    from . import curio_monitor
                    if not isinstance(data['item_id'],str):raise ValueError('商品编号无效')
                    result=curio_monitor.recheck_item(store,data['item_id'],load())
                    # 复查判下架：mark_gone 已在同一事务里入队隐藏（已发布的货），立即触发一次发送。
                    if result.get('outcome')=='gone':drop_preview();kick()
                    return self.reply(200,result)
                if self.path=='/api/match/decide':
                    return self.reply(200,matching.decide(store,gc_settings(),data['customer_id'],data['item_id'],data['decision'],data['request_id'],root))
                if self.path=='/api/rules/decide':
                    # 05 待审核：确认 / 否决（最终操作）；request_id 幂等。确认的规律在之后的判断里生效。
                    return self.reply(200,learning.decide_request(store,data['rule_id'],data['decision'],data['request_id'],root))
                # 06 客人（故事 14）：关联只在 curio.db；停用、重置、换邀请码入发件箱后立即触发一次发送。
                if self.path=='/api/guests/link':
                    action=data['action']
                    if action=='create':
                        return self.reply(200,guests.create_and_link(store,gc_settings(),data['account_id'],data['request_id'],gcdb_run))
                    return self.reply(200,guests.link(store,gc_settings(),action,data['account_id'],data['request_id'],data.get('customer_id')))
                if self.path=='/api/guests/command':
                    result=guests.command(store,data['action'],data['account_id'],data['request_id'])
                    if not result.get('replayed'):kick()
                    return self.reply(200,result)
                if self.path=='/api/guests/invite':
                    result=guests.change_invite(store,data['code'],data['request_id'])
                    if not result.get('replayed'):kick()
                    return self.reply(200,result)
                if self.path=='/api/archive-classifications/review':
                    from .archive_review import review
                    return self.reply(200,{'review_id':review(store,data['id'],data['fields'],data.get('note',''))})
                if self.path=='/api/market/decision':
                    market.set_decision(data['platform'],data['id'],data['irrelevant'],data.get('reason',''))
                    return self.reply(200,{'ok':True})
                if self.path=='/api/visual/run':
                    from .vision import api_disabled
                    if api_disabled(ROOT):return self.reply(409,{'error':'NVIDIA API已由用户停用'})
                    if not visual_lock.acquire(blocking=False):return self.reply(409,{'error':'已有一批视觉任务正在处理'})
                    def run_visual():
                        from .vision import enqueue,work
                        try:enqueue(store,3);work(store,3)
                        finally:visual_lock.release()
                    threading.Thread(target=run_visual,daemon=True).start()
                    return self.reply(200,{'started':True})
                if self.path=='/api/visual/confirm':
                    from .vision import confirm
                    result=confirm(store,data['result_id'],data['data'],data.get('note',''))
                    return self.reply(200,{'result_id':result})
                if self.path=='/api/block':
                    result=store.block(data['item_id'],data['word'],data['request_id'],data['rule_version'],load())
                    return self.reply(200,result)
                case=store.review(data['item_id'],data['decision'],data.get('reason',''),data.get('fair_price'),data['request_id'],data['rule_version'])
                return self.reply(200,{'case_id':case})
            except publish.PublishBusy as e:return self.reply(409,{'error':str(e)})
            except (ValueError,KeyError,TypeError) as e:return self.reply(400,{'error':str(e) or '保存失败，请刷新后重试'})
            except Exception:
                log.exception('后台写入失败：%s',self.path)
                return self.reply(500,{'error':'未保存，请重试'})
    return ThreadingHTTPServer((host,port),Handler)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='127.0.0.1',choices=LOCAL_HOSTS,help='只允许本机地址；手机经 tailscale serve 访问')
    parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args();httpd=server(Store(ROOT/'data/curio.db'),args.host,args.port)
    print(f'Curio 后台：http://{args.host}:{args.port}（旧审核页 /old）',flush=True)
    httpd.serve_forever()
