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
