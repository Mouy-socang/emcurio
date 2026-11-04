"""One SQLite for items, rules, screening, collection state and owner reviews."""
import base64
import json
import logging
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from .price import prices, money
from .rules import evaluate, standard_name, kb_fingerprint, kb_status, baseline_score
from . import rules
from .config import ROOT, fees_for
from . import triage
from . import sync
from . import publish
from . import behavior
from . import learning
from .config import learning_for
from screening.pipeline import process


def now():return datetime.now(timezone.utc).isoformat(timespec='seconds')
# 故事 22：01 瀑布流每批件数；行为事件每批上限、类型与停留秒数上限。
FEED_BATCH=40
OWNER_EVENT_BATCH=100
OWNER_EVENT_TYPES=('seen','dwell','open','detail_dwell','restore')   # restore：「看过未选」里放回
OWNER_DWELL_MAX=dict(dwell=600,detail_dwell=1800)

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

def humans(c):
    """已有有效人工拣选决定的货（口径同 triage.has_human）。"""
    return triage.decided_items(c)

_LEARN_WARNED=[]

def baseline_learned(c,item_id,learn=None):
    """基础分用的已确认规律：dict(rules, bonus)；没有已确认规律、该货已有人工决定（AD-26）时 None。
    learn：调用方已读好的 config.learning（按它的 root）；缺省读仓库里的。参数文件出错时只记一次日志、不加分，不中断打分。"""
    found=learning.confirmed(c)
    if not any(r['direction']=='keep' for r in found) or triage.has_human(c,item_id):return None
    if learn is None:
        try:learn=learning_for()
        except Exception:
            if not _LEARN_WARNED:
                _LEARN_WARNED.append(1);log.warning('config/learning.yaml 读取失败，基础分暂不计规律加分',exc_info=True)
            return None
    return dict(rules=found,bonus=learn['keep_bonus'])

def apply_removal_rule(c,rule,learn=None):
    """确认去掉类规律后当场生效：未经人工决定、处于新货的同组合货改判为不要（理由同规则归口），
    再经 triage.system_place 落位、重算基础分——与重判同一路径。调用方须已开写事务。返回移出件数。"""
    moved=0
    for (iid,) in c.execute("SELECT item_id FROM triage WHERE state='new'").fetchall():
        if triage.has_human(c,iid):continue
        item=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
        std=c.execute('SELECT * FROM std_names WHERE item_id=?',(iid,)).fetchone()
        if item is None or item['agent_decision']=='skip' or not rules.rule_applies(rule['dims'],std,item):continue
        c.execute("UPDATE items SET agent_decision='skip',agent_reason=?,fair_price='',rule_version=rule_version+1 WHERE item_id=?",
                  (rules.learned_reason(rule),iid))
        moved+=triage.system_place(c,iid)=='auto_removed'
        refresh_baseline(c,iid,learn)
    return moved

def judge_config(c,config):
    """判断用配置：加上库里的屏蔽词与已确认规律（CAP-8；只由 rules.evaluate 执行）。"""
    return dict(config,blocked_words=[r[0] for r in c.execute('SELECT word FROM blocked_words')],
                learned_rules=learning.confirmed(c))

def _eval_item(r,human):
    """rules.evaluate 的输入：货的字段加 human_decided（已有人工决定的货不受已确认规律影响，AD-26）。"""
    return dict(r,human_decided=human)

def rejudge(c,config,precomputed=None):
    """全量重判（屏蔽词与重判脚本共用）：理由或结论变化的货更新判断、rule_version+1，
    再经 triage.system_place 落位——只移动未经人工决定的货（AD-26）。调用方须已开写事务。
    precomputed：{item_id: ((_eval_sig, 是否有人工决定), evaluate 结果)}，签名一致时沿用，否则当场重算。
    返回 dict(checked, changed, blocked, moved, transitions={旧类别→新类别: 件数})。"""
    rows=c.execute('SELECT * FROM items').fetchall()
    human=humans(c)
    changed=matched=moved=0;transitions={}
    for r in rows:
        pre=(precomputed or {}).get(r['item_id'])
        sig=(_eval_sig(r),r['item_id'] in human)
        result=pre[1] if pre and pre[0]==sig else evaluate(_eval_item(dict(r),sig[1]),config)
        matched+=result['reason'].startswith('屏蔽词：')
        if (r['agent_decision'],r['agent_reason'],r['fair_price'] or '')!=(result['decision'],result['reason'],result.get('fair','')):
            c.execute('UPDATE items SET agent_decision=?,agent_reason=?,fair_price=?,rule_version=rule_version+1 WHERE item_id=?',
                (result['decision'],result['reason'],result.get('fair',''),r['item_id']))
            before=triage.state(c,r['item_id'])
            after=triage.system_place(c,r['item_id'])
            moved+=before is not None and before!=after
            refresh_baseline(c,r['item_id'])
            changed+=1
            key='%s（%s）→%s（%s）'%(_category(r['agent_reason']),r['agent_decision'],_category(result['reason']),result['decision'])
            transitions[key]=transitions.get(key,0)+1
    return dict(checked=len(rows),changed=changed,blocked=matched,moved=moved,
                transitions=dict(sorted(transitions.items(),key=lambda kv:-kv[1])))

def write_score(c,item_id,score,source,reason,rule_version,model=None,ts=None):
    """CAP-4：覆盖写入该货的分数（每件至多一行，以最新为准）。"""
    c.execute('INSERT OR REPLACE INTO scores(item_id,score,source,model,reason,rule_version,scored_at) VALUES(?,?,?,?,?,?,?)',
              (item_id,int(score),source,model,reason,rule_version,ts or now()))

def refresh_baseline(c,item_id,learn=None):
    """只对拣选状态为 new 的货：没有分数、分数过期（rule_version 不同）或现有规则分与当前基线不同时写入基线分。
    未过期的 AI 分保留。learn：已读好的 config.learning（见 baseline_learned）。返回是否写入。"""
    if triage.state(c,item_id)!='new':return False
    item=c.execute('SELECT * FROM items WHERE item_id=?',(item_id,)).fetchone()
    if not item:return False
    old=c.execute('SELECT * FROM scores WHERE item_id=?',(item_id,)).fetchone()
    if old and old['source']=='ai' and old['rule_version']==item['rule_version']:return False
    std=c.execute('SELECT * FROM std_names WHERE item_id=?',(item_id,)).fetchone()
    # CAP-8：已确认的留下类规律加分；已有人工决定的货（如捞回的）不受规律影响（AD-26）。
    score,reason=baseline_score(item,std,baseline_learned(c,item_id,learn))
    if old and (old['source'],old['score'],old['reason'],old['rule_version'])==('rule',score,reason,item['rule_version']):return False
    write_score(c,item_id,score,'rule',reason,item['rule_version'])
    return True

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
            -- CAP-4 可能要分数：每件货至多一行；rule_version 与 items 不同即过期。
            CREATE TABLE IF NOT EXISTS scores(
                item_id TEXT PRIMARY KEY, score INTEGER NOT NULL CHECK(score BETWEEN 0 AND 100),
                source TEXT NOT NULL CHECK(source IN ('rule','ai')), model TEXT, reason TEXT,
                rule_version INTEGER, scored_at TEXT NOT NULL);
            -- AI 已答复过的货（时间与当时 rule_version）：漏回、越界、截断的货 24 小时内不重发。
            CREATE TABLE IF NOT EXISTS ai_attempts(item_id TEXT PRIMARY KEY, attempted_at TEXT NOT NULL, rule_version INTEGER);
            -- 故事 19 看图硬筛：AI 对送检主图的三项观察（只是观察，是否移出由 judge.visual_filter 决定）；
            -- image 为送检时的 items.image，与现主图不同即作废。
            CREATE TABLE IF NOT EXISTS visual_checks(item_id TEXT PRIMARY KEY, image TEXT NOT NULL,
                single_cup INTEGER NOT NULL, plain_white INTEGER NOT NULL, not_porcelain INTEGER NOT NULL,
                model TEXT, checked_at TEXT NOT NULL);
            -- CAP-6 后台拣选：动作请求幂等（同一 request_id 重放返回同一结果）、墨墨点评、图片替代文字。
            CREATE TABLE IF NOT EXISTS triage_requests(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL, result TEXT NOT NULL, at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS comments(item_id TEXT PRIMARY KEY, text TEXT NOT NULL, updated_at TEXT NOT NULL);
            -- CAP-10：墨墨手动改的预估费用档位（手动优先，AD-2）；删除该行即恢复自动推断。
            CREATE TABLE IF NOT EXISTS fee_overrides(item_id TEXT PRIMARY KEY,
                tier TEXT NOT NULL CHECK(tier IN ('A','B','C','D','ask')), updated_at TEXT NOT NULL);
            -- CAP-14 账号关联（AD-8、AD-12）：服务器账号名单的副本（按 account_id 覆盖写，服务器没有的不删）；
            -- 账号 → GC 客户编号映射（永不上线）；只追加的关联日志（link / relink / unlink，含前后编号与时间）。
            CREATE TABLE IF NOT EXISTS guest_accounts(account_id TEXT PRIMARY KEY, username TEXT NOT NULL,
                nickname TEXT NOT NULL, status TEXT NOT NULL CHECK(status IN ('active','disabled')),
                created_at TEXT NOT NULL, account_rev INTEGER NOT NULL, pulled_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guest_links(account_id TEXT PRIMARY KEY, customer_id TEXT NOT NULL,
                linked_at TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS guest_link_log(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL,
                action TEXT NOT NULL CHECK(action IN ('link','relink','unlink')), before_id TEXT, after_id TEXT,
                at TEXT NOT NULL, request_id TEXT);
            CREATE INDEX IF NOT EXISTS guest_link_log_account ON guest_link_log(account_id,id);
            -- 06 客人的写请求幂等（同一 request_id 重放返回第一次的结果）；临时密码从不写入。
            CREATE TABLE IF NOT EXISTS guest_requests(request_id TEXT PRIMARY KEY, payload TEXT NOT NULL,
                result TEXT NOT NULL, at TEXT NOT NULL);
            -- 停用 / 重置密码指令与发件箱记录的对应（AD-24）：待同步、已生效、未同步由发件箱状态得出。
            CREATE TABLE IF NOT EXISTS guest_commands(command_id TEXT PRIMARY KEY, event_id TEXT NOT NULL UNIQUE,
                account_id TEXT NOT NULL, kind TEXT NOT NULL CHECK(kind IN ('disable_account','reset_password')),
                target_seq INTEGER NOT NULL, created_at TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS guest_commands_account ON guest_commands(account_id,created_at);
            -- CAP-5 04 匹配（故事 16）：墨墨对"这件货配这位客人"的处理——confirmed（确认，可复制单件页链接）/
            -- rejected（不合适，此后不再对该客人提出）。customer_id 为 GC 编号，只在本机。
            CREATE TABLE IF NOT EXISTS match_decisions(customer_id TEXT NOT NULL, item_id TEXT NOT NULL,
                decision TEXT NOT NULL CHECK(decision IN ('confirmed','rejected')), at TEXT NOT NULL,
                PRIMARY KEY(customer_id,item_id));
            -- 故事 22：墨墨在 01 瀑布流里的行为路径（seen / dwell / open / detail_dwell）。只在本机，不上线、
            -- 不进 GoodCustomer；留下 / 去掉不在此重复记录（以 triage_events 为准）。event_id 为客户端 UUID，
            -- INSERT OR IGNORE 去重；at 为服务器写入时间（权威），client_ts 仅作参考。
            CREATE TABLE IF NOT EXISTS owner_events(event_id TEXT PRIMARY KEY, type TEXT NOT NULL,
                item_id TEXT NOT NULL, client_ts TEXT, at TEXT NOT NULL, session_id TEXT NOT NULL, params TEXT);
            CREATE INDEX IF NOT EXISTS owner_events_item ON owner_events(item_id,at);
            CREATE INDEX IF NOT EXISTS owner_events_session ON owner_events(session_id,at);
            ''')
            # 故事 22：eff_at = 实际发生时间 min(client_ts, at)（client_ts 无效或在未来时取 at）；「看过即不要」按它比较。
            if 'eff_at' not in {r[1] for r in c.execute('PRAGMA table_info(owner_events)')}:
                try:
                    c.execute('ALTER TABLE owner_events ADD COLUMN eff_at TEXT')
                    c.execute('UPDATE owner_events SET eff_at=at WHERE eff_at IS NULL')
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e):raise
            c.execute('CREATE INDEX IF NOT EXISTS owner_events_item_type ON owner_events(item_id,type,eff_at)')
            c.execute('CREATE INDEX IF NOT EXISTS owner_events_item_type_at ON owner_events(item_id,type,at)')   # 放回时间
            # 替代文字按图片身份（本机图文件名或图片地址）保存，图片增减时不串位；image_index 只作显示参考。
            cols={r[1] for r in c.execute('PRAGMA table_info(alt_texts)')}
            if cols and 'image_key' not in cols:
                c.execute('ALTER TABLE alt_texts RENAME TO alt_texts_by_index')   # 早期按序号保存的版本，保留不删
            c.execute('''CREATE TABLE IF NOT EXISTS alt_texts(item_id TEXT NOT NULL, image_key TEXT NOT NULL, image_index INTEGER,
                text TEXT NOT NULL, updated_at TEXT NOT NULL, PRIMARY KEY(item_id,image_key))''')
            # 故事 5：外画标记列（已有库补列，旧行默认 0，重判脚本刷新）。
            if 'overpaint' not in {r[1] for r in c.execute('PRAGMA table_info(std_names)')}:
                try:
                    c.execute('ALTER TABLE std_names ADD COLUMN overpaint INTEGER NOT NULL DEFAULT 0')
                except sqlite3.OperationalError as e:
                    # 两个进程同时首次打开：另一方已补列。
                    if 'duplicate column' not in str(e):raise
            # 故事 19：ai_attempts 记下送检时的主图，同一张主图没回观察的货不再因"未看过"重发。
            if 'image' not in {r[1] for r in c.execute('PRAGMA table_info(ai_attempts)')}:
                try:
                    c.execute('ALTER TABLE ai_attempts ADD COLUMN image TEXT')
                except sqlite3.OperationalError as e:
                    if 'duplicate column' not in str(e):raise
            triage.ensure(c)
            # AD-23：Mac → 服务器的持久发件箱。
            sync.ensure(c)
            # AD-5、AD-22：榜、发布记录、图片转码缓存、已发布货的隐藏状态。
            publish.ensure(c)
            # 故事 15（AD-9、AD-12、AD-25）：行为账本、未识别事件、交接文件待重算标记。
            behavior.ensure(c)
            # 故事 17（CAP-8）：待审 / 已确认 / 已否决的规律。
            learning.ensure(c)
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
            config=judge_config(c,config)
            changed=old is None or old['listed_price']!=listed or old['currency']!=p['currency'] or old['title']!=p['title'] or (old['description'] or '')!=p['description']
            if changed:
                result=evaluate(_eval_item(p,triage.has_human(c,p['item_id'])),config)   # 口径同 humans()
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
            # 故事 19：主图变了，旧看图观察作废，也重新落位（看图移出的货回到新货、下轮重新看图）。
            if changed or (old and (old['image'] or '')!=(p['image'] or '')):triage.system_place(c,p['item_id'])
            # CAP-16：标题/描述变化、尚无结果或知识库变化时（重）生成标准名称，与判断同一事务。
            named=_std_name_or_none(c,p,config,changed)
            if named:write_std_name(c,p['item_id'],named,ts)
            # CAP-4：新货随判断写规则基线分（变化后旧分过期即被替换，AI 分下轮重算）。
            refresh_baseline(c,p['item_id'])
            # AD-5：已发布且曾因下架隐藏的货重新在售时，同一事务入发件箱取消隐藏。
            publish.reconcile(c,p['item_id'])
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
        # AD-5：已发布的货判下架时，与状态变更同一事务入发件箱隐藏。
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            if c.execute("UPDATE items SET status='gone',gone_at=?,checked_at=? WHERE item_id=? AND status='active'",(now(),now(),iid)).rowcount:
                publish.reconcile(c,iid)
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
            r=rejudge(c,judge_config(c,config))
            result=dict(checked=r['checked'],changed=r['changed'],blocked=r['blocked'],word=word)
            c.execute('INSERT INTO block_requests VALUES(?,?,?)',(request_id,payload,json.dumps(result,ensure_ascii=False)))
            return result

    def rejudge_all(self,config):
        """用当前规则（含库里的屏蔽词与已确认规律）重判全部货并刷新标准名称；只对未经人工决定的货移动拣选状态（AD-26）。
        判断与命名先在写锁外算好，再在一个短写事务里落库；期间有变化的货（签名不同）在事务里当场重算。
        知识库状态不是 ok 时不刷新标准名称（避免用残缺词表覆盖），报告 std_skipped。
        单件命名出错只计入 std_failed，不中断。幂等：再次运行改判数为 0。"""
        with self.connect() as c:
            rows=c.execute('SELECT * FROM items').fetchall()
            cfg=judge_config(c,config);human=humans(c)
        words,learned=cfg['blocked_words'],cfg['learned_rules']
        status=kb_status(cfg)
        refresh=status=='ok'
        failures=[]
        pre={}
        for r in rows:
            sig=(_eval_sig(r),r['item_id'] in human)
            pre[r['item_id']]=(sig,evaluate(_eval_item(dict(r),sig[1]),cfg))
        names={r['item_id']:((r['title'],r['description']),_safe_name(dict(r),cfg,failures)) for r in rows} if refresh else {}
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            now_cfg=judge_config(c,config)
            if (now_cfg['blocked_words'],now_cfg['learned_rules'])!=(words,learned):
                # 屏蔽词或已确认规律在此期间变了：预算结果作废，全部当场重算。
                cfg=now_cfg;pre={}
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
            # CAP-4：标准名称刷新后为全部新货补算基线分（未过期的 AI 分不动）。
            result['scores_written']=sum(refresh_baseline(c,r[0]) for r in c.execute("SELECT item_id FROM triage WHERE state='new'").fetchall())
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

    # ---- CAP-6 后台一页拣选（故事 7）----
    def triage_state(self,limit=50,offset=0,root=ROOT):
        """01 新货：待处理件数、已自动移出件数与队列卡片（分数高到低，捞回的在最前）。"""
        if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:raise ValueError('分页参数无效')
        with self.connect() as c:
            return dict(counts=backoffice_counts(c),queue=[card(c,r,root) for r in triage.queue(c,limit,offset)])
    def triage_feed(self,limit=FEED_BATCH,cursor=None,session=None,root=ROOT):
        """01 新货瀑布流（故事 22）：捞回的最前，其余按上架时间新到旧；卡片不带分数（详情里照旧显示）。
        session 为本次打开 01 的 session_id：别的打开里看过、没留下也没放回的货不再出现（「看过即不要」）。
        cursor 为上一批返回的 next_cursor（不透明字符串）；返回 dict(items, next_cursor, counts, header)。"""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('分页参数无效')
        with self.connect() as c:
            rows,nxt=triage.feed(c,limit,_decode_cursor(cursor),session)
            return dict(items=[_feed_card(c,r,root) for r in rows],next_cursor=_encode_cursor(nxt),
                        counts=_feed_counts(c,session),header=feed_header(c))
    def seen_unpicked(self,limit=FEED_BATCH,cursor=None,session=None,root=ROOT):
        """「看过未选」（故事 22）：按最近看到时间新到旧，游标续载；每件带 seen_at。"""
        if type(limit) is not int or not 1<=limit<=100:raise ValueError('分页参数无效')
        with self.connect() as c:
            rows,nxt=triage.seen_unpicked(c,limit,_decode_cursor(cursor),session)
            return dict(items=[dict(_feed_card(c,r,root),seen_at=r['seen_at']) for r in rows],next_cursor=_encode_cursor(nxt),
                        total=triage.seen_unpicked_count(c,session))
    def feed_header(self):
        with self.connect() as c:return feed_header(c)
    def record_owner_events(self,events):
        """故事 22：写入一批墨墨行为事件（≤100 条）。按 event_id INSERT OR IGNORE 去重；未知类型、
        不存在的 item_id、字段不合规的单条丢弃并计数，不整批失败。dwell 秒数截到 1–600，detail_dwell 截到 1–1800。
        返回 dict(stored, duplicate, dropped)。"""
        if not isinstance(events,list):raise ValueError('events 须为列表')
        if len(events)>OWNER_EVENT_BATCH:raise ValueError('每批最多 %d 条'%OWNER_EVENT_BATCH)
        stored=duplicate=dropped=0
        at=now()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            for ev in events:
                row=_owner_event(c,ev)
                if row is None:dropped+=1;continue
                cur=c.execute('INSERT OR IGNORE INTO owner_events(event_id,type,item_id,client_ts,at,session_id,params,eff_at) VALUES(?,?,?,?,?,?,?,?)',
                              (row['event_id'],row['type'],row['item_id'],row['client_ts'],at,row['session_id'],row['params'],_eff_at(row['client_ts'],at)))
                if cur.rowcount:stored+=1
                else:duplicate+=1
        return dict(stored=stored,duplicate=duplicate,dropped=dropped)
    def auto_removed_list(self,limit=50,offset=0,root=ROOT):
        if type(limit) is not int or not 1<=limit<=200 or type(offset) is not int or offset<0:raise ValueError('分页参数无效')
        with self.connect() as c:
            rows=triage.auto_removed(c,limit,offset)
            out=[]
            for r in rows:
                item=c.execute('SELECT * FROM items WHERE item_id=?',(r['item_id'],)).fetchone()
                if item is None:continue
                out.append(dict(card(c,item,root),reason=r['reason'],reason_lang=guess_lang(r['reason']),removed_at=r['removed_at']))
            return dict(items=out,total=backoffice_counts(c)['auto_removed'])
    def item_detail(self,iid,root=ROOT):
        with self.connect() as c:return item_detail(c,iid,root)
    def triage_act(self,action,iid,request_id,root=ROOT):
        """后台拣选动作（留下 / 去掉 / 捞回 / 撤销上一步）。同一 request_id 重放返回首次结果、不产生第二个事件；
        内容不同的重放报错。返回 dict(item_id, state, code, remaining, auto_removed, card)。"""
        if action not in ('keep','remove','rescue','undo'):raise ValueError('未知的拣选动作')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('请求编号无效')
        if action!='undo' and (not isinstance(iid,str) or not iid):raise ValueError('商品编号无效')
        payload=json.dumps([action,iid if action!='undo' else None],ensure_ascii=False)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev=c.execute('SELECT * FROM triage_requests WHERE request_id=?',(request_id,)).fetchone()
            if prev:
                if prev['payload']!=payload:raise ValueError('重复请求内容不同')
                return json.loads(prev['result'])
            if action=='undo':r=triage.undo(c)
            else:r=dict(getattr(triage,action)(c,iid,source='backoffice'),item_id=iid)
            item=c.execute('SELECT * FROM items WHERE item_id=?',(r['item_id'],)).fetchone()
            counts=backoffice_counts(c)
            result=dict(item_id=r['item_id'],state=r['state'],code=r['code'],remaining=counts['new'],
                        auto_removed=counts['auto_removed'],card=card(c,item,root) if item else None)
            c.execute('INSERT INTO triage_requests VALUES(?,?,?,?)',(request_id,payload,json.dumps(result,ensure_ascii=False),now()))
            return result
    def save_comment(self,iid,text):
        """墨墨点评（不用 AI）：覆盖保存；空文字删除点评。"""
        if not isinstance(iid,str) or not iid:raise ValueError('商品编号无效')
        if not isinstance(text,str) or len(text)>5000:raise ValueError('点评须为 5000 字以内的文字')
        with self.connect() as c:
            if not c.execute('SELECT 1 FROM items WHERE item_id=?',(iid,)).fetchone():raise ValueError('商品不存在')
            if text.strip():c.execute('INSERT OR REPLACE INTO comments VALUES(?,?,?)',(iid,text,now()))
            else:c.execute('DELETE FROM comments WHERE item_id=?',(iid,))
        return dict(item_id=iid,text=text if text.strip() else '')
    def save_alt(self,iid,index,text,root=ROOT):
        """图片替代文字：index 为当前图片列表里的序号，按该图的身份（文件名或地址）覆盖保存；空文字恢复默认。"""
        if not isinstance(iid,str) or not iid:raise ValueError('商品编号无效')
        if type(index) is not int or index<0:raise ValueError('图片序号无效')
        if not isinstance(text,str) or len(text)>500:raise ValueError('替代文字须为 500 字以内的文字')
        with self.connect() as c:
            item=c.execute('SELECT image FROM items WHERE item_id=?',(iid,)).fetchone()
            if not item:raise ValueError('商品不存在')
            images=item_images(c,iid,item['image'],root)
            if index>=len(images):raise ValueError('图片序号无效')
            key=images[index]['key']
            if text.strip():c.execute('INSERT OR REPLACE INTO alt_texts(item_id,image_key,image_index,text,updated_at) VALUES(?,?,?,?,?)',(iid,key,index,text.strip(),now()))
            else:c.execute('DELETE FROM alt_texts WHERE item_id=? AND image_key=?',(iid,key))
        return dict(item_id=iid,index=index,text=text.strip())
    def save_fee(self,iid,tier,request_id,root=ROOT):
        """CAP-10 手动改档：tier 为 A/B/C/D/ask；None 恢复自动推断。选的正好是推断值时同样清掉手动值（不留"粘住"的手动档）。
        每次改动记一条人工 fee 事件（triage_events，可追溯、不可撤销）。同一 request_id 重放返回首次结果。
        返回 dict(item_id, fee=fee_info)。"""
        if not isinstance(iid,str) or not iid:raise ValueError('商品编号无效')
        if tier is not None and tier not in FEE_TIERS:raise ValueError('档位无效')
        if not isinstance(request_id,str) or not 1<=len(request_id)<=100:raise ValueError('请求编号无效')
        payload=json.dumps(['fee',iid,tier],ensure_ascii=False)
        fees=fees_for(root)
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            prev=c.execute('SELECT * FROM triage_requests WHERE request_id=?',(request_id,)).fetchone()
            if prev:
                if prev['payload']!=payload:raise ValueError('重复请求内容不同')
                return json.loads(prev['result'])
            item=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
            if not item:raise ValueError('商品不存在')
            before=fee_info(c,item,fees)
            if tier is None or tier==before['auto']['tier']:c.execute('DELETE FROM fee_overrides WHERE item_id=?',(iid,))
            else:c.execute('INSERT OR REPLACE INTO fee_overrides(item_id,tier,updated_at) VALUES(?,?,?)',(iid,tier,now()))
            after=fee_info(c,item,fees)
            if (before['tier'],before['manual'])!=(after['tier'],after['manual']):
                note=('%s→%s'%(before['tier'],after['tier']))+('（手动）' if after['manual'] else '（自动）')
                triage.note_fee(c,iid,note,source='backoffice')
            result=dict(item_id=iid,fee=after)
            c.execute('INSERT INTO triage_requests VALUES(?,?,?,?)',(request_id,payload,json.dumps(result,ensure_ascii=False),now()))
            return result
MAX_IMAGES=16
FEE_TIERS=('A','B','C','D','ask')

def fee_info(c,item,fees=None):
    """一件货当前生效的预估费用档位：墨墨手动改过的优先（AD-2），否则规则推断（rules.fee_tier）。
    fees：已读好的 config.fee_tiers（调用方按自己的 root 读一次传进来；缺省读仓库里的）。
    返回 dict(tier, amount（欧元整数，请咨询为 None）, label, manual, auto={tier, amount, label, reason})。"""
    fees=fees or fees_for()
    item=dict(item)
    auto=rules.fee_tier(item,_std(c,item['item_id']),fees)
    row=c.execute('SELECT tier FROM fee_overrides WHERE item_id=?',(item['item_id'],)).fetchone()
    tier=row[0] if row else auto['tier']
    info=fees['tiers'].get(tier)
    return dict(tier=tier,amount=info['amount'] if info else None,label=info['label'] if info else '请咨询',
                manual=row is not None,auto={k:auto[k] for k in ('tier','amount','label','reason')})

def fee_choices(fees=None):
    """后台 03 费用档位的选项：[{tier, amount, label}]（A–D 与请咨询）。"""
    fees=fees or fees_for()
    return [dict(tier=k,amount=v['amount'],label=v['label']) for k,v in fees['tiers'].items()]+[dict(tier='ask',amount=None,label='请咨询')]

PLATFORM_LABELS=dict(ebay='eBay',etsy='Etsy',kleinanzeigen='Kleinanzeigen')
_DE=re.compile(r'[äöüßÄÖÜ]|\b(und|der|die|das|mit|nicht|keine?|sehr|guter?|zustand|tasse|teller|unterglasur)\b',re.I)
_EN=re.compile(r'\b(the|and|with|no|not|very|good|condition|cup|saucer|plate|porcelain|chips?|cracks?|please|see|photos?|pattern|marked?)\b',re.I)
_CJK=re.compile(r'[一-鿿]')

def guess_lang(text):
    """卖家原文的语言（只用于 lang 属性）：德语 de、英语 en、中文 None（随页面），无法判断 None。"""
    text=text or ''
    if not text.strip() or _CJK.search(text):return None
    if _DE.search(text):return 'de'
    return 'en' if _EN.search(text) else None

# ---- 故事 22：01 瀑布流与行为记录 ----

def _encode_cursor(key):
    return base64.urlsafe_b64encode(json.dumps(key,ensure_ascii=False).encode()).decode().rstrip('=') if key else None

def _decode_cursor(cursor):
    if not cursor:return None
    try:return json.loads(base64.urlsafe_b64decode(cursor.encode()+b'='*(-len(cursor)%4)))
    except (ValueError,TypeError,UnicodeError):raise ValueError('游标无效')

def _feed_card(c,row,root):
    d=card(c,row,root);d.pop('score',None);d.pop('score_reason',None);return d

def _feed_counts(c,session):
    """瀑布流计数：new 为本次打开实际能看到的件数（新货减去「看过即不要」排除的），seen 为被排除的件数。
    backoffice_counts 不变（其他调用方仍用）。"""
    out=backoffice_counts(c);seen=triage.seen_unpicked_count(c,session)
    return dict(out,new=max(0,out['new']-seen),seen=seen)

def feed_header(c,today=None):
    """瀑布流页头（故事 22）："今日已留下 N 件 · 商品池共 M 件"。今日按柏林日期；N 为今天未撤销的人工留下（按件去重），
    M 为当前留下状态的件数（与 02 商品池计数同一口径）。"""
    from zoneinfo import ZoneInfo
    berlin=ZoneInfo('Europe/Berlin')
    day=today or datetime.now(berlin).date()
    start=datetime(day.year,day.month,day.day,tzinfo=berlin)
    lo=start.astimezone(timezone.utc).isoformat(timespec='seconds')
    hi=(start+timedelta(days=1)).astimezone(timezone.utc).isoformat(timespec='seconds')   # 带时区的加法按当地钟面，跨夏令时也是次日零点
    kept_today=c.execute("""SELECT count(DISTINCT item_id) FROM triage_events WHERE actor='human' AND action='keep'
        AND undone_by IS NULL AND at>=? AND at<?""",(lo,hi)).fetchone()[0]
    pool=c.execute("SELECT count(*) FROM triage WHERE state='kept'").fetchone()[0]
    return dict(kept_today=kept_today,pool=pool)

def _eff_at(client_ts,at):
    """min(client_ts, at)，与 at 同一格式（UTC、秒、+00:00）；client_ts 无法解析、无时区或在 at 之后时取 at。"""
    if not client_ts:return at
    try:ts=datetime.fromisoformat(client_ts.replace('Z','+00:00'))
    except ValueError:return at
    if ts.tzinfo is None:return at
    ts=ts.astimezone(timezone.utc).replace(microsecond=0).isoformat(timespec='seconds')
    return ts if ts<at else at

def _short_str(v,n):
    return isinstance(v,str) and 1<=len(v)<=n

def _owner_event(c,ev):
    """校验一条行为事件，合规返回待写入的行，否则 None（调用方计入丢弃数）。"""
    if not isinstance(ev,dict):return None
    kind=ev.get('type');iid=ev.get('item_id')
    if kind not in OWNER_EVENT_TYPES:return None
    if not _short_str(ev.get('event_id'),100) or not _short_str(ev.get('session_id'),100) or not _short_str(iid,200):return None
    ts=ev.get('client_ts')
    if ts is not None and not _short_str(ts,64):return None
    params=ev.get('params')
    if params is not None and not isinstance(params,dict):return None
    params=dict(params) if params else {}
    if kind in OWNER_DWELL_MAX:
        sec=params.get('seconds')
        if isinstance(sec,bool) or not isinstance(sec,(int,float)) or sec!=sec:return None
        try:params['seconds']=round(min(max(float(sec),1.0),float(OWNER_DWELL_MAX[kind])),1)
        except OverflowError:return None      # 超大整数
    try:text=json.dumps(params,ensure_ascii=False,allow_nan=False) if params else None
    except (ValueError,TypeError):return None
    if text is not None and len(text)>2000:return None
    if not c.execute('SELECT 1 FROM items WHERE item_id=?',(iid,)).fetchone():return None
    # 放回只对仍在新货、在售的货有效；已留下 / 去掉 / 下架的丢弃（界面据此提示"未能放回"）。
    if kind=='restore' and not c.execute("""SELECT 1 FROM triage t JOIN items i ON i.item_id=t.item_id
            WHERE t.item_id=? AND t.state='new' AND i.status='active'""",(iid,)).fetchone():return None
    return dict(event_id=ev['event_id'],type=kind,item_id=iid,client_ts=ts,session_id=ev['session_id'],params=text)

def backoffice_counts(c):
    """01 计数与队列口径一致（不含 backlog 与已下架）；自动移出计全部当前处于该状态的货。"""
    new=c.execute("""SELECT count(*) FROM triage t JOIN items i ON i.item_id=t.item_id
        WHERE t.state='new' AND t.backlog=0 AND i.status='active'""").fetchone()[0]
    auto=c.execute("SELECT count(*) FROM triage WHERE state='auto_removed'").fetchone()[0]
    return dict(new=new,auto_removed=auto)

def price_label(amount,currency):
    """欧元显示 €185；非欧元显示原币 185 USD；无价格显示「价格待询」。"""
    if amount is None:return '价格待询'
    text=('%d'%amount) if float(amount).is_integer() else '%.2f'%amount
    return '€'+text if (currency or 'EUR').upper()=='EUR' else text+' '+currency.upper()

def item_images(c,iid,item_image='',root=ROOT):
    """图片列表 [{src, key}]：最近一次 product_snapshots 里已下载到本机的图（/archive/images/…），没有则 items.image。
    key 为图片身份（本机图文件名或图片地址），替代文字按它保存。损坏或空的快照记录跳过。"""
    try:
        rows=c.execute('SELECT data FROM product_snapshots WHERE item_id=? ORDER BY id DESC',(iid,)).fetchall()
    except sqlite3.OperationalError:
        rows=[]   # 快照表由 vision 模块建立，尚未建立时只用 items.image
    for row in rows:
        try:images=json.loads(row[0] or 'null')
        except (ValueError,TypeError):continue
        images=images.get('images') if isinstance(images,dict) else None
        if not isinstance(images,list):continue
        out=[]
        for img in images:
            if not isinstance(img,dict) or not isinstance(img.get('file'),str):continue
            name=img['file'].rsplit('/',1)[-1]
            if re.fullmatch(r'[a-f0-9]{64}\.(jpg|png|webp)',name) and (Path(root)/'data/porcelain/images'/name).is_file():
                out.append(dict(src='/archive/images/'+name,key=name))
        if out:return out[:MAX_IMAGES]
    return [dict(src=item_image,key=item_image)] if isinstance(item_image,str) and re.match(r'https?://',item_image) else []

def _std(c,iid):
    return c.execute('SELECT * FROM std_names WHERE item_id=?',(iid,)).fetchone()

def card(c,item,root=ROOT):
    """刷货卡与列表行共用的展示字段：标准名称（无则卖家标题并标 lang）、价格、平台、分数与理由、首图、货号、标签。"""
    item=dict(item);iid=item['item_id']
    std=_std(c,iid)
    named=std is not None and std['status']=='named' and bool(std['std_name'])
    score=c.execute('SELECT score,reason FROM scores WHERE item_id=?',(iid,)).fetchone()
    images=item_images(c,iid,item.get('image'),root)
    tags=[]
    if std is not None and 'overpaint' in std.keys() and std['overpaint']:tags.append('外画')
    if item.get('platform')=='kleinanzeigen':tags.append('仅后台 · Kleinanzeigen')
    return dict(item_id=iid,name=std['std_name'] if named else item.get('title') or '',
                name_lang=None if named else guess_lang(item.get('title')),
                price=price_label(item.get('listed_price'),item.get('currency')),
                platform=PLATFORM_LABELS.get(item.get('platform'),item.get('platform') or ''),
                score=score['score'] if score else None,score_reason=score['reason'] if score else None,
                image=images[0]['src'] if images else None,code=triage.code(c,iid),tags=tags,status=item.get('status'))

def item_detail(c,iid,root=ROOT):
    """货详情：货号（已留下才有）、名称与标签、图片与替代文字、卖家原文、来源与判断、分数、点评、拣选状态。"""
    item=c.execute('SELECT * FROM items WHERE item_id=?',(iid,)).fetchone()
    if not item:raise ValueError('商品不存在')
    out=card(c,item,root)
    images=item_images(c,iid,item['image'],root)
    saved=dict(c.execute('SELECT image_key,text FROM alt_texts WHERE item_id=?',(iid,)).fetchall())
    total=len(images)
    out['images']=[dict(src=im['src'],alt=saved.get(im['key']) or '%s · 第 %d 张 / 共 %d 张'%(out['name'],n+1,total),
                        default_alt='%s · 第 %d 张 / 共 %d 张'%(out['name'],n+1,total)) for n,im in enumerate(images)]
    comment=c.execute('SELECT text FROM comments WHERE item_id=?',(iid,)).fetchone()
    url=item['url'] or ''
    out.update(description=item['description'] or '',description_lang=guess_lang(item['description']),
               title=item['title'],url=url if re.match(r'https?://',url) else '',
               agent_reason=item['agent_reason'] or '',comment=comment[0] if comment else '',
               state=triage.state(c,iid),sold=triage.is_sold(c,iid))
    fees=fees_for(root)
    out.update(fee=fee_info(c,item,fees),fee_choices=fee_choices(fees))
    return out
