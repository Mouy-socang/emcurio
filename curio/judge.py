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
    # 你确认的：纯白瓷 / 素白塑像（注意 "weiß / vergoldet" 这种带金边的不算纯白）
    (r"blanc\s*de\s*chine|biscuitporzellan|\bbiscuit\b|\bbiskuit\b|"
     r"wei[sß]{1,2}e[sn]?\s+porzellan|wei[sß]{1,2}porzellan|wei[sß]{1,2}ware|pure\s+white\s+porcelain|"
     r"ohne\s+dekor|undekoriert|ohne\s+malerei",
     "纯白瓷/无装饰白瓷"),
    (r"wei[sß]{1,2}e[sn]?\s+(figur|gruppe|putto|madonna)", "素白塑像"),
    # 你确认的：年度纪念盘 / 收藏盘
    (r"jahres(teller|tasse|becher|glocke|kugel|plakette)|sammelteller|weihnachts(teller|jahr|glocke|kugel)|"
     r"club\s*edition|wandteller\s+jahr",
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


# 标题里单独写 "weiß/weiss/white" 也当白瓷；但带金/彩绘/蓝白（如 "weiß / vergoldet"、"blau-weiß"）不算
_WHITE_WORD = re.compile(r"\b(wei[sß]{1,2}|white)\b", re.I)
_NOT_PLAIN = re.compile(r"gold|vergoldet|blau|blue|bunt|dekor|malerei|bemalt|kobalt|rose|blume|indisch|zwiebel", re.I)

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


def standard_name(title, description='', config=None):
    """据卖家标题/描述生成候选标准名称（不调用 AI，不影响判断结论）。

    返回 dict：brand / brand_display、pattern_id / pattern、form_id / form、function_id / function、
    era、seller_grade（卖家原文，不入名）、std_name（无器型且无功能时为 None）、
    std_key（品牌|图案ID|器型ID|功能ID，缺值写 -）、status（named / insufficient）、kb_status、kb_fingerprint。
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
    parts = [era, brand_display, pattern and pattern[1], form and form[1], function and function[1]]
    return dict(
        brand=brand, brand_display=brand_display,
        pattern_id=pattern and pattern[0], pattern=pattern and pattern[1],
        form_id=form and form[0], form=form and form[1],
        function_id=function and function[0], function=function and function[1],
        era=era, seller_grade=grade,
        std_name=' '.join(p for p in parts if p) if named else None,
        std_key='|'.join(x or '-' for x in (brand, pattern and pattern[0], form and form[0], function and function[0])),
        status='named' if named else 'insufficient',
        kb_status=kb_status, kb_fingerprint=fp)
