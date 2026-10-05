"""Mobile-friendly local review server; no collection or model calls on page load."""
import argparse,json,secrets
from http.server import ThreadingHTTPServer,BaseHTTPRequestHandler
from urllib.parse import urlparse,parse_qs
from .config import ROOT
from .store import Store

def server(store,host='127.0.0.1',port=8788):
    token=secrets.token_urlsafe(32)
    class Handler(BaseHTTPRequestHandler):
        def log_message(self,*args):pass
        def reply(self,status,data,kind='application/json; charset=utf-8'):
            body=(json.dumps(data,ensure_ascii=False) if kind.startswith('application/json') else data).encode()
            self.send_response(status);self.send_header('Content-Type',kind);self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store');self.send_header('X-Content-Type-Options','nosniff');self.end_headers();self.wfile.write(body)
        def do_GET(self):
            url=urlparse(self.path)
            if url.path=='/':
                page=(ROOT/'curio/review.html').read_text().replace('__TOKEN__',token)
                return self.reply(200,page,'text/html; charset=utf-8')
            if url.path=='/api/items':
                try:
                    q=parse_qs(url.query);result=store.page(q.get('tab',['pending'])[0],int(q.get('page',['1'])[0]))
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
            if self.path!='/api/review':return self.reply(404,{'error':'接口不存在'})
            try:
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=16000:raise ValueError('请求过大或为空')
                data=json.loads(self.rfile.read(length))
                case=store.review(data['item_id'],data['decision'],data.get('reason',''),data.get('fair_price'),data['request_id'],data['rule_version'])
                return self.reply(200,{'case_id':case})
            except (ValueError,KeyError,TypeError):return self.reply(400,{'error':'保存失败：不要必须填写原因，心理价需为非负数字；若商品已更新，请刷新后重试。'})
    return ThreadingHTTPServer((host,port),Handler)

if __name__=='__main__':
    parser=argparse.ArgumentParser();parser.add_argument('--host',default='127.0.0.1');parser.add_argument('--port',type=int,default=8788)
    args=parser.parse_args();httpd=server(Store(ROOT/'data/curio.db'),args.host,args.port)
    print(f'Curio 审核页：http://{args.host}:{args.port}',flush=True)
    httpd.serve_forever()
