"""One SQLite for items, rules, screening, collection state and owner reviews."""
import json
import logging
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from .price import prices, money
from .rules import evaluate, standard_name, kb_fingerprint, kb_status
from . import triage
from screening.pipeline import process


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')

log=logging.getLogger(__name__)
_STD_WARNED=[]

def _std_name_or_none(c,item,config,changed):
    """需要（重）生成时返回标准名称结果，否则 None。命名出错只记一次日志、不写行，绝不中断入库。
    需要生成：新货/改价改文、尚无结果、或库里结果的 kb_fingerprint 与当前知识库不同（知识库改过或恢复了）。"""
    try:
        row=c.execute('SELECT kb_fingerprint FROM std_names WHERE item_id=?',(item['item_id'],)).fetchone()
        if not changed and row and (row[0] or '')==kb_fingerprint(config):return None
        return standard_name(item,config)
    except Exception:
        if not _STD_WARNED:
            _STD_WARNED.append(1);log.warning('标准名称生成失败，已跳过（不影响入库）',exc_info=True)
        return None

def write_std_name(c,item_id,r,ts=None):
    """按 item_id 覆盖写入一条标准名称结果（入库与回填共用）。"""
    segments=json.dumps(dict(era=r['era'],brand=r['brand_display'],pattern=r['pattern'],form=r['form'],function=r['function']),ensure_ascii=False)
    c.execute('''INSERT OR REPLACE INTO std_names(item_id,std_name,std_key,status,brand,pattern_id,form_id,function_id,era,
        seller_grade,segments,kb_status,kb_fingerprint,generated_at,overpaint) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)''',
        (item_id,r['std_name'],r['std_key'],r['status'],r['brand'],r['pattern_id'],r['form_id'],r['function_id'],r['era'],
         r['seller_grade'],segments,r['kb_status'],r['kb_fingerprint'],ts or now(),int(bool(r.get('overpaint')))))

# 重判时比对的标准名称字段：有任一不同才改写（生成时间随之更新）。
STD_FIELDS=('std_name','std_key','status','brand','pattern_id','form_id','function_id','era','seller_grade','kb_status','kb_fingerprint','overpaint')

def _safe_name(item,config,failures):
    """生成标准名称；出错时计数、只记一次日志并返回 None（不中断重判）。"""
    try:
        return standard_name(item,config)
    except Exception:
        failures.append(item.get('item_id'))
        if not _STD_WARNED:
            _STD_WARNED.append(1);log.warning('标准名称生成失败，已跳过',exc_info=True)
        return None

def _std_tuple(r):
    return tuple(int(bool(r['overpaint'])) if f=='overpaint' else r[f] for f in STD_FIELDS)

def _category(reason):
    """理由的类别（用于重判报告）：取「：」前的部分。"""
    return (reason or '（无）').split('：',1)[0]

# 判断用到的字段（加 rule_version）：锁外预算的结果只在这些都没变时沿用。
EVAL_FIELDS=('title','description','condition','listed_price','price_text','currency','rule_version')

def _eval_sig(r):
    return tuple(r[f] for f in EVAL_FIELDS)

def rejudge(c,config,precomputed=None):
    """全量重判（屏蔽词与重判脚本共用）：理由或结论变化的货更新判断、rule_version+1，
    再经 triage.system_place 落位——只移动未经人工决定的货（AD-26）。调用方须已开写事务。
    precomputed：{item_id: (_eval_sig, evaluate 结果)}，签名与当前行一致时沿用，否则当场重算。
    返回 dict(checked, changed, blocked, moved, transitions={旧类别→新类别: 件数})。"""
    rows=c.execute('SELECT * FROM items').fetchall()
    changed=matched=moved=0;transitions={}
    for r in rows:
        pre=(precomputed or {}).get(r['item_id'])
        result=pre[1] if pre and pre[0]==_eval_sig(r) else evaluate(dict(r),config)
        matched+=result['reason'].startswith('屏蔽词：')
        if (r['agent_decision'],r['agent_reason'],r['fair_price'] or '')!=(result['decision'],result['reason'],result.get('fair','')):
            c.execute('UPDATE items SET agent_decision=?,agent_reason=?,fair_price=?,rule_version=rule_version+1 WHERE item_id=?',
                (result['decision'],result['reason'],result.get('fair',''),r['item_id']))
            before=triage.state(c,r['item_id'])
            after=triage.system_place(c,r['item_id'])
            moved+=before is not None and before!=after
            changed+=1
            key='%s（%s）→%s（%s）'%(_category(r['agent_reason']),r['agent_decision'],_category(result['reason']),result['decision'])
            transitions[key]=transitions.get(key,0)+1
    return dict(checked=len(rows),changed=changed,blocked=matched,moved=moved,
                transitions=dict(sorted(transitions.items(),key=lambda kv:-kv[1])))

def similar_prices(c,std_key):
    # 品牌段为空（-|…）的键会把各厂同功能的货混成一组，不算同类
    if not std_key or std_key.split('|',1)[0]=='-':return []
    rows=c.execute('''SELECT h.item_id,h.price,h.total_price,h.seen_at,i.currency,i.title,i.platform,s.std_name
        FROM std_names s JOIN price_history h ON h.item_id=s.item_id LEFT JOIN items i ON i.item_id=s.item_id
        WHERE s.std_key=? AND s.status='named' ORDER BY h.seen_at,h.rowid''',(std_key,)).fetchall()
    return [dict(r) for r in rows]

class Store:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS items(
                item_id TEXT PRIMARY KEY, platform TEXT NOT NULL, title TEXT NOT NULL,
                description TEXT, price REAL, listed_price REAL, total_price REAL,currency TEXT,
                price_text TEXT,buying TEXT,url TEXT,image TEXT,created TEXT,ends TEXT,
                condition TEXT,country TEXT,first_seen TEXT,last_seen TEXT,checked_at TEXT,
                status TEXT DEFAULT 'active',gone_at TEXT,agent_decision TEXT,agent_reason TEXT,
                fair_price TEXT,rule_version INTEGER DEFAULT 1);
            CREATE TABLE IF NOT EXISTS price_history(item_id TEXT,price REAL,total_price REAL,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS price_drops(item_id TEXT,old_price REAL,new_price REAL,pct REAL,seen_at TEXT);
            CREATE TABLE IF NOT EXISTS cases(
                id INTEGER PRIMARY KEY AUTOINCREMENT,item_id TEXT NOT NULL,decision TEXT NOT NULL,
                reason TEXT NOT NULL,fair_price REAL,currency TEXT,agent_decision TEXT,agent_reason TEXT,
                listed_price REAL,total_price REAL,rule_version INTEGER,decided_at TEXT,
                request_id TEXT NOT NULL UNIQUE);
            CREATE TABLE IF NOT EXISTS market_decisions(platform TEXT,item_id TEXT,irrelevant INTEGER NOT NULL,reason TEXT,updated_at TEXT,PRIMARY KEY(platform,item_id));
            CREATE TABLE IF NOT EXISTS blocked_words(word TEXT PRIMARY KEY,created_at TEXT);
            CREATE TABLE IF NOT EXISTS block_requests(request_id TEXT PRIMARY KEY,payload TEXT,result TEXT);
            CREATE TABLE IF NOT EXISTS kv(k TEXT PRIMARY KEY,v TEXT);
            CREATE INDEX IF NOT EXISTS review_order ON items(status,agent_decision,created,first_seen);
            CREATE INDEX IF NOT EXISTS cases_item ON cases(item_id);
            -- CAP-16 标准名称（候选，据卖家描述）：每件货一行，入库与回填都按 item_id 覆盖。
            CREATE TABLE IF NOT EXISTS std_names(
                item_id TEXT PRIMARY KEY, std_name TEXT, std_key TEXT NOT NULL, status TEXT NOT NULL,
                brand TEXT, pattern_id TEXT, form_id TEXT, function_id TEXT, era TEXT, seller_grade TEXT,
                segments TEXT, kb_status TEXT NOT NULL, kb_fingerprint TEXT, generated_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS std_names_key ON std_names(std_key,status);
            CREATE INDEX IF NOT EXISTS price_history_item ON price_history(item_id,seen_at);
            ''')
            # 故事 5：外画标记列（已有库补列，旧行默认 0，重判脚本刷新）。
            if 'overpaint' not in {r[1] for r in c.execute('PRAGMA table_info(std_names)')}:
                try:
                    c.execute('ALTER TABLE std_names ADD COLUMN overpaint INTEGER NOT NULL DEFAULT 0')
                except sqlite3.OperationalError as e:
                    # 两个进程同时首次打开：另一方已补列。
                    if 'duplicate column' not in str(e):raise
            triage.ensure(c)
    @contextmanager
    def connect(self):
        c=sqlite3.connect(self.path,timeout=30);c.row_factory=sqlite3.Row
        try:
            # AD-19：WAL 让后台读与采集写并行；busy_timeout 与 timeout 一致。
            c.execute('PRAGMA journal_mode=WAL');c.execute('PRAGMA busy_timeout=30000')
            yield c;c.commit()
        except Exception:
            c.rollback();raise
        finally:c.close()
    def known(self):
        with self.connect() as c:return {r[0] for r in c.execute('SELECT item_id FROM items')}
    def get(self,key,default=None):
        with self.connect() as c:r=c.execute('SELECT v FROM kv WHERE k=?',(key,)).fetchone()
        return r[0] if r else default
    def set(self,key,value):
        with self.connect() as c:c.execute('INSERT OR REPLACE INTO kv VALUES(?,?)',(key,str(value)))
    def ingest(self,item,config):
        # Only explicit public product fields are persisted; no response blobs or credentials.
        fields=('item_id','platform','title','description','currency','price_text','buying','url','image','created','ends','condition','country')
        p={k:item.get(k,'') or '' for k in fields}
        if not p['item_id'] or p['platform'] not in ('ebay','kleinanzeigen','etsy'):raise ValueError('来源商品编号无效')
        listed,total=prices(item,config['auction_fee_rate']);p.update(price=listed,listed_price=listed,total_price=total)
        ts=now()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            old=c.execute('SELECT * FROM items WHERE item_id=?',(p['item_id'],)).fetchone()
            if old and 'description' not in item:p['description']=old['description'] or ''
            config=dict(config,blocked_words=[r[0] for r in c.execute('SELECT word FROM blocked_words')])
            changed=old is None or old['listed_price']!=listed or old['currency']!=p['currency'] or old['title']!=p['title'] or (old['description'] or '')!=p['description']
            if changed:
                result=evaluate(p,config)
                p.update(agent_decision=result['decision'],agent_reason=result['reason'],fair_price=result.get('fair',''))
                p['rule_version']=old['rule_version']+1 if old else 1
                process(c,p,result,config['daily_budget'])
                c.execute('INSERT INTO price_history VALUES(?,?,?,?)',(p['item_id'],listed,total,ts))
                if old and old['currency']==p['currency'] and old['listed_price'] and listed is not None and 0<=listed<old['listed_price'] and 'AUCTION' not in p['buying']:
                    pct=(old['listed_price']-listed)/old['listed_price']*100
                    if pct>=10:c.execute('INSERT INTO price_drops VALUES(?,?,?,?,?)',(p['item_id'],old['listed_price'],listed,pct,ts))
            p.update(last_seen=ts,checked_at=ts,status='active',gone_at=None)
            if old:
                # Preserve known creation date if a detail lookup omits it.
                if not p['created']:p['created']=old['created']
                c.execute('UPDATE items SET '+','.join(k+'=?' for k in p if k!='item_id')+' WHERE item_id=?',
                          [v for k,v in p.items() if k!='item_id']+[p['item_id']])
            else:
                p['first_seen']=ts
                c.execute('INSERT INTO items('+','.join(p)+') VALUES('+','.join('?' for _ in p)+')',list(p.values()))
            # AD-26：系统只在未经人工决定的货上落位（new / auto_removed）；未变化的货不重放。
            if changed:triage.system_place(c,p['item_id'])
            # CAP-16：标题/描述变化、尚无结果或知识库变化时（重）生成标准名称，与判断同一事务。
            named=_std_name_or_none(c,p,config,changed)
            if named:write_std_name(c,p['item_id'],named,ts)
        return 'new' if old is None else 'changed' if changed else 'unchanged'
    def similar_prices(self,std_key):
        """同类历史价：标准名称分组键相同（且已生成名称）的全部货的 price_history，按时间排序。"""
        with self.connect() as c:return similar_prices(c,std_key)
    def by_code(self,code):
        """按货号查标准名称与同类历史价（经 item_codes 关联）；未知货号返回空 dict。"""
        with self.connect() as c:
            row=c.execute('''SELECT k.code,k.item_id,s.std_name,s.std_key,s.status,s.brand,s.pattern_id,s.form_id,s.function_id,
                s.era,s.seller_grade,s.segments,s.kb_status,s.kb_fingerprint,s.generated_at
                FROM item_codes k LEFT JOIN std_names s ON s.item_id=k.item_id WHERE k.code=?''',(code,)).fetchone()
            if not row:return {}
            out=dict(row)
            out['segments']=json.loads(out['segments']) if out.get('segments') else None
            out['prices']=similar_prices(c,row['std_key']) if row['status']=='named' else []
            return out
    def mark_gone(self,iid):
        with self.connect() as c:c.execute("UPDATE items SET status='gone',gone_at=?,checked_at=? WHERE item_id=? AND status='active'",(now(),now(),iid))
    def archive(self):
        # Missing/withdrawn is never asserted to be sold. Archive in SQLite only.
        with self.connect() as c:c.execute("UPDATE items SET status='archived' WHERE status='gone' AND julianday(gone_at)<julianday('now','-3 days')")
    def review(self,iid,decision,reason,fair_price,request_id,version):
        if decision not in ('want','probe','skip','irrelevant'):raise ValueError('判断无效')
        if not isinstance(reason,str) or len(reason)>2000:raise ValueError('原因格式无效')
        reason=reason.strip()
        if decision=='skip' and not reason:raise ValueError('选择不要时必须填写原因')
        fair=money(fair_price)
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('请求编号无效')
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev=c.execute('SELECT * FROM cases WHERE request_id=?',(request_id,)).fetchone()
            if prev:
                if (prev['item_id'],prev['decision'],prev['reason'],prev['fair_price'])!=(iid,decision,reason,fair):raise ValueError('重复请求内容不同')
                return prev['id']
            row=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
            if not row:raise ValueError('商品不存在')
            if type(version) is not int or row['rule_version']!=version:raise ValueError('商品价格或规则已更新，请刷新后审核')
            cur=c.execute('''INSERT INTO cases(item_id,decision,reason,fair_price,currency,agent_decision,agent_reason,
                listed_price,total_price,rule_version,decided_at,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)''',
                (iid,decision,reason,fair,row['currency'],row['agent_decision'],row['agent_reason'],row['listed_price'],row['total_price'],version,now(),request_id))
            self._triage_review(c,iid,decision,reason)
            return cur.lastrowid
    @staticmethod
    def _triage_review(c,iid,decision,reason):
        # 审核结果经拣选状态机落位（AD-26）：want→留下（自动移出/移出商品池的一并处理，一次撤销全回退），
        # skip/irrelevant→去掉，probe→自动移出的先捞回，并记一条询市事件（不改状态）。
        reason=reason or None
        if decision=='want':triage.keep(c,iid,reason,source='review')
        elif decision in ('skip','irrelevant'):triage.remove(c,iid,reason,source='review')
        else:
            if triage.current(c,iid)=='auto_removed':triage.rescue(c,iid,reason,source='review')
            triage.note_probe(c,iid,reason,source='review')
    def block(self, iid, word, request_id, version, config):
        if not isinstance(word,str) or not 1<=len(word.strip())<=100:
            raise ValueError('请从商品标题或描述中选择1至100字的屏蔽词')
        word=word.strip()
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:
            raise ValueError('请求编号无效')
        payload=json.dumps([iid,word,version],ensure_ascii=False)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            previous=c.execute('SELECT * FROM block_requests WHERE request_id=?',(request_id,)).fetchone()
            if previous:
                if previous['payload']!=payload:raise ValueError('重复请求内容不同')
                return json.loads(previous['result'])
            row=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
            if not row or type(version) is not int or row['rule_version']!=version:
                raise ValueError('商品已更新，请刷新后重试')
            if not any(word.casefold() in (row[field] or '').casefold() for field in ('title','description')):
                raise ValueError('屏蔽词必须来自这件商品的标题或描述')
            existing=c.execute('SELECT word FROM blocked_words').fetchall()
            if not any(r[0].casefold()==word.casefold() for r in existing):
                c.execute('INSERT INTO blocked_words VALUES(?,?)',(word,now()))
            c.execute('INSERT INTO cases(item_id,decision,reason,currency,agent_decision,agent_reason,listed_price,total_price,rule_version,decided_at,request_id) VALUES(?,?,?,?,?,?,?,?,?,?,?)',
                (iid,'irrelevant','屏蔽词：'+word,row['currency'],row['agent_decision'],row['agent_reason'],row['listed_price'],row['total_price'],version,now(),request_id))
            triage.remove(c,iid,'屏蔽词：'+word,source='block')
            cfg=dict(config,blocked_words=[r[0] for r in c.execute('SELECT word FROM blocked_words')])
            r=rejudge(c,cfg)
            result=dict(checked=r['checked'],changed=r['changed'],blocked=r['blocked'],word=word)
            c.execute('INSERT INTO block_requests VALUES(?,?,?)',(request_id,payload,json.dumps(result,ensure_ascii=False)))
            return result

    def rejudge_all(self,config):
        """用当前规则（含库里的屏蔽词）重判全部货并刷新标准名称；只对未经人工决定的货移动拣选状态（AD-26）。
        判断与命名先在写锁外算好，再在一个短写事务里落库；期间有变化的货（签名不同）在事务里当场重算。
        知识库状态不是 ok 时不刷新标准名称（避免用残缺词表覆盖），报告 std_skipped。
        单件命名出错只计入 std_failed，不中断。幂等：再次运行改判数为 0。"""
        with self.connect() as c:
            rows=c.execute('SELECT * FROM items').fetchall()
            words=[r[0] for r in c.execute('SELECT word FROM blocked_words')]
        cfg=dict(config,blocked_words=words)
        status=kb_status(cfg)
        refresh=status=='ok'
        failures=[]
        pre={r['item_id']:(_eval_sig(r),evaluate(dict(r),cfg)) for r in rows}
        names={r['item_id']:((r['title'],r['description']),_safe_name(dict(r),cfg,failures)) for r in rows} if refresh else {}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            now_words=[r[0] for r in c.execute('SELECT word FROM blocked_words')]
            if now_words!=words:
                # 屏蔽词在此期间变了：预算结果作废，全部当场重算。
                cfg=dict(config,blocked_words=now_words);pre={}
            result=rejudge(c,cfg,pre)
            written=unchanged=0
            if refresh:
                old={r['item_id']:_std_tuple(r) for r in c.execute('SELECT * FROM std_names')}
                ts=now()
                for r in c.execute('SELECT item_id,title,description FROM items').fetchall():
                    got=names.get(r['item_id'])
                    named=got[1] if got and got[0]==(r['title'],r['description']) else _safe_name(dict(r),cfg,failures)
                    if named is None:continue
                    if old.get(r['item_id'])==_std_tuple(named):
                        unchanged+=1;continue
                    write_std_name(c,r['item_id'],named,ts);written+=1
            result.update(std_written=written,std_unchanged=unchanged,std_failed=len(failures),
                          std_skipped=None if refresh else '知识库状态为 %s，未刷新标准名称'%status)
            return result

    def page(self,tab='pending',page=1,size=30):
        if tab not in ('pending','excluded','reviewed'):raise ValueError('标签无效')
        if page<1 or not 1<=size<=100:raise ValueError('分页参数无效')
        clauses={
          'pending':"NOT EXISTS(SELECT 1 FROM market_decisions md WHERE md.item_id=i.item_id AND md.platform=i.platform AND md.irrelevant=1) AND i.status='active' AND i.agent_decision IN ('want','probe') AND NOT EXISTS(SELECT 1 FROM cases c WHERE c.item_id=i.item_id)",
          'excluded':"(i.agent_decision='skip' OR EXISTS(SELECT 1 FROM market_decisions md WHERE md.item_id=i.item_id AND md.platform=i.platform AND md.irrelevant=1))",
          'reviewed':"EXISTS(SELECT 1 FROM cases c WHERE c.item_id=i.item_id)"}
        with self.connect() as c:
            count=c.execute('SELECT COUNT(*) FROM items i WHERE '+clauses[tab]).fetchone()[0]
            rows=c.execute('''SELECT i.*,(SELECT decision FROM cases c WHERE c.item_id=i.item_id ORDER BY id DESC LIMIT 1) owner_decision
                FROM items i WHERE '''+clauses[tab]+" ORDER BY COALESCE(NULLIF(created,''),first_seen) DESC,item_id LIMIT ? OFFSET ?",(size,(page-1)*size)).fetchall()
        return dict(items=[dict(r) for r in rows],total=count,page=page,size=size)
