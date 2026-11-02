"""Mobile-friendly local review server; no collection or model calls on page load."""
import argparse,json,secrets,threading
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from .config import ROOT,load
from .store import Store

def server(store,host='127.0.0.1',port=8788):
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
        def _words(self):
            with store.connect() as c:return c.execute("SELECT word FROM blocked_words ORDER BY created_at").fetchall()
        def do_GET(self):
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
            origin=self.headers.get('Origin')
            if self.headers.get('X-Curio-Token')!=token or (origin and urlparse(origin).netloc!=self.headers.get('Host')):
                return self.reply(403,{'error':'请刷新本机审核页后重试'})
            if self.path not in ('/api/review','/api/block','/api/visual/confirm','/api/visual/run','/api/market/decision','/api/archive-classifications/review'):return self.reply(404,{'error':'接口不存在'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=150000:raise ValueError('请求过大或为空')
                data=json.loads(self.rfile.read(length))
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
    return ThreadingHTTPServer((host,port),Handler)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args();httpd=server(Store(ROOT/'data/curio.db'),args.host,args.port)
    print(f'Curio 审核页：http://{args.host}:{args.port}',flush=True)
    httpd.serve_forever()
