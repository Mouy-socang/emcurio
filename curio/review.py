"""本机后台服务（只绑定本机；手机经 tailscale serve 访问，AD-4）。打开页面不触发采集或模型调用。

`/` 为后台一页拣选（CAP-6，curio/triage.html + triage.js）；旧审核页在 `/old`。"""
import argparse,ipaddress,json,logging,secrets,threading
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

def server(store,host='127.0.0.1',port=8788):
    if host not in LOCAL_HOSTS:raise ValueError('后台只监听本机地址（AD-4）；手机请经 tailscale serve 访问')
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
            if url.path.startswith('/api/triage'):
                try:
                    q=parse_qs(url.query)
                    num=lambda k,d:int(q.get(k,[str(d)])[0])
                    if url.path=='/api/triage':return self.reply(200,store.triage_state(num('limit',50),num('offset',0)))
                    if url.path=='/api/triage/auto-removed':return self.reply(200,store.auto_removed_list(num('limit',50),num('offset',0)))
                    if url.path=='/api/triage/item':return self.reply(200,store.item_detail(q.get('id',[''])[0]))
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
            if self.headers.get('X-Curio-Token')!=token or (origin and urlparse(origin).netloc.lower() not in hosts):
                return self.reply(403,{'error':'请刷新本机审核页后重试'})
            if self.path not in ('/api/triage/action','/api/triage/comment','/api/triage/alt','/api/triage/recheck','/api/review','/api/block','/api/visual/confirm','/api/visual/run','/api/market/decision','/api/archive-classifications/review'):return self.reply(404,{'error':'接口不存在'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=150000:raise ValueError('请求过大或为空')
                data=json.loads(self.rfile.read(length))
                if not isinstance(data,dict):raise ValueError('请求格式无效')
                if self.path=='/api/triage/action':
                    return self.reply(200,store.triage_act(data['action'],data.get('item_id'),data['request_id']))
                if self.path=='/api/triage/comment':return self.reply(200,store.save_comment(data['item_id'],data['text']))
                if self.path=='/api/triage/alt':return self.reply(200,store.save_alt(data['item_id'],data['index'],data['text']))
                if self.path=='/api/triage/recheck':
                    from . import curio_monitor
                    if not isinstance(data['item_id'],str):raise ValueError('商品编号无效')
                    return self.reply(200,curio_monitor.recheck_item(store,data['item_id'],load()))
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
