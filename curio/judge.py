"""
Curio · 选货判断规则（从 em watch 的 agent.py 迁移并修正）

三种处置（沿用 em watch）：
  want  要     —— 价格/系列合适，先备选
  probe 询市   —— 信息不全、VB、二等品等，先问价/问客群
  skip  不要   —— 硬过滤或经验上不要

规则都是"假设"，由你的判断案例不断修正。改规则只改这个文件。
"""
import re

# ---------------------------------------------------------------- 硬过滤（直接不入库）
HARD_FILTERS = [
    # 无关类目（Kleinanzeigen 搜 Meissen 会混进 Meißen 这座城市的房产/招聘/汽车）
    (r"stellengesuch|stellenangebot|\bjobs?\b|bewerbung|minijob|vollzeit|teilzeit|sicherheitsmitarbeiter",
     "招聘/求职"),
    (r"\bwohnung\b|\bimmo|\bimmobilie|\bmiet(?:e|vertrag|wohnung)\b|\bnachmieter\b|\bhäuser\b|"
     r"\bhaus\s+zu\s+(verkaufen|vermieten)|\b[23][\s-]?zimmer|etagenwohnung|einzimmerwohnung|massa-haus|bauen sie",
     "房产/租房"),
    (r"\bpostbote\b|\bkurier\b|\bfahrer\s+gesucht\b|\bauto\b|\bpkw\b|haustür|\btür\b", "劳务/汽车/建材"),
    (r"\bankauf\b|ankaufen|\bsuche\b|\bgesucht\b", "求购帖"),
    # 你确认的：纯白瓷 / 素白塑像（只带金饰的标题白瓷见 _NOT_PLAIN）
    (r"blanc\s*de\s*chine|biscuitporzellan|\bbiscuit\b|\bbiskuit\b|"
     r"wei[sß]{1,2}e[sn]?\s+porzellan|wei[sß]{1,2}porzellan|wei[sß]{1,2}ware|pure\s+white\s+porcelain|"
     r"ohne\s+dekor|undekoriert|ohne\s+malerei",
     "纯白瓷/无装饰白瓷"),
    (r"wei[sß]{1,2}e[sn]?\s+(figur|gruppe|putto|madonna)", "素白塑像"),
    # 你确认的：年度纪念盘 / 收藏盘
    (r"jahres(teller|tasse|becher|glocke|kugel|plakette)|sammelteller|weihnachts(teller|jahr|glocke|kugel)|"
     r"club\s*edition|wandteller\s+jahr|gedenk(?:teller|tasse|becher)|erinnerungs(?:teller|tasse)|commemorative",
     "年度纪念盘/收藏盘"),
    (r"\blampe\b|\bleuchte\b|tischlampe|porzellanlampe", "灯具"),
    (r"replik|replica|nachbildung|art\s+meissen|meissener\s+art|\bbuch\b|katalog|postkarte", "仿品/非瓷器"),
]
_HARD = [(re.compile(p, re.I), why) for p, why in HARD_FILTERS]

# ---------------------------------------------------------------- 品牌（只看标题，不看搜索关键词）
BRANDS = [("meissen", "Meissen"), ("meißen", "Meissen"), ("kpm", "KPM"),
          ("royal copenhagen", "Royal Copenhagen"), ("sèvres", "Sèvres"), ("sevres", "Sèvres"),
          ("rosenthal", "Rosenthal"), ("herend", "Herend")]

WANT_SIGNALS = [
    (r"rote\s+rose", "已知系列：红玫瑰，价格合适可要"),
    (r"malabar", "已知系列：KPM Malabar"),
    (r"musselmalet|blue\s+fluted", "已知系列：RC Musselmalet 小件常可备选"),
]
PROBE_SIGNALS = [
    (r"\bvb\b|verhandlungsbasis|preis\s*vorschlag", "价格未定/VB，适合询市"),
    (r"2\.\s*wahl|schliffstrich", "二等品，需确认客群，优先询市"),
]
SERIES = [(r"malabar", "Malabar"), (r"rote\s+rose", "红玫瑰"), (r"musselmalet|blue\s+fluted", "Musselmalet 蓝花"),
          (r"kurland", "Kurland"), (r"arkadia", "Arkadia"), (r"durchbruch", "镂空边"), (r"zwiebel", "洋葱纹"),
          (r"schwanenservice", "天鹅"), (r"indisch", "印度花")]
FORMS = [(r"tasse", "杯/杯碟"), (r"teller|platte", "盘"), (r"schale|schüssel", "碗/钵"), (r"vase", "瓶"),
         (r"kanne", "壶"), (r"figur|gruppe|putto", "塑像"), (r"dose", "盒"), (r"service|geschirr|\d+\s*tlg|teilig", "成套")]

# 价格经验（来自 em watch 的案例/经验，均为假设，可改）
MALABAR_CEILING = 380       # KPM Malabar 有利润价上限 €380
KPM_HIGH = 800              # KPM 无强系列时 >€800 默认不要
MEISSEN_HIGH = 2000         # Meissen ≥€2000 且非 VB、无明确题材 → 不要
RC_SMALL_MAX = 50           # RC 小件 ≤€50 常可要
CHEAP_ANY_BRAND = 30        # 有品牌且 ≤€30 → 先备选


# 标题里单独写 "weiß/weiss/white" 也当白瓷；只带金饰（"weiß / vergoldet"、"Goldrand"）也算白瓷（墨墨 2026-11-04）；
# 带彩绘/蓝白（如 "blau-weiß"、"mit blauem Dekor"、"Rote Rose"）不算
_WHITE_WORD = re.compile(r"\b(wei[sß]{1,2}|white)\b", re.I)
_NOT_PLAIN = re.compile(r"blau|blue|bunt|dekor|malerei|bemalt|kobalt|rose(?!nthal)|blume|indisch|zwiebel|"
                        r"watteau|kakiemon|imari|purpur|vogel|drache|landschaft|frücht|obst|blümchen|chines|chinoi|"
                        r"\brot\b|grün|farbig|famille", re.I)

# 故事 19 看图硬筛：AI 只给观察（AD-16），是否移出与理由只在这里；多项命中取第一项。
VISUAL_RULES = (
    ("single_cup", "硬筛（看图）：主图只有杯子，不带碟"),
    ("plain_white", "硬筛（看图）：白瓷（含金饰）"),
    ("not_porcelain", "硬筛（看图）：不是瓷器"),
)


def visual_filter(obs):
    """看图观察（dict：single_cup / plain_white / not_porcelain）命中即返回移出理由，否则 None。"""
    if not obs:
        return None
    keys = set(obs.keys())
    for key, why in VISUAL_RULES:
        if key in keys and obs[key]:
            return why
    return None

# KPM 不一定是 KPM Berlin：Krister / Bavaria / "KPM Germany" 是别的厂
_NOT_KPM_BERLIN = re.compile(r"krister|bavaria|kpm\s+germany|kups|schlesien|waldenburg", re.I)


def hard_filter(title):
    """命中硬过滤返回原因，否则 None。"""
    title = title or ""
    for rx, why in _HARD:
        if rx.search(title):
            return why
    if _WHITE_WORD.search(title) and not _NOT_PLAIN.search(title):
        return "纯白瓷（标题写 weiß）"
    return None


def _first(text, pairs):
    for p, name in pairs:
        if re.search(p, text, re.I):
            return name
    return ""


def detect_brand(title):
    low = (title or "").lower()
    for key, name in BRANDS:
        if key in low:
            return name
    return ""


def judge(title, price, price_text="", use_legacy_prices=True):
    """
    price：已解析好的欧元数值（0 或 None 表示未定/VB）。
    返回 dict(decision, reason, fair, brand, series, form)
    """
    title = title or ""
    low = f"{title} {price_text or ''}".lower()
    brand = detect_brand(title)
    series = _first(low, SERIES)
    form = _first(low, FORMS)
    price = price or None

    def out(decision, reason, fair=""):
        return {"decision": decision, "reason": reason, "fair": fair,
                "brand": brand, "series": series, "form": form}

    hf = hard_filter(title)
    if hf:
        return out("skip", f"硬过滤：{hf}")
    if not brand and not re.search(r"porzellan|porcelain", low):
        return out("skip", "标题里没有品牌/瓷器信号")

    decision, reason, fair = "probe", "信息不全，先询市", ""
    if brand == "KPM" and _NOT_KPM_BERLIN.search(title):
        return out("probe", "注意：可能是 KPM Krister/Bavaria 等，不是 KPM Berlin")
    for p, why in WANT_SIGNALS:
        if re.search(p, low, re.I):
            decision, reason = "want", why
    for p, why in PROBE_SIGNALS:
        if re.search(p, low, re.I):
            decision, reason = "probe", why

    if not use_legacy_prices:
        return out(decision, reason, fair)

    if "malabar" in low:
        fair = str(MALABAR_CEILING)
        if price and price > MALABAR_CEILING:
            decision, reason = "skip", f"Malabar 标价 €{price:g} 高于有利润价上限 €{MALABAR_CEILING}"
    elif brand == "Royal Copenhagen" and price and price <= RC_SMALL_MAX:
        decision, reason, fair = "want", f"RC 小件 ≤€{RC_SMALL_MAX}，常可进备选", str(RC_SMALL_MAX)
    elif brand == "KPM" and price and price > KPM_HIGH and not series:
        decision, reason = "skip", f"KPM 无明确系列且标价 >€{KPM_HIGH}，默认不要"
    elif brand == "Meissen" and price and price >= MEISSEN_HIGH and "vb" not in low:
        decision, reason = "skip", f"Meissen 标价 ≥€{MEISSEN_HIGH} 且无议价空间，需明确题材/客群，默认不要"
    elif brand and price and price <= CHEAP_ANY_BRAND:
        decision, reason = "want", f"{brand} 标价 €{price:g} 很低，先备选"

    return out(decision, reason, fair)


LABEL = {"want": "要", "probe": "询市", "skip": "不要"}


# ================================================================ 标准名称（CAP-16，AD-15）
# 词表唯一来源：EM 西洋瓷器知识库（只读）。本节只用 open(..., 'r') / stat / listdir 读取，
# 任何代码路径都不在知识库里写入、改名或建文件。
# 命名按知识库《瓷器描述字段和命名规则 V2》：年代 → 品牌 → 厂家等级 → 图案 → 器型 → 功能，
# 缺值段省略；卖家写的等级只存原文（seller_grade），不进名称；不从图案反推品牌。
# 生成结果全部是据卖家描述的候选，不是正式商品标题。
import csv
import hashlib
import datetime
import json
import logging
import os
import unicodedata
from pathlib import Path

log = logging.getLogger(__name__)

DEFAULT_KB_PATH = '~/Desktop/EM 西洋瓷器'
KB_BRANDS_DIR = '06 Brands and Manufactories 品牌与窑厂'
_MEISSEN = (KB_BRANDS_DIR, 'Meissen')
KB_FILES = {
    'patterns': _MEISSEN + ('4 Patterns 图案', 'Meissen_图案主题数据.csv'),
    'catalog': _MEISSEN + ('4 Patterns 图案', 'Meissen_官方图案目录数据.csv'),
    'forms': _MEISSEN + ('3 Forms and Relief 器型与浮雕', 'Forms 器型', 'Meissen_器型索引.csv'),
    'functions': ('05 General Knowledge 通用知识', '买手培训', '功能词表.csv'),
}
# 品牌中文名：知识库里 Meissen 没有中文别名，沿用 product_v2 的厂名中文（梅森）；其它品牌只写外文名。
BRAND_ZH = {'Meissen': '梅森'}
MEISSEN_BRAND = 'Meissen'

_SEP = r"[\s,.;:/\-–]*"
_SPLIT_TOKENS = re.compile(r"[\s,.;:/\-–]+")
_PAREN = re.compile(r'（[^（）]*）|\([^()]*\)')
_PAREN_INNER = re.compile(r'（([^（）]*)）|\(([^()]*)\)')
_LIST_SEP = re.compile(r'[；;／、]|\s/\s')
_LATIN_RUN = re.compile(r"[A-Za-z0-9À-ɏ°&'’][A-Za-z0-9À-ɏ°&'’ .,\-]*")
_NOT_LATIN_PREFIX = re.compile(r"^[^A-Za-zÀ-ɏ]*")
_STOP_TERMS = {'met', 'mak', 'skd', 'imm', 'v&a', 'moyu', 'ga', 'vb'}
_GRADE = re.compile(r"(?<![\w.])(?:(?:[1-4]|I{1,3}|IV)\s*\.\s*Wahl|(?:erste|zweite|dritte)\s+Wahl)\b", re.I)

_KB_CACHE = {}
_WARNED = set()


def _norm(s):
    """大小写、重音不敏感：casefold（ß→ss）+ 去掉组合附加符。"""
    s = unicodedata.normalize('NFKD', str(s or '')).casefold()
    return ''.join(ch for ch in s if not unicodedata.combining(ch)).replace('’', "'")


def _tokens(term):
    return [t for t in _SPLIT_TOKENS.split(_norm(term)) if t]


def _rx(term, compound=False, adjective=False):
    """词边界匹配；词内分隔符（空格、逗号、点、连字符）宽松。
    compound=True：允许作为德语复合词的词尾（如 Porzellanvase 中的 Vase），右侧仍须是词边界。
    adjective=True：品牌名后可接德语形容词词尾 -er（如 Meissener）。"""
    toks = _tokens(term)
    if not toks:
        return None
    body = _SEP.join(re.escape(t) for t in toks)
    left = '' if compound else r'(?<!\w)'
    right = r'(?:er)?(?!\w)' if adjective else r'(?!\w)'
    return re.compile(left + body + right)


def _cell_terms(cell, with_parens=False):
    """从知识库单元格（外文名、别名与卖家用词）里取出外文词条。
    括号内说明默认丢弃；以“注意 / 不…”开头的片段是反例说明，跳过。"""
    cell = str(cell or '')
    pieces = []
    stripped, prev = cell, None
    while prev != stripped:
        prev, stripped = stripped, _PAREN.sub(' ', stripped)
    pieces.append(stripped)
    if with_parens:
        pieces += [a or b for a, b in _PAREN_INNER.findall(cell)]
    out = []
    for piece in pieces:
        for frag in _LIST_SEP.split(piece):
            frag = frag.strip()
            prefix = _NOT_LATIN_PREFIX.match(frag).group(0)
            # 反例说明（注意…、不写…、…另立/已拆为其它条目）与带日期/编号前缀的批注（如“2026-10-24 补：…”）跳过
            if ('注意' in prefix or prefix.strip().startswith('不') or re.search(r'\d', prefix)
                    or any(w in frag for w in ('另立', '已拆', '拆为'))):
                continue
            m = _LATIN_RUN.match(frag[len(prefix):])
            if not m:
                continue
            term = m.group(0).strip(" .,-'’")
            key = _norm(term)
            if len(key) < 3 or key in _STOP_TERMS or key.replace(' ', '').isdigit():
                continue
            out.append(term)
            # “Kaffeetasse m. U.” 这类型录写法，另取主词 Kaffeetasse
            head = re.sub(r'\s+m\.\s*[A-Za-z]\.?$', '', term)
            if head != term and len(head) >= 3:
                out.append(head)
    return out


def _read_csv(path):
    with open(path, 'r', encoding='utf-8-sig', newline='') as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError('空表')
    return rows


def _fingerprint(root):
    """所用 CSV（及品牌文件夹列表）的修改时间与大小摘要；用于缓存失效与结果追溯。"""
    h = hashlib.sha256()
    try:
        brands = sorted(e.name for e in os.scandir(root / KB_BRANDS_DIR) if e.is_dir() and not e.name.startswith('.'))
    except OSError:
        brands = None
    h.update(repr(brands).encode())
    for key in sorted(KB_FILES):
        p = root.joinpath(*KB_FILES[key])
        try:
            st = p.stat()
            h.update(f'{key}:{st.st_mtime_ns}:{st.st_size};'.encode())
        except OSError:
            h.update(f'{key}:absent;'.encode())
    return h.hexdigest()[:16]


def _vocab_entries(terms, payload, compound=False):
    out = []
    for term in terms:
        rx = _rx(term)
        if rx:
            toks = _tokens(term)
            out.append((rx, toks[0], len(''.join(toks)), False, payload))
            if compound and len(toks) == 1 and len(toks[0]) >= 4:
                out.append((_rx(term, compound=True), toks[0], len(toks[0]), True, payload))
    return out


def _dedupe(entries):
    """同一词条只归第一个出现的 ID（按 CSV 顺序），避免同词多义时结果抖动。"""
    seen, out = set(), []
    for e in entries:
        k = (e[0].pattern, e[3])
        if k not in seen:
            seen.add(k)
            out.append(e)
    return out


def _load_kb(root):
    vocab = dict(brands=[], patterns=[], codes={}, code_prefixes=[], forms=[], functions=[])
    problems = []
    # 品牌：品牌与窑厂下的文件夹名
    try:
        names = sorted(e.name for e in os.scandir(root / KB_BRANDS_DIR) if e.is_dir() and not e.name.startswith('.'))
        if not names:
            raise ValueError('无品牌文件夹')
        for name in names:
            rx = _rx(name, adjective=True)
            if rx:
                vocab['brands'].append((rx, _tokens(name)[0], name))
    except (OSError, ValueError) as e:
        problems.append('brands')
    brand_norms = {_norm(b[2]) for b in vocab['brands']}
    # 功能（全部品牌通用）
    try:
        entries = []
        for r in _read_csv(root.joinpath(*KB_FILES['functions'])):
            fid, zh = (r.get('功能ID') or '').strip(), (r.get('中文名称') or '').strip()
            if not fid:
                continue
            terms = []
            for col in ('德文', '英文', '别名与卖家用词'):
                terms += _cell_terms(r.get(col))
            # 品牌名不作功能词（批注里常出现“Meissen 件…”）
            terms = [x for x in terms if _norm(x) not in brand_norms and not re.search(r'\d', x)]
            entries += _vocab_entries(terms, (fid, zh or fid), compound=True)
        if not entries:
            raise ValueError('无可用词条')
        vocab['functions'] = _dedupe(entries)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeDecodeError):
        problems.append('functions')
    # Meissen 图案主题（MP-xxx）
    mp_by_code = {}
    try:
        entries = []
        for r in _read_csv(root.joinpath(*KB_FILES['patterns'])):
            pid, zh = (r.get('图案ID') or '').strip(), (r.get('中文名称') or '').strip()
            if not pid:
                continue
            payload = (pid, zh or pid)
            entries += _vocab_entries(_cell_terms(r.get('外文或通用名称'), with_parens=True), payload)
            for code in re.split(r'[、,，;；/\s]+', r.get('图案编号') or ''):
                code = code.strip()
                if re.fullmatch(r'\d{6}', code):
                    mp_by_code.setdefault(code, payload)
                elif re.fullmatch(r'\d{1,5}x+', code, re.I):
                    vocab['code_prefixes'].append((code.rstrip('xX'), payload))
        if not entries:
            raise ValueError('无可用词条')
        vocab['patterns'] += _dedupe(entries)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeDecodeError):
        problems.append('patterns')
    vocab['codes'].update(mp_by_code)

    def mp_for(code):
        if code in mp_by_code:
            return mp_by_code[code]
        return _prefix_match(vocab['code_prefixes'], code)

    # Meissen 官方图案目录（六位编号）：编号已归入某个 MP 时用 MP，保证同类分组一致
    try:
        rows = _read_csv(root.joinpath(*KB_FILES['catalog']))
        by_name = {}
        for r in rows:
            code = (r.get('图案编号') or '').strip()
            if not re.fullmatch(r'\d{6}', code):
                continue
            base = Path((r.get('档案路径') or '').replace('\\', '/')).stem
            zh = re.search(r'[一-鿿][一-鿿（）()·／]*$', base)
            payload = mp_for(code) or (code, zh.group(0) if zh else (r.get('名称（德文）') or code).strip())
            vocab['codes'].setdefault(code, payload)
            name = (r.get('名称（德文）') or '').strip()
            if name:
                by_name.setdefault(name, []).append((code, _norm(r.get('金绘（德文）') or ''), payload))
        entries = []
        for name, options in by_name.items():
            options.sort()
            entries += _vocab_entries([name], ('catalog', tuple(options)))
        if not rows:
            raise ValueError('空表')
        vocab['patterns'] += _dedupe(entries)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeDecodeError):
        problems.append('catalog')
    # Meissen 器型（MS-xxx）
    try:
        entries = []
        for r in _read_csv(root.joinpath(*KB_FILES['forms'])):
            sid, zh = (r.get('器型ID') or '').strip(), (r.get('中文名称') or '').strip()
            if not sid:
                continue
            if not zh or zh == '待定':
                zh = _cell_terms(r.get('德文或官方名称'))[:1] or [sid]
                zh = zh[0]
            entries += _vocab_entries(_cell_terms(r.get('德文或官方名称')), (sid, zh))
        if not entries:
            raise ValueError('无可用词条')
        vocab['forms'] = _dedupe(entries)
    except (OSError, ValueError, KeyError, csv.Error, UnicodeDecodeError):
        problems.append('forms')
    status = 'ok' if not problems else 'partial:' + ','.join(problems)
    return vocab, status


def kb_vocab(path=None):
    """读取（带缓存）知识库词表；返回 (vocab, kb_status, kb_fingerprint)。
    kb_status：ok / missing（路径不存在）/ partial:缺失或无法解析的部分。"""
    root = Path(os.path.expanduser(path or DEFAULT_KB_PATH))
    if not root.is_dir():
        _warn_once(str(root), 'missing', f'EM 西洋瓷器知识库不存在：{root}；标准名称只用可得段')
        return dict(brands=[], patterns=[], codes={}, code_prefixes=[], forms=[], functions=[]), 'missing', ''
    fp = _fingerprint(root)
    cached = _KB_CACHE.get(str(root))
    if cached and cached[0] == fp:
        return cached[1], cached[2], fp
    vocab, status = _load_kb(root)
    _KB_CACHE[str(root)] = (fp, vocab, status)
    if status != 'ok':
        _warn_once(str(root), status, f'EM 西洋瓷器知识库部分词表缺失或无法解析（{status}）：{root}')
    return vocab, status, fp


def _warn_once(root, status, message):
    if (root, status) not in _WARNED:
        _WARNED.add((root, status))
        log.warning(message)


def _best(entries, text):
    """在一段文字里找最具体的词条：词条越长越优先，同长时整词优先于复合词尾。"""
    best = None
    for i, (rx, first, score, compound, payload) in enumerate(entries):
        if first not in text:
            continue
        if rx.search(text):
            key = (score, not compound, -i)
            if best is None or key > best[0]:
                best = (key, payload)
    return best[1] if best else None


def _prefix_match(prefixes, code):
    """NNxxxx 式编号：取最长的匹配前缀，与 CSV 顺序无关。"""
    best = None
    for prefix, payload in prefixes:
        if code.startswith(prefix) and (best is None or len(prefix) > len(best[0])):
            best = (prefix, payload)
    return best[1] if best else None


# “im Stil von Meissen”“wie Meissen”“Meissen-Art”之类不是品牌证据
_BRAND_NEG_BEFORE = re.compile(r'(?:stil von|art von|wie|nicht|a la|style of|im stil)\s*$')
_BRAND_NEG_AFTER = re.compile(r'^(?:-art|-stil|\s+style)\b')


def _brand(vocab, text):
    hits = []
    for rx, first, name in vocab['brands']:
        if first in text:
            for m in rx.finditer(text):
                if _BRAND_NEG_BEFORE.search(text[:m.start()]) or _BRAND_NEG_AFTER.match(text[m.end():]):
                    continue
                hits.append((m.start(), -len(m.group(0)), name))
                break
    return min(hits)[2] if hits else None


def _pattern(vocab, text):
    for code in re.findall(r'(?<!\d)(\d{6})(?!\d)', text):
        if code in vocab['codes']:
            return vocab['codes'][code]
        hit = _prefix_match(vocab['code_prefixes'], code)
        if hit:
            return hit
    hit = _best(vocab['patterns'], text)
    if hit and hit[0] == 'catalog':
        options = hit[1]
        gilded = []
        for o in options:
            rx = _rx(o[1]) if o[1] else None   # 金绘格只有“-”“/”等分隔符时无词条
            if rx and rx.search(text):
                gilded.append(o)
        return (gilded or options)[0][2]
    return hit


_YEAR = r'(1[7-9]\d\d|20[0-2]\d)'
_RANGE = re.compile(r'(?<![\d.,])' + _YEAR + r'\s*[-–/]\s*' + _YEAR + r'(?![\d.,]|\s*(?:€|eur|cm|mm|ml|g\b))')
_SINGLE = re.compile(r'(?:\b(um|ca\.?|circa|approx\.?|around|gegen)\s*)?(?<!\d[.,])(?<![\d€])' + _YEAR + r'(?![\d.,]|\s*(?:€|eur|euro|,-|\.-|cm|mm|ml|g\b|stk|tlg))')
_CENTURY = re.compile(r'(?<!\d)(1[6-9]|20)\.\s*(?:/\s*(1[7-9]|20|21)\.\s*)?(?:jh\b\.?|jahrhundert)')
# 年份前是创立、价格、币种、购买或运费标签时不算年代
_FOUNDED = re.compile(r'(?:seit|since|gegr\w*|founded|est\.?|established|preis|eur|€|vb|gekauft|kauf|versand|neuware)[\s:.]*$')


def _era(text):
    m = _RANGE.search(text)
    if m and int(m.group(1)) < int(m.group(2)) and not _FOUNDED.search(text[:m.start()]):
        return f'{m.group(1)}–{m.group(2)}年'
    for m in _SINGLE.finditer(text):
        if _FOUNDED.search(text[:m.start()]):
            continue
        return ('约' if m.group(1) else '') + m.group(2) + '年'
    m = _CENTURY.search(text)
    if m:
        return f'{m.group(1)}–{m.group(2)}世纪' if m.group(2) else f'{m.group(1)}世纪'
    return None


def _in_order(title, description, fn):
    """先看标题，标题里没有再看描述。"""
    for text in (title, description):
        if text:
            hit = fn(text)
            if hit:
                return hit
    return None


# 外画（Hausmalerei 等）：只标记、不扣分；进入标准名称与分组键，同类比价只与外画货比较。
# 在 _norm 之后匹配：ß→ss、ä/ü 去附加符，另收 ae/ue 写法。
_OVERPAINT = re.compile(r"\bhausmaler(?:ei|in)?\b|aussenmalerei|nachtr(?:a|ae)glich\s+(?:bemalt|staffiert)|"
                        r"\b(?:u|ue)bermalt\b|fremdbemal\w*|fremdmalerei|\bover[\s-]?paint(?:ed|ing)?\b|"
                        r"outside[\s-]+decorated|later\s+decorated|decorated\s+outside\s+the\s+factory")
OVERPAINT_LABEL = '外画'


_OVERPAINT_NEG = re.compile(r"\b(?:kein\w*|nicht|ohne|no|not|without)\s+(?:\w+\s+)?$")
_OVERPAINT_POSTNEG = re.compile(r"\W*(?:nein|keine?|no|not)\b")


def overpaint(title, description=''):
    """标题或描述里有外画说法时返回 True（大小写、ß/ss 不敏感）；紧跟否定（如 nicht übermalt）的不算。"""
    for text in (title, description):
        t = _norm(text)
        for m in _OVERPAINT.finditer(t):
            if not _OVERPAINT_NEG.search(t[max(0, m.start() - 40):m.start()]) and not _OVERPAINT_POSTNEG.match(t[m.end():]):
                return True
    return False


def standard_name(title, description='', config=None):
    """据卖家标题/描述生成候选标准名称（不调用 AI，不影响判断结论）。

    返回 dict：brand / brand_display、pattern_id / pattern、form_id / form、function_id / function、
    era、seller_grade（卖家原文，不入名）、std_name（无器型且无功能时为 None）、
    std_key（品牌|图案ID|器型ID|功能ID，缺值写 -；外画货末尾追加 |外画）、overpaint、status（named / insufficient）、kb_status、kb_fingerprint。
    """
    path = (config or {}).get('em_kb_path') or DEFAULT_KB_PATH
    vocab, kb_status, fp = kb_vocab(path)
    t, d = _norm(title), _norm(description)
    brand = _in_order(t, d, lambda x: _brand(vocab, x))
    pattern = form = None
    if brand == MEISSEN_BRAND:
        pattern = _in_order(t, d, lambda x: _pattern(vocab, x))
        form = _in_order(t, d, lambda x: _best(vocab['forms'], x))
    function = _in_order(t, d, lambda x: _best(vocab['functions'], x))
    era = _era(t)   # 年代只看标题：描述里多是价格、购买日期等
    grade = None
    for text in (title or '', description or ''):
        m = _GRADE.search(text)
        if m:
            grade = m.group(0)
            break
    brand_display = (brand + BRAND_ZH.get(brand, '')) if brand else None
    named = bool(form or function)
    # 厂家等级须可靠证据（V2），卖家陈述不入名，故名称中此段始终省略。
    painted = overpaint(title, description)
    parts = [era, brand_display, painted and OVERPAINT_LABEL, pattern and pattern[1], form and form[1], function and function[1]]
    return dict(
        brand=brand, brand_display=brand_display,
        pattern_id=pattern and pattern[0], pattern=pattern and pattern[1],
        form_id=form and form[0], form=form and form[1],
        function_id=function and function[0], function=function and function[1],
        era=era, seller_grade=grade,
        std_name=' '.join(p for p in parts if p) if named else None,
        std_key='|'.join(x or '-' for x in (brand, pattern and pattern[0], form and form[0], function and function[0]))
        + ('|' + OVERPAINT_LABEL if painted else ''),
        overpaint=painted,
        status='named' if named else 'insufficient',
        kb_status=kb_status, kb_fingerprint=fp)


# ================================================================ 预估费用档位（CAP-10，FR-21a/21b，AD-15）
# 档位金额与词表是数据（config/fee_tiers.yaml，经 curio/config.fee_tiers 读取校验）；推断只在这里。
# 规则（fee-tiers.md）：从卖家标题/描述读尺寸（cm、mm、英寸；H、Höhe、Ø、Durchmesser 等），取读到的最大值；
#   杯碟类：没写尺寸或不超过 A 档上限 → A；否则按尺寸：≤B → B、≤C → C、≤D → D、超过 → 请咨询；
#   人物瓷偶等难包装的上跳一档（D 上跳为请咨询）；特别易碎或读不出尺寸 → 请咨询。
# 结果带理由（如「高 28 cm → B」），只给后台看，不进快照。

_NUM = r'\d{1,4}(?:[.,]\d{1,2})?'
_UNIT = r'(cm|mm|zentimeter|centimeters?|inch(?:es)?|["″])'
# 一组数值 + 单位：「28 cm」「28,5cm」「30 x 20 x 12 cm」「280 mm」「11"」。左边不能紧贴数字或小数点。
_DIMS = re.compile(r'(?<![\d.,])(' + _NUM + r'(?:\s*(?:x|×|\*)\s*' + _NUM + r')*)\s*' + _UNIT + r'(?![a-z])')
# 带高度/直径标签、没写单位的数值（按厘米）：「Höhe: 28」「Durchmesser 24」「Ø 24」。
_LABELLED = re.compile(r'(?:hohe|height|durchmesser|diameter|ø|⌀)\s*[:=]?\s*(?:ca\.?|circa|approx\.?|etwa)?\s*'
                       r'(' + _NUM + r')(?![\d.,]|\s*' + _UNIT + r'|\s*(?:m|l|ml|cl|g|kg)\b)')
_HEIGHT_LABEL = re.compile(r'(?:\bh\b\.?|\bhohe\b|\bhoch\b|\bheight\b|\bhigh\b|\btall\b|\bgesamthohe\b)[^\d]{0,12}$')
_DIAM_LABEL = re.compile(r'(?:ø|⌀|\bdurchmesser\b|\bdm\b\.?|\bd\b\.?|\bdiameter\b|\bdiam\b\.?)[^\d]{0,12}$')
_HEIGHT_AFTER = re.compile(r'^\s*(?:hoch|high|tall)\b')
_PACKAGE = re.compile(r'(?:karton|paket|verpackung|versand|shipping|package)\w*[^.;,\n]{0,40}$')
_MIN_CM, _MAX_CM = 0.5, 500.0
_ORDER = ('A', 'B', 'C', 'D', 'ask')
_TIER_NAME = {'ask': '请咨询'}


def _num(text):
    return float(text.replace(',', '.'))


def _cm(value, unit):
    unit = unit.lower()
    if unit == 'mm':
        return value / 10
    if unit.startswith('inch') or unit in ('"', '″'):
        return value * 2.54
    return value


def read_size(*texts):
    """从文字里读出最大的尺寸（厘米）及其种类（高 / 直径 / 尺寸）。读不出返回 None。"""
    best = None
    for text in texts:
        t = _norm(str(text or '').replace('″', '"').replace('×', 'x'))
        found = []
        for m in _DIMS.finditer(t):
            before = t[max(0, m.start() - 60):m.start()]
            if _PACKAGE.search(before):
                continue                    # 包装 / 运输尺寸不算
            values = [_cm(_num(v), m.group(2)) for v in re.split(r'\s*[x*]\s*', m.group(1))]
            labelled_h = _HEIGHT_LABEL.search(before) or _HEIGHT_AFTER.match(t[m.end():])
            labelled_d = _DIAM_LABEL.search(before)
            if m.group(2) == '"':
                # 英寸符号只认紧贴数字、且在尺寸语境里（H / Höhe / Ø 标签或 a x b 多维）：「Nr. 30"」是型号不是尺寸。
                if m.group(0)[-2].isspace() or (len(values) == 1 and not (labelled_h or labelled_d)):
                    continue
            kind = '尺寸' if len(values) > 1 else ('高' if labelled_h else '直径' if labelled_d else '尺寸')
            found += [(v, kind) for v in values]
        for m in _LABELLED.finditer(t):
            if _PACKAGE.search(t[max(0, m.start() - 60):m.start()]):
                continue
            kind = '直径' if re.match(r'(?:durchmesser|diameter|ø|⌀)', m.group(0)) else '高'
            found.append((_num(m.group(1)), kind))
        for v, kind in found:
            if _MIN_CM <= v <= _MAX_CM and (best is None or v > best[0]):
                best = (v, kind)
    return best


def _cm_text(v):
    return ('%d' % v) if float(v).is_integer() else ('%.1f' % v).rstrip('0').rstrip('.')


def _std_get(std, key):
    if std is None:
        return None
    try:
        return std[key]
    except (KeyError, IndexError):
        return None


def _std_words(std):
    """标准名称里可用于器型判断的中文与 ID：功能 ID、器型/功能中文名、标准名称本身。"""
    names = [_std_get(std, 'std_name') or '']
    seg = _std_get(std, 'segments')
    if isinstance(seg, str):
        try:
            seg = json.loads(seg)
        except ValueError:
            seg = None
    if isinstance(seg, dict):
        names += [seg.get('form') or '', seg.get('function') or '']
    for k in ('form', 'function'):
        v = _std_get(std, k)
        if isinstance(v, str):
            names.append(v)
    return _std_get(std, 'function_id'), ' '.join(names)


def _category(fees, function_id, t_title, zh):
    """(人物瓷偶类, 杯碟类)。依据先后：标准名称的功能 ID → 标题里的词 → 标准名称的中文。
    两类的词都出现时，取先出现的那个为货本身的器型（「Tasse mit Watteau-Figuren」是杯，后面是装饰）。"""
    cups, figs = fees['cups'], fees['hard_to_pack']
    if function_id and function_id in cups['function_ids']:
        return False, True
    if function_id and function_id in figs['function_ids']:
        return True, False
    for text, key in ((t_title, 'rx'), (zh, 'zh')):
        c = cups[key].search(text) if cups[key] else None
        f = figs[key].search(text) if figs[key] else None
        if c or f:
            fig = bool(f) and (not c or f.start() < c.start())
            return fig, not fig
    return False, False


def fee_tier(title, description='', std=None, fees=None):
    """推断预估费用档位。fees：config.fee_tiers() 的结果。std：std_names 行或 standard_name 结果（可空）。
    返回 dict(tier='A'|'B'|'C'|'D'|'ask', amount=欧元整数或 None, label, reason, size_cm)。"""
    if fees is None:
        from .config import fee_tiers
        fees = fee_tiers()
    tiers = fees['tiers']
    t_title, t_all = _norm(title), _norm(f'{title or ""}\n{description or ""}')
    function_id, zh = _std_words(std)

    def out(tier, reason, size=None):
        info = tiers.get(tier)
        return {'tier': tier, 'amount': info['amount'] if info else None,
                'label': info['label'] if info else _TIER_NAME['ask'], 'reason': reason,
                'size_cm': size}

    frag = fees['fragile']['rx'].search(t_all) if fees['fragile']['rx'] else None
    if frag:
        return out('ask', f'特别易碎（{frag.group(0)}）→ 请咨询')

    figure, cup = _category(fees, function_id, t_title, zh)
    size = read_size(title, description)
    if size is None:
        if cup:
            return out('A', '杯碟类，未写尺寸 → A')
        return out('ask', ('人物瓷偶，' if figure else '') + '读不出尺寸 → 请咨询')
    cm, kind = size
    said = f'{kind} {_cm_text(cm)} cm'
    if cup and cm <= tiers['A']['max_cm']:
        return out('A', f'杯碟类，{said} → A', cm)
    tier = next((k for k in ('B', 'C', 'D') if cm <= tiers[k]['max_cm']), 'ask')
    if tier == 'ask':
        return out('ask', f'{said}，超过 {_cm_text(tiers["D"]["max_cm"])} cm → 请咨询', cm)
    if figure:
        up = _ORDER[_ORDER.index(tier) + 1]
        name = _TIER_NAME.get(up, up)
        return out(up, f'人物瓷偶，{said} → {tier}，难包装上跳一档 → {name}', cm)
    return out(tier, f'{said} → {tier}', cm)


# ================================================================ 客人匹配（CAP-5，FR-9，AD-15；故事 16）
# 词表与权重是数据（config/matching.yaml，经 curio/config.match_rules 读取校验）；对照与分级只在这里。
# 只看货的标准名称（品牌、图案、器型/功能）与卖家标题，对照 GoodCustomer 的需求与喜好（只读，不调用 AI）：
#   明说想要：needs.jsonl 里 status=open 的需求——品牌对上（maker，或没写 maker 时需求文字里提到品牌），
#             写了图案（pattern_names_raw）的须对上图案，写了器型的、货的器型已知时须对上器型；
#   明说喜欢 / 推算口味：preferences.jsonl 里 polarity=like、status=active 的维度对上（maker → 品牌、pattern → 图案、
#             form/object_type → 器型；比较喜好的 value，value 为空才用 value_raw）；对上的喜好里须有一条提到
#             这件货的品牌、或对上只属于该品牌的图案词组（只对上器型或通用图案不算）；
#             明说（explicit）的品牌或只属于该品牌的图案对上即"明说喜欢"，否则"推算口味"（只对上器型的明说不升级）；
#             detail_required 里的分级还须有一条图案或器型喜好也对上（只对上品牌太宽：一件梅森会推给大半客户）；
#             明说想要在 detail_required 里时，需求也须对上图案或器型（品牌只是门槛，不单独构成匹配）；
#   任一 status=active、polarity=dislike 的喜好对上同一维度 → 这位客人不匹配这件货（需求也不算）。
# 结果只给后台 04 看，不进快照。

_LATIN = re.compile(r'[a-z0-9]')
# 普通否定只看别名自己那一小段（到"、"为止）；排除词（以外 / 除…）看整句（"、"并列也算在内）。
_CLAUSE_END = re.compile(r'[,，、。;；!！?？()（）\n]')
_SENTENCE_END = re.compile(r'[,，。;；!！?？()（）\n]')
MATCH_REASON_MAX = 40
MATCH_REASON_VALUES = 3


def _term_rx(term, compound=False):
    """外文词的整词正则（compound=True 时左侧可接德语复合词前缀，右侧仍须是词边界）。"""
    left = '' if compound else r'(?<![a-z0-9])'
    return re.compile(left + re.escape(term) + r'(?![a-z0-9])')


def _term_spans(text, term, compound=False):
    """term（已规范化）在 text（已规范化）里出现的位置：外文词按整词 / 复合词词尾，中文按包含。"""
    if not term or term not in text:
        return []
    if not _LATIN.search(term):
        out, i = [], text.find(term)
        while i >= 0:
            out.append((i, i + len(term)))
            i = text.find(term, i + 1)
        return out
    return [m.span() for m in _term_rx(term, compound).finditer(text)]


def _term_in(text, term, compound=False):
    """term（已规范化）是否出现在 text（已规范化）里（规则同 _term_spans）。"""
    return bool(_term_spans(text, term, compound))


def _brand_aliases(rules, brand):
    b = _norm(brand)
    return [b] + rules['brands'].get(b, [])


def _brand_hit(text, brand, rules, negate=True):
    """客人文字（已规范化）里是否提到这个品牌（词见 matching.yaml）。
    排除（"梅森、老德以外的小牌子""除了梅森、KPM"）：整句里有排除词时始终不算提到——喜欢、需求、不喜欢都一样。
    普通否定（"不一定梅森"）：只在 negate=True 时检查，只看别名自己那一小段；不喜欢的喜好传 negate=False
    （"不要梅森"就是不喜欢梅森）。"""
    if not brand or not text:
        return False
    for alias in _brand_aliases(rules, brand):
        start = 0
        while alias and (i := text.find(alias, start)) >= 0:
            start = i + 1
            # 外文别名按整词（只看字母边界：「kpm73号」算 KPM）
            if _LATIN.search(alias) and (re.match(r'[a-z]', text[i + len(alias):i + len(alias) + 1])
                                         or (i and re.match(r'[a-z]', text[i - 1]))):
                continue
            tail = _SENTENCE_END.split(text[i + len(alias):], 1)[0]
            head = _SENTENCE_END.split(text[:i])[-1]
            if any(n in tail for n in rules['negations']) or any(n in head for n in rules['exclusions_before']):
                continue
            if negate:
                near = _CLAUSE_END.split(text[max(0, i - 8):i])[-1].rstrip()
                if any(near.endswith(n) for n in rules['negations_before']):
                    continue
            return True
    return False


def _groups_in(texts, groups, compound=False):
    """在各段文字里找词组命中；同一处被几个词覆盖时只算最长的那个（Untertasse 是碟不是杯，Bildplatte 是瓷板不是盘）。
    groups：{组名: [词…]}。返回命中的组名集合。"""
    found = set()
    for text in texts:
        if not text:
            continue
        hits = [(a, b, name) for name, terms in groups.items() for term in terms
                for a, b in _term_spans(text, term, compound)]
        for a, b, name in hits:
            if not any(a2 <= a and b <= b2 and (b2 - a2) > (b - a) and n2 != name for a2, b2, n2 in hits):
                found.add(name)
    return found


def match_facts(std, title, rules):
    """一件货用于匹配的要素：品牌、图案（ID、中文名、所属词组）、器型词组。std 为 std_names 行或 dict（可空）。"""
    seg = _std_get(std, 'segments')
    if isinstance(seg, str):
        try:
            seg = json.loads(seg)
        except ValueError:
            seg = None
    seg = seg if isinstance(seg, dict) else {}
    brand = _std_get(std, 'brand')
    pattern_id = _std_get(std, 'pattern_id')
    pattern = _norm(seg.get('pattern') or '')
    t = _norm(title)
    b = _norm(brand) if brand else ''
    scoped = {n: g for n, g in enumerate(rules['patterns']) if not g['brands'] or b in g['brands']}
    by_terms = _groups_in((pattern, t), {n: g['terms'] for n, g in scoped.items()}, compound=True)
    groups = sorted(n for n, g in scoped.items() if (pattern_id and pattern_id in g['ids']) or n in by_terms)
    form_text = _norm(' '.join(x for x in (seg.get('form'), seg.get('function'), _std_get(std, 'std_name'))
                               if isinstance(x, str) and x))
    forms = _groups_in((form_text, t), rules['forms'], compound=True)
    return dict(brand=brand, pattern_id=pattern_id, pattern=pattern, pattern_groups=groups, forms=forms)


def _pattern_how(text, facts, rules):
    """客人文字（已规范化）是否对上这件货的图案：None / 'name'（货的图案名出现在文字里）/ 'group'（命中不限品牌的
    词组）/ 'scoped'（命中只属于这件货品牌的词组——这也说明了品牌）。"""
    how = None
    for n in facts['pattern_groups']:
        g = rules['patterns'][n]
        if any(_term_in(text, term) for term in g['terms']):
            if g['brands']:
                return 'scoped'
            how = 'group'
    p = facts['pattern']
    if not how and p and (len(p) >= 4 or (len(p) >= 2 and not _LATIN.search(p))) and _term_in(text, p):
        how = 'name'
    return how


def _pattern_hit(text, facts, rules):
    return _pattern_how(text, facts, rules) is not None


def _form_groups(text, rules):
    return _groups_in((text,), rules['forms'])


def _strs(value):
    if isinstance(value, str):
        return [value] if value.strip() else []
    if isinstance(value, list):
        return [v for v in value if isinstance(v, str) and v.strip()]
    return []


def _cut(text):
    text = ' '.join(text.split())
    return text if len(text) <= MATCH_REASON_MAX else text[:MATCH_REASON_MAX] + '…'


def _need_detail(need, facts, rules):
    """一条需求与这件货：不对上返回 None；对上返回 (理由文字, 对上的维度, 需求说到的维度数)。
    维度为 品牌 / 图案 / 器型；门槛：品牌必须对上，写了图案须对上，写了器型不能与货的器型冲突。"""
    if not isinstance(need, dict) or need.get('status') != 'open':
        return None
    makers = _strs(need.get('maker'))
    pats = _strs(need.get('pattern_names_raw'))
    desc = _strs(need.get('additional_description'))
    filters = _strs(need.get('object_filters'))
    texts = pats + desc + filters
    words = _norm(' '.join(texts))
    if makers:
        if not any(_brand_hit(_norm(m), facts['brand'], rules) for m in makers):
            return None
    elif not _brand_hit(words, facts['brand'], rules):
        return None
    pat_ok = bool(pats) and any(_pattern_hit(_norm(p), facts, rules) for p in pats)
    if pats and not pat_ok:
        return None
    want = _form_groups(words, rules)
    if want and facts['forms'] and not (want & facts['forms']):
        return None
    form_ok = bool(want & facts['forms'])
    # 只说了品牌（"想要梅森的东西"）不够：须再对上图案或器型（detail_required 含 need 时）。
    if 'need' in rules['detail_required'] and not (pat_ok or form_ok):
        return None
    dims = ['品牌'] + (['图案'] if pat_ok else []) + (['器型'] if form_ok else [])
    named = 1 + bool(pats) + bool(want)
    return _cut(desc[0] if desc else '、'.join(pats + makers) or '、'.join(filters) or '需求'), dims, named


def _need_hit(need, facts, rules):
    """一条需求是否对上这件货；对上时返回理由文字（不含分级前缀）。"""
    d = _need_detail(need, facts, rules)
    return d[0] if d else None


def _when(value):
    """GoodCustomer 的时间字段 → 日期（取时间自身时区的日期）；解析不了返回 None。"""
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text.endswith(('Z', 'z')):
        text = text[:-1] + '+00:00'
    try:
        return datetime.datetime.fromisoformat(text).date()
    except ValueError:
        return None


def berlin_today():
    """今天（Europe/Berlin 日期，与主机时区无关）。"""
    from zoneinfo import ZoneInfo
    return datetime.datetime.now(ZoneInfo('Europe/Berlin')).date()


def need_recent(need, rules, today=None):
    """需求是否在近期内（matching.yaml 的 need_recent_days）：last_seen_at，缺或解析不了用 first_seen_at；
    都解析不了的当作不在近期。today：datetime.date，缺省为 Europe/Berlin 的今天。"""
    if not isinstance(need, dict):
        return False
    today = today or berlin_today()
    seen = _when(need.get('last_seen_at')) or _when(need.get('first_seen_at'))
    return seen is not None and seen >= today - datetime.timedelta(days=rules['need_recent_days'])


def need_match(need, facts, rules, today):
    """04 匹配（故事 29）：一条近期 open 的需求与一件货。不匹配返回 None；匹配返回
    dict(dims=[对上的 品牌/图案/器型], named=需求说到的维度数, text=需求一句)。门槛与 _need_hit 相同。"""
    if not facts.get('brand') or not need_recent(need, rules, today):
        return None
    d = _need_detail(need, facts, rules)
    if not d:
        return None
    return dict(dims=d[1], named=d[2], text=d[0])


def best_need_match(facts, needs, prefs, rules, today):
    """一位客人与一件货在 04 的匹配：只看近期 open 的需求（喜好不构成匹配；不喜欢的维度对上仍排除，与 match_customer
    一致）。几条需求都对上时取一致性最高的（对上项数多、全部对上的在前，同等取先出现的）。
    不匹配返回 None；匹配返回 need_match 的结果加 need（那条需求的原行）。"""
    if not facts.get('brand'):
        return None
    prefs = [p for p in prefs if isinstance(p, dict) and p.get('status') == 'active']
    if customer_dislikes(facts, prefs, rules):
        return None
    best = None
    for n in needs:
        m = need_match(n, facts, rules, today)
        if m and (best is None or consistency_key(m) < consistency_key(best)):
            best = dict(m, need=n)
    return best


def consistency_key(m):
    """一致性排序键（小者在前）：对上项数多的在前；同项数时需求说到的全部对上的在前。"""
    return (-len(m['dims']), len(m['dims']) < m['named'])


def _pref_dim(pref, rules):
    if not isinstance(pref, dict):
        return None
    dim = pref.get('dimension')
    return rules['dim_of'].get(dim) if isinstance(dim, str) else None


def _pref_text(pref):
    """喜好的文字：value，value 为空才用 value_raw（比较与理由同一口径）。"""
    return (_strs(pref.get('value')) or _strs(pref.get('value_raw')) or [''])[0]


def _source(pref):
    src = pref.get('source_type')
    return src if isinstance(src, str) else None


def customer_dislikes(facts, prefs, rules):
    """任一 status=active、polarity=dislike 的喜好对上这件货的同一维度（品牌 / 图案 / 器型）即 True。"""
    if not facts.get('brand'):
        return False
    for p in prefs:
        if not isinstance(p, dict) or p.get('status') != 'active' or p.get('polarity') != 'dislike':
            continue
        dim = _pref_dim(p, rules)
        if not dim:
            continue
        text = _norm(_pref_text(p))
        hit = (_brand_hit(text, facts['brand'], rules, negate=False) if dim == 'brand'
               else _pattern_hit(text, facts, rules) if dim == 'pattern'
               else bool(_form_groups(text, rules) & facts['forms']))
        if hit:
            return True
    return False


def match_customer(facts, needs, prefs, rules):
    """一位客人与一件货：不匹配返回 None；匹配返回 dict(grade, label, weight, reasons)。
    facts：match_facts 的结果；needs / prefs：该客人 needs.jsonl / preferences.jsonl 的行（dict；坏行忽略）。"""
    if not facts.get('brand'):
        return None
    prefs = [p for p in prefs if isinstance(p, dict) and p.get('status') == 'active']
    if customer_dislikes(facts, prefs, rules):
        return None
    grades = rules['grades']
    wanted = [r for r in (_need_hit(n, facts, rules) for n in needs) if r]
    if wanted:
        g = grades['need']
        return dict(grade='need', label=g['label'], weight=g['weight'],
                    reasons=[g['label'] + '：' + r for r in dict.fromkeys(wanted)][:3])
    hits, brand_seen, explicit_brand, detail = [], False, False, False
    for p in prefs:
        dim = _pref_dim(p, rules)
        if not dim or p.get('polarity') != 'like':
            continue
        text = _norm(_pref_text(p))
        names_brand = _brand_hit(text, facts['brand'], rules)
        if dim == 'brand':
            ok = names_brand
        elif dim == 'pattern':
            how = _pattern_how(text, facts, rules)
            ok = how is not None
            names_brand = names_brand or how == 'scoped'
        else:
            ok = bool(_form_groups(text, rules) & facts['forms'])
        if not ok:
            continue
        hits.append(p)
        detail = detail or dim in ('pattern', 'form')
        # 品牌须由喜好说出；"明说喜欢"只看明说的品牌 / 只属于这个品牌的图案，只对上器型的明说不算。
        brand_seen = brand_seen or names_brand
        if names_brand and dim in ('brand', 'pattern') and _source(p) == 'explicit':
            explicit_brand = True
    if not hits or not brand_seen:
        return None
    grade = 'explicit' if explicit_brand else 'inferred'
    if grade in rules['detail_required'] and not detail:
        return None
    g = grades[grade]
    shown = []
    for p in hits:
        value = _pref_text(p) or '?'
        src = _source(p)
        if src != 'explicit':
            value += '（' + rules['sources'].get(src, src or '未注明来源') + '）'
        shown.append(_cut(value))
    shown = list(dict.fromkeys(shown))
    text = '、'.join(shown[:MATCH_REASON_VALUES]) + ('等' if len(shown) > MATCH_REASON_VALUES else '')
    return dict(grade=grade, label=g['label'], weight=g['weight'], reasons=[g['label'] + '：' + text])


# ================================================================ 千人千面商品池（CAP-12，FR-24，AD-11；故事 18）
# 每位已关联客户对商品池每件货打分，Mac 发布时算好货号顺序随快照下发；服务器只照顺序排，不做任何计算。
# 参数是数据（config/personalize.yaml，经 curio/config.personalize_rules 读取校验）；打分只在这里，不调用 AI。
#   分数 = 匹配分级权重（match_customer：明说想要 > 明说喜欢 > 推算口味，权重取 matching.yaml）
#        + 浏览信号：该客户各账号在账本里的 item_open（每次 open 分）与 item_dwell（每秒 dwell_per_second 分），
#          按事件时间半衰期衰减，乘以被浏览那件与这件货的相近度（同品牌 / 同图案 / 同器型各自的权重相加）；
#          被浏览的那件本身不因自己被浏览加分（避免反复推同一件）。
#   沉底：不喜欢的维度对上（customer_dislikes），或墨墨在 04 标了这位客户"不合适"的货 → 排到末尾（不移出）。
#   同分保持墨墨顺序（稳定排序）。


def browse_affinity(facts, seen, weights):
    """被浏览那件（seen，match_facts 结果）与这件货（facts）的相近度：同品牌 / 同图案 / 同器型的权重之和。"""
    score = 0.0
    if facts.get('brand') and facts.get('brand') == seen.get('brand'):
        score += weights['brand']
    same_pattern = (facts.get('pattern_id') and facts.get('pattern_id') == seen.get('pattern_id')) or \
        bool(set(facts.get('pattern_groups') or ()) & set(seen.get('pattern_groups') or ()))
    if same_pattern:
        score += weights['pattern']
    if set(facts.get('forms') or ()) & set(seen.get('forms') or ()):
        score += weights['form']
    return score


def browse_signal(kind, amount, age_days, params):
    """一条浏览事件的信号强度（已按时间衰减）：item_open 一次 open 分；item_dwell 每秒 dwell_per_second 分。"""
    if kind == 'item_open':
        base = params['open']
    elif kind == 'item_dwell':
        base = params['dwell_per_second'] * max(0, min(amount or 0, params['dwell_cap_seconds']))
    else:
        return 0.0
    return base * 0.5 ** (max(0.0, age_days) / params['half_life_days'])


def personal_scores(pool, needs, prefs, seen, rejected, rules, params):
    """一位客户对池中每件货的排序键。
    pool：[(货号, facts)]；needs / prefs：GoodCustomer 需求与喜好行；
    seen：[(货号, facts, 事件类型, 数量（停留秒数，点开为 None）, 距今天数)]，该客户各账号的浏览；
    rejected：墨墨对这位客户标"不合适"的货号集合；rules：matching 词表；params：personalize 参数。
    浏览加分每件趋近而不超过 params['max_score']（小于分级权重的最小级差，明说想要 > 明说喜欢 > 推算口味 > 无匹配
    始终成立）。
    返回 ({货号: (沉底, 分数)}, 需求/喜好是否对上池中至少一件)。"""
    prefs = [p for p in prefs if isinstance(p, dict) and p.get('status') == 'active']
    totals = {}                     # 被浏览的货号 → [facts, 信号合计]（先按货号合并，打分只与浏览过的件数成正比）
    for code, f, kind, amount, age in seen:
        s = browse_signal(kind, amount, age, params)
        if s > 0:
            totals.setdefault(code, [f, 0.0])[1] += s
    signals = [(code, f, s) for code, (f, s) in totals.items()]
    out, matched = {}, False
    for code, facts in pool:
        disliked = customer_dislikes(facts, prefs, rules)
        m = None if disliked else match_customer(facts, needs, prefs, rules)
        matched = matched or disliked or m is not None
        browse = sum(s * browse_affinity(facts, f, params['same']) for c, f, s in signals if c != code)
        # 浏览加分 = max_score × b / (b + 1)：随信号单调增加、趋近而不超过 max_score（不会把不同信号压成同分）。
        score = (float(m['weight']) if m else 0.0) + params['max_score'] * browse / (browse + 1.0)
        out[code] = (bool(disliked or code in rejected), round(score, 9))
    return out, matched


def order_by_scores(codes, scores):
    """按 personal_scores 的排序键排货号（codes 为墨墨顺序）：沉底的在后，分高的在前，同分保持墨墨顺序；
    scores 里没有的货（算分之后才进商品池）按 0 分、不沉底。"""
    keyed = [((scores.get(c, (False, 0.0))[0], -scores.get(c, (False, 0.0))[1], i), c) for i, c in enumerate(codes)]
    return [c for _, c in sorted(keyed)]


def personal_order(pool, needs, prefs, seen, rejected, rules, params):
    """一位客户的商品池顺序（personal_scores + order_by_scores）。返回 (货号顺序, 需求/喜好是否对上池中至少一件)。"""
    scores, matched = personal_scores(pool, needs, prefs, seen, rejected, rules, params)
    return order_by_scores([c for c, _ in pool], scores), matched


# ---------------------------------------------------------------- 综合评分（故事 27，方案 C；AD-15）
# 纯函数：不读库、不读文件、不调用 AI。参数见 config/scoring.yaml（config.scoring_rules），
# 读库与写 composite_scores 在 curio/scoring.py；对外入口 curio/rules.composite_scores。
#
# 一件货（dict）用到的字段：item_id、brand、pattern_id、function_id（标准名称）、listed_price、currency、
# label（墨墨人工留下 1 / 去掉或「看过没选」0 / 无 None）、ai_score（source=ai 的旧分数，否则 None）。

import bisect as _bisect
import math as _math
import statistics as _statistics

_TASTE_LEVELS = (('b', ('brand',)), ('bf', ('brand', 'function_id')), ('bfp', ('brand', 'function_id', 'pattern_id')))


def _key(it, kind, fields):
    vals = tuple(it.get(f) for f in fields)
    return (kind,) + vals if all(vals) else None


def eur_price(it):
    """欧元标价；非欧元、无价或非正数为 None。"""
    p = it.get('listed_price')
    if (it.get('currency') or '').upper() != 'EUR' or p is None or isinstance(p, bool):
        return None
    try:
        p = float(p)
    except (TypeError, ValueError):
        return None
    return p if p > 0 and _math.isfinite(p) else None


def price_tier(it, edges):
    """欧元标价所在价位档（第一个大于标价的上界；超过最后一档为 inf）；无欧元价 None。"""
    p = eur_price(it)
    if p is None:
        return None
    for e in edges:
        if p < e:
            return e
    return float('inf')


def taste_counts(items, params):
    """留下率统计：({分组键: [留下数, 有标签件数]}, 全局留下率)。分组：品牌、品牌+器型、品牌+器型+图案、品牌+价位。"""
    cnt = {}
    labelled = [it for it in items if it.get('label') in (0, 1)]
    for it in labelled:
        keys = [_key(it, kind, fields) for kind, fields in _TASTE_LEVELS]
        tier = price_tier(it, params['price_edges'])
        keys.append(('pb', it.get('brand'), tier) if it.get('brand') and tier is not None else None)
        for k in keys:
            if k is not None:
                c = cnt.setdefault(k, [0, 0])
                c[0] += it['label']
                c[1] += 1
    cnt[('all',)] = [sum(it['label'] for it in labelled), len(labelled)]     # 全局（留一时据此去掉自己）
    g = cnt[('all',)][0] / len(labelled) if labelled else 0.0
    return cnt, g


def taste(it, cnt, g, params):
    """墨墨口味（原始留下率）与理由依据：(rate, dict(level, keys, kept, total))。
    分层平滑：从全局留下率起，依次向品牌、品牌+器型、品牌+器型+图案收缩（强度 m = smoothing），
    下一层没有记录就停；再乘以 √(品牌+价位留下率 ÷ 全局留下率)（价位层同样平滑到全局）。
    这件货自己的标签不计入（留一）。level 为用到的最深一层（kind 与键），没有可用记录时 None。"""
    m = params['smoothing']
    y = it.get('label') if it.get('label') in (0, 1) else None
    if y is not None and ('all',) in cnt:        # 留一：全局留下率也不含自己
        a, n = cnt[('all',)]
        g = (a - y) / (n - 1) if n > 1 else 0.0
    rate, used = g, None
    for kind, fields in _TASTE_LEVELS:
        k = _key(it, kind, fields)
        if k is None or k not in cnt:
            break
        a, n = cnt[k]
        if y is not None:
            a, n = a - y, n - 1
        if n <= 0:
            break
        rate = (a + m * rate) / (n + m)
        used = (k, a, n)
    tier = price_tier(it, params['price_edges'])
    pk = ('pb', it.get('brand'), tier) if it.get('brand') and tier is not None else None
    if pk is not None and pk in cnt and g > 0:
        a, n = cnt[pk]
        if y is not None:
            a, n = a - y, n - 1
        if n > 0:
            rate *= ((a + m * g) / (n + m) / g) ** 0.5
    return rate, (dict(key=used[0], kept=used[1], total=used[2]) if used else None)


def peer_medians(items, params):
    """同类挂牌价中位数：{('bfp'|'bf', …): 中位数}，只留件数 ≥ min_peers 的组（只算有品牌、有欧元标价的货）。"""
    groups = {}
    for it in items:
        p = eur_price(it)
        if p is None or not it.get('brand'):
            continue
        for kind, fields in (('bfp', ('brand', 'function_id', 'pattern_id')), ('bf', ('brand', 'function_id'))):
            k = _key(it, kind, fields)
            if k is not None:
                groups.setdefault(k, []).append(p)
    return {k: _statistics.median(v) for k, v in groups.items() if len(v) >= params['min_peers']}


def value_ratio(it, med):
    """性价比原始值：(欧元标价 ÷ 同类中位数, 中位数, 为什么没有)。先找品牌+器型+图案，再找品牌+器型。
    算不了时比值与中位数为 None，第三项为原因（无标价 / 非欧元 / 无品牌 / 无同类）。"""
    if it.get('listed_price') is None:
        return None, None, '无标价'
    p = eur_price(it)
    if p is None:
        return None, None, '非欧元标价' if (it.get('currency') or '').upper() != 'EUR' else '无标价'
    if not it.get('brand'):
        return None, None, '无品牌'
    for kind, fields in (('bfp', ('brand', 'function_id', 'pattern_id')), ('bf', ('brand', 'function_id'))):
        k = _key(it, kind, fields)
        if k is not None and k in med and med[k] > 0:
            return p / med[k], med[k], None
    return None, None, '无同类'


def gc_order_facts(name, maker_guess, aliases, patterns, functions):
    """一条 GoodCustomer 订单的（品牌, 图案 ID, 器型 ID）：品牌按中文品名包含品牌别名（aliases：[(小写别名, 品牌)]，按顺序），
    没有再用 maker_guess；图案 / 器型按中文品名包含知识库中文名（patterns / functions：[(中文名, ID)]，长的在前）。"""
    name = name or ''
    low = name.lower()
    brand = next((b for a, b in aliases if a in low), None)
    if not brand and maker_guess:
        brand = dict(aliases).get(str(maker_guess).lower(), maker_guess)
    pat = next((i for zh, i in patterns if zh in name), None)
    fun = next((i for zh, i in functions if zh in name), None)
    return brand, pat, fun


def gc_demand(orders, aliases, patterns, functions, today, params):
    """客群成交量（按成交日期衰减，每 half_life_days 天减半；不含退款）：{('b', 品牌) / ('bp', 品牌, 图案) / ('bf', 品牌, 器型): 量}。
    orders：GoodCustomer order_items 行（dict）；today：date；日期读不了的按 365 天前算。"""
    from datetime import date as _date
    dem = {}
    for o in orders:
        if not isinstance(o, dict) or o.get('refunded'):
            continue
        b, p, f = gc_order_facts(o.get('product_name'), o.get('maker_guess'), aliases, patterns, functions)
        try:
            age = max(0, (today - _date.fromisoformat(str(o.get('session_date'))[:10])).days)
        except (TypeError, ValueError):
            age = 365
        w = 0.5 ** (age / params['half_life_days'])
        if b:
            dem[('b', b)] = dem.get(('b', b), 0.0) + w
            if p:
                dem[('bp', b, p)] = dem.get(('bp', b, p), 0.0) + w
            if f:
                dem[('bf', b, f)] = dem.get(('bf', b, f), 0.0) + w
    return dem


def trend(it, dem, params):
    """客群趋势原始值：ln(1+同品牌) + pattern × ln(1+同品牌+图案) + function × ln(1+同品牌+器型)；无品牌为 0。"""
    b = it.get('brand')
    if not b:
        return 0.0
    tw = params['trend_weights']
    return (tw['brand'] * _math.log1p(dem.get(('b', b), 0.0))
            + tw['pattern'] * _math.log1p(dem.get(('bp', b, it.get('pattern_id')), 0.0))
            + tw['function'] * _math.log1p(dem.get(('bf', b, it.get('function_id')), 0.0)))


def percentiles(values):
    """{键: 原始值或 None} → {键: 0–100 百分位}：在全部非 None 值里的位置（同值取最低名次）；None 为 50。"""
    known = sorted(v for v in values.values() if v is not None)
    if len(set(known)) < 2:          # 没有可区分的值（如还没有标签、只有一个 AI 分）：一律 50
        return {k: 50.0 for k in values}
    span = max(1, len(known) - 1)
    return {k: 50.0 if v is None else 100.0 * _bisect.bisect_left(known, v) / span for k, v in values.items()}


def absolute_parts(raw_t, raw_v, raw_r, params):
    """绝对刻度（absolute = 1）：成分不再是"在全部货里排第几"，而是本身的大小，各 0–100。
    口味 = 平滑留下率 × 100（留下 1/4 左右约 25）；性价比 = 100 ÷ (1 + 标价/同类中位)（同价 50，越便宜越高）；
    客群 = 100 × (1 − e^(−趋势原始值 ÷ trend_scale))（成交越多越接近 100）。缺值为 50。"""
    k = params['trend_scale']
    pt = {i: min(100.0, 100.0 * v) for i, v in raw_t.items()}
    pv = {i: 50.0 if v is None else 100.0 / (1.0 + _math.exp(-v)) for i, v in raw_v.items()}
    pr = {i: 50.0 if v is None else 100.0 * (1.0 - _math.exp(-v / k)) for i, v in raw_r.items()}
    return pt, pv, pr


def customer_weights(n, params):
    """客人喜好的权重随数据量增长：w = n ÷ (n + k)；返回 (口味权重, 客人权重) = (momo − max·w, max·w)。
    n 为这位客人的数据量（条数），≤ 0 时客人权重为 0。"""
    n = max(0.0, float(n or 0))
    w = n / (n + params['customer_k'])
    return params['weights']['momo'] - params['customer_max'] * w, params['customer_max'] * w


def composite_total(parts, params, momo_weight=None, customer_weight=None):
    """合成：(口味·wm + 性价比·(口味/100)²·wv + 趋势·wt + 客人·wc) ÷ 权重和。parts = dict(momo, value, trend, customer)。"""
    w = dict(params['weights'])
    if momo_weight is not None:
        w['momo'] = momo_weight
    if customer_weight is not None:
        w['customer'] = customer_weight
    total = sum(w.values())
    gated = parts['value'] * (parts['momo'] / 100.0) ** 2
    return (w['momo'] * parts['momo'] + w['value'] * gated + w['trend'] * parts['trend']
            + w['customer'] * parts['customer']) / total if total else 0.0


def _group_label(key, names):
    """分组键 → "梅森·洋葱·盘"（品牌·图案·器型）。names：dict(brand, pattern, function) 三个 {ID: 中文}。"""
    kind, brand = key[0], key[1]
    parts = [names.get('brand', {}).get(brand) or brand]
    if kind == 'bfp':
        parts.append(names.get('pattern', {}).get(key[3]) or key[3])
    if kind in ('bf', 'bfp'):
        parts.append(names.get('function', {}).get(key[2]) or key[2])
    return '·'.join(parts)


def _euro(v):
    return '€%d' % round(v) if v >= 10 else '€%.2f' % v


def composite_reason(parts, why, names, demand_ok=True, weights=None):
    """一句中文理由，如 "口味 82（梅森·洋葱·盘：你留下 6/20）· 性价比 40（同类中位 €80）· 客群 75"。
    weights 里权重为 0 的性价比不写进理由（2026-11-04 起性价比暂停用）。"""
    t = why.get('taste')
    if t:
        momo = '%s：你留下 %d/%d' % (_group_label(t['key'], names), t['kept'], t['total'])
    elif why.get('brand'):
        momo = '%s：还没有你的记录' % (names.get('brand', {}).get(why['brand']) or why['brand'])
    else:
        momo = '无品牌，按全局留下率'
    if why.get('ai') is not None:
        momo += '；AI 分 %d' % round(why['ai'])
    value = '同类中位 ' + _euro(why['median']) if why.get('median') else why.get('value_missing') or '无同类'
    text = '口味 %d（%s）' % (round(parts['momo']), momo)
    if not weights or weights.get('value'):
        text += '· 性价比 %d（%s）' % (round(parts['value']), value)
    text += '· 客群 %d' % round(parts['trend'])
    if not demand_ok:
        text += '（客群数据缺）'
    return text


def composite_scores(items, demand, names, params):
    """方案 C 综合评分。items 见上；demand 为 gc_demand 结果，None 表示客群数据读不了（趋势全为 50）；
    names 为理由用的中文名 dict(brand, pattern, function)。absolute = 0 时成分为本次全部货上的百分位，
    absolute = 1 时为绝对刻度（见 absolute_parts）。
    返回 {item_id: dict(score, momo, value, trend, customer, reason)}（分数与成分都是 0–100 的浮点数）。"""
    items = list(items)
    if not items:
        return {}
    cnt, g = taste_counts(items, params)
    med = peer_medians(items, params)
    raw_t, why_t, raw_v, why_v, raw_r = {}, {}, {}, {}, {}
    for it in items:
        iid = it['item_id']
        raw_t[iid], why_t[iid] = taste(it, cnt, g, params)
        ratio, m, missing = value_ratio(it, med)
        raw_v[iid] = None if ratio is None or ratio <= 0 else -_math.log(ratio)
        why_v[iid] = (m, missing)
        raw_r[iid] = None if demand is None else trend(it, demand, params)
    if params.get('absolute'):
        pt, pv, pr = absolute_parts(raw_t, raw_v, raw_r, params)
        ai = {it['item_id']: float(it['ai_score']) for it in items if it.get('ai_score') is not None}
    else:
        pt, pv, pr = percentiles(raw_t), percentiles(raw_v), percentiles(raw_r)
        ai = percentiles({it['item_id']: it.get('ai_score') for it in items if it.get('ai_score') is not None})
    share = params['ai_share']
    out = {}
    for it in items:
        iid = it['item_id']
        momo = pt[iid] if iid not in ai else (1 - share) * pt[iid] + share * ai[iid]
        parts = dict(momo=momo, value=pv[iid], trend=pr[iid], customer=0.0)
        why = dict(taste=why_t[iid], brand=it.get('brand'), median=why_v[iid][0], value_missing=why_v[iid][1],
                   ai=it.get('ai_score') if iid in ai else None)
        out[iid] = dict(parts, score=composite_total(parts, params),
                        reason=composite_reason(parts, why, names, demand is not None, params['weights']))
    return out


# ---------------------------------------------------------------- 曝光衰减（故事 28；AD-15）
# 时间不进综合分：按发布历史累计"在已发布 R12 上挂了几天"，换算成排序扣分。参数在 config/scoring.yaml 的 exposure 段。

def exposure_days(publishes, now):
    """累计挂牌天数 {货号: 天}。publishes：[(发布时刻 datetime（带时区）, [货号…])]（非 dropped、已按发布顺序排好）；
    now：带时区的当前时刻，其时区即计日的时区（柏林）。每次发布从发布时刻挂到下一次发布（最后一次挂到 now），
    这一段计"结束日期 − 开始日期"天（按 now 的时区取日期）；连续上榜的各段首尾相接，合起来就是首尾日期之差，
    下榜后再上榜另起一段累加。下榜后不扣回。"""
    tz = now.tzinfo
    out = {}
    pubs = sorted(publishes, key=lambda p: p[0])
    for k, (at, codes) in enumerate(pubs):
        end = pubs[k + 1][0] if k + 1 < len(pubs) else now
        if end <= at:
            continue
        days = (end.astimezone(tz).date() - at.astimezone(tz).date()).days
        for code in dict.fromkeys(codes or []):
            out[code] = out.get(code, 0) + days
    return {code: d for code, d in out.items() if d > 0}


def exposure_penalty(days, params):
    """曝光扣分：每天 per_day 分，最多 max 分。params 为 scoring.yaml 的 exposure 段。"""
    return min(max(days or 0, 0) * params['per_day'], params['max'])


def exposure_sort_key(score, penalty):
    """排序键（大者在前）：综合分 − 曝光扣分；没有分数时 None（排在最后）。"""
    return None if score is None else score - (penalty or 0)
