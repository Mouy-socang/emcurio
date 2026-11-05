"""故事 32：拍卖行自己的网站——名单（config/settings.yaml 的 auction_houses）上 status=allowed、enabled、有适配器的，
每柏林日最多看一次，找 window_days（90）天内开拍的场次，只读瓷器分类的列表页（Lempertz 读一个目录 JSON），
把拍品以纯拍卖身份（platform auction_house、buying AUCTION）入库，之后与 eBay 拍卖走同一套。

礼貌规则（Fetcher）：User-Agent 如实写 CurioObjectBot/0.1（可附 contact），不伪装浏览器；每次先读 robots.txt，禁止的路径不访问；
同一主机两次请求至少隔 interval 秒（名单里配置；robots 的 Crawl-delay 更长时按它）；每家每次最多 MAX_REQUESTS 个请求；
遇到 403、429 或验证页面，这家当天停下并记错，不重试、不绕过。不打开拍品页、不下载 PDF 图录与图片（入池存档的主图除外，见 vision）。

每个适配器分成纯解析函数（输入 HTML / JSON 文本，测试直接用样本）与取数函数（经 Fetcher）。
各家状态写 kv ah_status（JSON：key → {date, at, new, found, error, note}），后台拍卖新品顶部显示。"""
import html
import json
import logging
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib import robotparser
from urllib.parse import urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests

from .config import ROOT

log = logging.getLogger(__name__)
BERLIN = ZoneInfo('Europe/Berlin')
BOT = 'CurioObjectBot/0.1'
PLATFORM = 'auction_house'
MAX_REQUESTS = 40
MAX_REDIRECTS = 3
TIMEOUT = 30
STATUS_KEY = 'ah_status'
# 只认验证页本身的标记（普通页面里的 reCAPTCHA 表单、文字不算）。
BLOCK_RE = re.compile(r'cf-chl|challenge-platform|verify you are human|<title>\s*Just a moment|<title>\s*Attention Required', re.I)
MAX_DELAY = 60            # robots Crawl-delay 的上限（秒）
RUN_SECONDS = 20 * 60     # run_due 每轮的总时限：超过后其余拍卖行本轮不开始、不记为今天已看
PARSE_FAILED = '解析不到拍品，可能改版'


# ---------- 礼貌访问 ----------

class Stop(Exception):
    """这家今天停下（被拦、robots 禁止、到请求上限）。信息为中文，写进状态。"""


class Blocked(Stop):
    pass


class RobotsDenied(Stop):
    pass


class LimitReached(Stop):
    pass


class FetchError(Exception):
    """一次请求没拿到可用的页面（网络错误、非 200）。"""


def user_agent(contact=None, root=ROOT):
    """如实的 User-Agent：CurioObjectBot/0.1，contact 非空时附「(+contact)」。contact 为 None 时读 root 的 settings.yaml。"""
    if contact is None:
        try:
            from .config import auction_house_settings
            contact = auction_house_settings(root)['bot']['contact']
        except Exception:
            contact = ''
    return BOT + (' (+' + contact + ')' if contact else '')


class Fetcher:
    """一家拍卖行一次运行用的取数器。session 只需 request(method, url, **kw)。"""

    def __init__(self, session, ua=BOT, interval=5.0, max_requests=MAX_REQUESTS, sleep=time.sleep, monotonic=time.monotonic):
        self.session, self.ua, self.interval, self.max_requests = session, ua, float(interval), max_requests
        self.sleep, self.monotonic = sleep, monotonic
        self.requests = 0
        self.last = {}         # 主机 → 上次请求的 monotonic 时间
        self.delay = {}        # 主机 → 最短间隔（interval 与 robots Crawl-delay 取大）
        self.robots = {}       # origin → RobotFileParser

    def get(self, url):
        return self._send('GET', url)

    def post_json(self, url, payload):
        return self._send('POST', url, json=payload)

    def _rules(self, url):
        parts = urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self.robots:
            rp = robotparser.RobotFileParser(origin + '/robots.txt')
            target = origin + '/robots.txt'
            for _ in range(MAX_REDIRECTS + 1):
                status, text, location = self._raw('GET', target, with_location=True)
                if status in (301, 302, 303, 307, 308) and location:
                    target = urljoin(target, location)
                    hop = urlsplit(target)
                    if hop.scheme == 'http' and hop.netloc == parts.netloc:
                        target = hop._replace(scheme='https').geturl()   # 同站 robots.txt 跳到 http（如 Mehlis）：改回 https 再读
                    elif hop.scheme != 'https':
                        raise FetchError('跳转到非 https 地址')
                    continue
                break
            else:
                raise FetchError('跳转过多')
            if status == 200:
                rp.parse(text.splitlines())
            elif 400 <= status < 500:
                rp.allow_all = True           # 没有 robots.txt（404 等）：按惯例视为不限制
            else:
                raise FetchError('robots.txt 读取失败（HTTP %d）' % status)
            self.robots[origin] = rp
            try:
                crawl = rp.crawl_delay(self.ua)
            except Exception:
                crawl = None
            self.delay[parts.hostname] = max(self.interval, min(float(crawl or 0), MAX_DELAY))
        return self.robots[origin]

    def _send(self, method, url, **kw):
        for _ in range(MAX_REDIRECTS + 1):
            if not self._rules(url).can_fetch(self.ua, url):
                path = urlsplit(url).path or '/'
                raise RobotsDenied('robots 禁止：' + path)
            status, text, location = self._raw(method, url, with_location=True, **kw)
            if status in (301, 302, 303, 307, 308) and location:
                url = urljoin(url, location)
                if urlsplit(url).scheme != 'https':
                    raise FetchError('跳转到非 https 地址')
                if status in (301, 302, 303) and method == 'POST':
                    method, kw = 'GET', {}
                continue
            if status != 200:
                raise FetchError('HTTP %d：%s' % (status, urlsplit(url).path))
            return text
        raise FetchError('跳转过多')

    def _raw(self, method, url, with_location=False, **kw):
        if self.requests >= self.max_requests:
            raise LimitReached('已到每次 %d 个请求的上限，其余明天再看' % self.max_requests)
        host = urlsplit(url).hostname
        gap = self.delay.get(host, self.interval)
        if host in self.last:
            wait = gap - (self.monotonic() - self.last[host])
            if wait > 0:
                self.sleep(wait)
        self.requests += 1
        try:
            r = self.session.request(method, url, headers={'User-Agent': self.ua, 'Accept-Language': 'de'},
                                     timeout=TIMEOUT, allow_redirects=False, **kw)
        except requests.RequestException:
            raise FetchError('网络请求失败') from None
        finally:
            self.last[host] = self.monotonic()
        code = r.status_code
        if code in (403, 429):
            raise Blocked('被拦（HTTP %d），今天停止' % code)
        text = r.text or ''
        if code == 200 and BLOCK_RE.search(text[:20000]) and not urlsplit(url).path.endswith('robots.txt'):
            raise Blocked('被拦（出现验证页面），今天停止')
        if with_location:
            return code, text, (r.headers or {}).get('location') or (r.headers or {}).get('Location')
        return code, text


# ---------- 通用解析 ----------

MONTHS = {'januar': 1, 'jänner': 1, 'februar': 2, 'märz': 3, 'maerz': 3, 'april': 4, 'mai': 5, 'juni': 6, 'juli': 7,
          'august': 8, 'september': 9, 'oktober': 10, 'november': 11, 'dezember': 12}
_TAG = re.compile(r'<[^>]+>')


def text_of(fragment):
    """HTML 片段 → 纯文字（去标签、反转义、合并空白）。"""
    return re.sub(r'\s+', ' ', html.unescape(_TAG.sub(' ', fragment or ''))).strip()


def german_days(text):
    """「24. | 25. | 26. | 27. Februar 2027」「19. November 2026 bis 21. November 2026」→ 日期列表（按出现顺序）。
    没写月份 / 年份的日子沿用后面最近写出的月份与年份（跨年时减一年）。读不出时空列表。"""
    tokens = re.findall(r'(\d{1,2})\.\s*([A-Za-zÄÖÜäöü]+)?\s*(\d{4})?', text_of(text))
    out, month, year = [], None, None
    for day, mon, yr in reversed(tokens):
        m = MONTHS.get((mon or '').lower())
        if yr:
            year = int(yr)
        if m:
            if month is not None and m > month and not yr and year is not None:
                year -= 1
            month = m
        if month is None or year is None:
            continue
        try:
            out.append(date(year, month, int(day)))
        except ValueError:
            continue
    return list(reversed(out))


def utc_z(local):
    """柏林当地时间（naive datetime）→ UTC「YYYY-MM-DDTHH:MM:SS.000Z」（与 eBay 的 ends 同格式）。"""
    return local.replace(tzinfo=BERLIN).astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z')


def euro_number(text):
    """「300,00」「1.200,50」→ 300.0 / 1200.5；读不出 None。"""
    m = re.search(r'\d[\d.]*(?:,\d+)?', text or '')
    if not m:
        return None
    try:
        return float(m[0].replace('.', '').replace(',', '.'))
    except ValueError:
        return None


def euro_text(value):
    """300.0 → 「300」，300.5 → 「300,50」。"""
    return ('%d' % value) if float(value).is_integer() else ('%.2f' % value).replace('.', ',')


def row(key, sale, lot, title, description, price, price_text, url, image, ends):
    return dict(item_id='ah-%s-%s-%s' % (key, sale, lot), platform=PLATFORM, title=title[:500], description=description or '',
                price=price, currency='EUR', price_text=price_text, buying='AUCTION', url=url, image=image or '',
                ends=ends or '', country='DE')


def _slug(value):
    return re.sub(r'[^0-9A-Za-z]+', '', str(value)).lower()


# ---------- Wendl ----------

WENDL_TERMINE = 'https://www.auktionshauswendl.de/tcms/auktionen/termine'
WENDL_LIST = 'https://auktionen.auktionshauswendl.de/de/listpage/{sale}/{cat}/{page}?bidResultStatus=Unknown'
WENDL_PORZELLAN = 138


def wendl_sales(page):
    """termine 页：<h2><b>117. | Frühjahrs</b>auktion<br><span>24. | … Februar 2027</span></h2> → [{sale, start, end}]。"""
    out = []
    for m in re.finditer(r'<h2[^>]*>\s*<b>\s*(\d+)\.[^<]*</b>[^<]*<br\s*/?>\s*<span>(.*?)</span>', page, re.S):
        days = german_days(m[2])
        if days:
            out.append(dict(sale=int(m[1]), start=min(days), end=max(days)))
    return out


def wendl_lots(page):
    """瓷器列表页 → (拍品列表, 末页序号（从 0 起）, 卡片数)。只取 #lotContainer 里的卡片，丢掉每张卡片里隐藏的重复块；
    已有成交价（lot-zuschlag）的不取。找不到 #lotContainer（页面结构变了）时卡片数为 None。"""
    last = 0
    m = re.search(r'href="[^"]*/listpage/\d+/\d+/(\d+)[^"]*"\s+class="last-page"', page)
    if m:
        last = int(m[1])
    start = page.find('id="lotContainer"')
    if start < 0:
        return [], last, None
    lots, seen = [], set()
    chunks = re.split(r'<div class="lot-container\b', page[start:])[1:]
    for chunk in chunks:
        chunk = chunk.split('style="display: none;"')[0]
        lid = re.search(r'value="(\d+)"\s+name="ignoreList"', chunk)
        number = re.search(r'<span class="lot-number">\s*([^<]+?)\s*</span>', chunk)
        if not lid or not number or lid[1] in seen or 'class="lot-zuschlag"' in chunk:
            continue
        seen.add(lid[1])
        title = re.search(r'<span class="lot-title">\s*(?:<div>)?(.*?)(?:</div>)?\s*</span>', chunk, re.S)
        descp = re.search(r'<div class="lot-descp">(.*?)</div>', chunk, re.S)
        limit = re.search(r'<span class="lot-limit">(.*?)</span>', chunk, re.S)
        link = re.search(r'href="(https://auktionen\.auktionshauswendl\.de/de/lot/lot-details/[^"]+)"', chunk)
        img = re.search(r'<img src="(https://cdn\.aeman\.nl/[^"]+)"', chunk)
        lots.append(dict(id=lid[1], lot=text_of(number[1]), title=text_of(title[1] if title else '').rstrip('. ').strip() or text_of(number[1]),
                         description=text_of(descp[1] if descp else ''),
                         limit=euro_number(text_of(limit[1])) if limit else None,
                         url=html.unescape(link[1]) if link else '', image=html.unescape(img[1]) if img else ''))
    return lots, last, len(chunks)


def wendl_row(sale, lot):
    ends = utc_z(datetime.combine(sale['end'], dtime(23, 59)))
    price = lot['limit']
    return row('wendl', sale['sale'], _slug(lot['lot']), lot['title'], lot['description'], price,
               'Limit %s €' % euro_text(price) if price is not None else '', lot['url'], lot['image'], ends)


def collect_wendl(fetcher, today, window_days, out):
    sales = wendl_sales(fetcher.get(WENDL_TERMINE))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found = 0
    for sale in due:
        found += _pages(lambda n: wendl_lots(fetcher.get(WENDL_LIST.format(sale=sale['sale'], cat=WENDL_PORZELLAN, page=n))),
                        0, lambda lot: out.append(wendl_row(sale, lot)))
    return None if found else _empty_note(window_days)


def _pages(read, first, add):
    """逐页读一场的瓷器列表：read(页码) → (拍品, 末页, 卡片数)。只有一页卡片都没有才停（整页已成交照样往后翻）；
    末页取各页给出的最大值。第一页就找不到列表结构时 ValueError(PARSE_FAILED)。返回取到的拍品件数。"""
    page, last, found = first, first, 0
    while page <= last:
        lots, page_last, cards = read(page)
        if cards is None:
            if page == first:
                raise ValueError(PARSE_FAILED)
            break
        if not cards:
            break
        last = max(last, page_last)
        for lot in lots:
            add(lot)
        found += len(lots)
        page += 1
    return found


def _empty_note(window_days):
    return '%d 天内的场次里没有未成交的瓷器拍品' % window_days


# ---------- Mehlis ----------

MEHLIS_BASE = 'https://www.mehlis.eu'
MEHLIS_CATALOGS = MEHLIS_BASE + '/de/catalogs/'
MEHLIS_TIMELINE = MEHLIS_BASE + '/de/catalogs/timeline/'
MEHLIS_LIST = MEHLIS_BASE + '/de/catalogs/{cid}/{cat}/?page={page}'
MEHLIS_PORZELLAN = 9


def mehlis_sales(page):
    """/de/catalogs/ 的表格行「<a href="/de/catalogs/12426/">124. Auktion</a> | 19. November 2026 bis 21. November 2026 | Aktuell」
    → [{sale, cid, start, end}]。"""
    out = []
    for m in re.finditer(r'<tr>\s*<td>\s*<a href="/de/catalogs/(\d+)/">\s*(\d+)\.\s*Auktion\s*</a>\s*</td>\s*<td>(.*?)</td>', page, re.S):
        days = german_days(m[3])
        if days:
            out.append(dict(sale=int(m[2]), cid=m[1], start=min(days), end=max(days)))
    return out


def mehlis_timeline(page):
    """时间表 → (场次号, [{day, category, lo, hi, at（柏林当地 datetime）}])。每天一个 h3，表行「Porzellan | 3200 - 3425 | Beginn 10.00 Uhr」。"""
    sale = re.search(r'Zeitplan der (\d+)\.\s*Auktion', page)
    rows = []
    parts = re.split(r'<h3[^>]*>(.*?)</h3>', page, flags=re.S)
    for heading, body in zip(parts[1::2], parts[2::2]):
        days = german_days(heading)
        if not days:
            continue
        for tr in re.findall(r'<tr>(.*?)</tr>', body, re.S):
            cells = [text_of(td) for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
            if len(cells) < 3:
                continue
            rng = re.search(r'(\d+)\s*-\s*(\d+)', cells[1])
            at = re.search(r'(\d{1,2})[.:](\d{2})\s*Uhr', cells[2])
            if not rng or not at:
                continue
            rows.append(dict(day=days[0], category=cells[0], lo=int(rng[1]), hi=int(rng[2]),
                             at=datetime.combine(days[0], dtime(int(at[1]), int(at[2])))))
    return (int(sale[1]) if sale else None), rows


def mehlis_lots(page):
    """瓷器列表页 → (拍品列表, 末页序号（从 1 起）, 卡片数)。.media 里 h4.media-heading、首个 p（描述）、
    「Katalog-Nr.: 3200<br/>Limit: 480,00 €」。找不到 .media-list（页面结构变了）时卡片数为 None。"""
    pages = [int(n) for n in re.findall(r'[?&]page=(\d+)', page)]
    if 'class="media-list"' not in page:
        return [], max(pages) if pages else 1, None
    lots = []
    chunks = re.split(r'<div class="media">', page)[1:]
    for chunk in chunks:
        number = re.search(r'Katalog-Nr\.:\s*([0-9]+[a-zA-Z]?)', chunk)
        if not number:
            continue
        head = re.search(r'<h4 class="media-heading">(.*?)</h4>', chunk, re.S)
        desc = re.search(r'</h4>\s*<p>(.*?)</p>', chunk, re.S)
        limit = re.search(r'Limit:\s*([^<]*)', chunk)
        link = re.search(r'href\s*="(/de/catalogs/\d+/item/[^"]+)"', chunk)
        img = re.search(r'<img[^>]*class="[^"]*catalog-image-preview[^"]*"[^>]*src="([^"]+)"', chunk) or \
            re.search(r'<img[^>]*src="([^"]+)"[^>]*class="[^"]*catalog-image-preview', chunk)
        lots.append(dict(lot=number[1], title=text_of(head[1]) if head else number[1], description=text_of(desc[1]) if desc else '',
                         limit=euro_number(limit[1]) if limit else None,
                         url=urljoin(MEHLIS_BASE, html.unescape(link[1])) if link else '',
                         image=urljoin(MEHLIS_BASE, html.unescape(img[1])) if img else ''))
    return lots, max(pages) if pages else 1, len(chunks)


def mehlis_ends(sale, timeline, lot):
    """拍品的结束（开拍）时间：时间表里号段含这件拍品的那一行；没有时取「Porzellan」行；都没有时该场最后一天 23:59。"""
    number, rows = timeline
    if number == sale['sale']:
        n = re.match(r'\d+', str(lot))
        for r in rows:
            if n and r['lo'] <= int(n[0]) <= r['hi']:
                return utc_z(r['at'])
        for r in rows:
            if r['category'].lower().startswith('porzellan'):
                return utc_z(r['at'])
    return utc_z(datetime.combine(sale['end'], dtime(23, 59)))


def mehlis_row(sale, timeline, lot):
    price = lot['limit']
    return row('mehlis', sale['sale'], _slug(lot['lot']), lot['title'], lot['description'], price,
               'Limit %s €' % euro_text(price) if price is not None else '', lot['url'], lot['image'],
               mehlis_ends(sale, timeline, lot['lot']))


def collect_mehlis(fetcher, today, window_days, out):
    sales = mehlis_sales(fetcher.get(MEHLIS_CATALOGS))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    try:
        timeline = mehlis_timeline(fetcher.get(MEHLIS_TIMELINE))
    except FetchError:
        timeline = (None, [])            # 时间表可选：取不到时按该场最后一天 23:59
    found = 0
    for sale in due:
        found += _pages(lambda n: mehlis_lots(fetcher.get(MEHLIS_LIST.format(cid=sale['cid'], cat=MEHLIS_PORZELLAN, page=n))),
                        1, lambda lot: out.append(mehlis_row(sale, timeline, lot)))
    return None if found else _empty_note(window_days)


# ---------- Lempertz ----------

LEMPERTZ_BASE = 'https://www.lempertz.com'
LEMPERTZ_CATALOGUES = LEMPERTZ_BASE + '/de/auktionskataloge.html'
LEMPERTZ_ENDPOINT = LEMPERTZ_BASE + '/de/?endpoint=catalogue'


def _local(text):
    try:
        return datetime.strptime((text or '').strip(), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def lempertz_catalogues(page):
    """目录页的 .catalogue-item[data-auction-number][data-catalogue-id]（带 span.action-dataset[data-bid-start]）
    → [{sale, cid, start（柏林当地 datetime）, deadline, end, title}]，同一目录只取一次。"""
    out, seen = [], set()
    pat = r'<div class="catalogue-item\s*"\s+data-auction-number="(\d+)"\s+data-catalogue-id="(\d+)">\s*<span class="action-dataset[^"]*"([^>]*)>'
    for m in re.finditer(pat, page):
        if m[2] in seen:
            continue
        attrs = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', m[3]))
        start = _local(attrs.get('bid-start'))
        if start is None:
            continue
        seen.add(m[2])
        title = re.search(r'<h3>(.*?)</h3>', page[m.end():m.end() + 4000], re.S)
        out.append(dict(sale=int(m[1]), cid=m[2], start=start, deadline=_local(attrs.get('bid-deadline')),
                        end=_local(attrs.get('bid-end')), title=text_of(title[1]) if title else ''))
    return out


def _porzellan_uids(categories):
    """categories 为 {keyword_uid: {word, lots}} 或 {Porzellan: [uid…]}；返回 Porzellan 类拍品 uid 集合。"""
    uids = set()
    if isinstance(categories, dict):
        for k, v in categories.items():
            if isinstance(v, dict) and str(v.get('word', '')).strip().lower() == 'porzellan':
                uids.update(v.get('lots') or [])
            elif str(k).strip().lower() == 'porzellan' and isinstance(v, list):
                uids.update(v)
    return {str(u) for u in uids}


def lempertz_lots(text):
    """目录 JSON（{lots: [...], categories: {...}}）→ Porzellan 类、未成交的拍品。JSON 无效时 ValueError。"""
    data = json.loads(text)
    if not isinstance(data, dict) or not isinstance(data.get('lots'), list):
        raise ValueError(PARSE_FAILED)
    wanted = _porzellan_uids(data.get('categories'))
    out = []
    for lot in data['lots']:
        try:
            if not isinstance(lot, dict) or str(lot.get('uid')) not in wanted or (number_of(lot.get('hammerprice')) or 0) > 0:
                continue
            number = '%s%s' % (lot.get('number') or '', str(lot.get('position') or '').strip())
            if not number:
                continue
            out.append(dict(lot=number, title=str(lot.get('title') or '').strip() or number,
                            description=str(lot.get('description') or '').strip(), estimate=number_of(lot.get('estimate')) or None,
                            estimate_to=number_of(lot.get('estimate_to')) or None, sale=lot.get('auction_number'),
                            ends=_local(lot.get('session_end')) or _local(lot.get('updated_bid_deadline')) or _local(lot.get('bid_deadline')),
                            url=str(lot.get('link') or ''), image=str(lot.get('identifier') or '')))
        except (TypeError, ValueError, AttributeError):
            log.warning('Lempertz 拍品格式异常，跳过')
    return out


def number_of(value):
    """int / float / 数字字符串 → float；其余（含布尔、空、读不出）→ None。"""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value.strip().replace(',', '.'))
        except ValueError:
            return None
    return None


def lempertz_row(catalogue, lot):
    price, high = number_of(lot['estimate']), number_of(lot['estimate_to'])
    if price is None:
        text = ''
    elif high and high != price:
        text = '估价 %s–%s €' % (euro_text(price), euro_text(high))
    else:
        text = '估价 %s €' % euro_text(price)
    ends = lot['ends'] or catalogue.get('end') or catalogue['start']
    url = lot['url'] if str(lot['url']).startswith('https://') else ''
    image = lot['image'] if str(lot['image']).startswith('https://') else ''
    return row('lempertz', lot['sale'] or catalogue['sale'], _slug(lot['lot']), lot['title'], lot['description'],
               price, text, url, image, utc_z(ends))


def collect_lempertz(fetcher, today, window_days, out):
    cats = lempertz_catalogues(fetcher.get(LEMPERTZ_CATALOGUES))
    if not cats:
        raise ValueError('解析不到场次，可能改版')
    sales = [dict(c, start=c['start'].date(), end=(c['end'] or c['start']).date()) for c in cats]
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    by_cid = {c['cid']: c for c in cats}
    found = 0
    for sale in due:
        lots = lempertz_lots(fetcher.post_json(LEMPERTZ_ENDPOINT, {'catalogue_uid': sale['cid'], 'catalogue_type': 'catalogueList'}))
        out.extend(lempertz_row(by_cid[sale['cid']], lot) for lot in lots)
        found += len(lots)
    if not found:
        return '%d 天内的目录里没有瓷器拍品' % window_days
    return None


# ---------- 调度 ----------

ADAPTERS = {'wendl': collect_wendl, 'mehlis': collect_mehlis, 'lempertz': collect_lempertz}


def _window(sales, today, window_days):
    """(要看的场次：还没结束且 window_days 天内开拍, 之后才开拍的场次)。"""
    horizon = today + timedelta(days=window_days)
    due = [s for s in sales if s['end'] >= today and s['start'] <= horizon]
    later = sorted((s for s in sales if s['start'] > horizon), key=lambda s: s['start'])
    return due, later


def _next_note(later, window_days):
    if later:
        return '下一场 %s，未到 %d 天' % (later[0]['start'].isoformat(), window_days)
    return '暂无即将开拍的场次'


def runnable(house):
    """只有 status=allowed、enabled 且有适配器（名单写了 adapter: true 且代码里有）的才访问。"""
    return house['status'] == 'allowed' and house['enabled'] and house['adapter'] and house['key'] in ADAPTERS


def make_session():
    return requests.Session()


def load_status(store):
    try:
        data = json.loads(store.get(STATUS_KEY) or '{}')
    except ValueError:
        data = {}
    return data if isinstance(data, dict) else {}


def run_due(store, config, clock=time.time, session=None, sleep=time.sleep, monotonic=time.monotonic, run_seconds=RUN_SECONDS):
    """采集轮里调用：今天（柏林日期）还没看过的拍卖行各看一次。每家先记今天的日期（出错也不再重试），
    取到的拍品逐件 store.ingest（新货、价格或结束时间变化由 ingest 自己判断；结束时间已过的不入库，免得把已判下架的拍品改回在售）。
    一家出错不影响其他家。本轮用时超过 run_seconds 后，其余拍卖行不开始、也不记为今天已看（下轮再看）。
    返回 {key: {new, found, error, note, requests}}（只含本轮实际运行的）。"""
    houses = [h for h in config.get('auction_houses') or [] if runnable(h)]
    bot = config.get('auction_house_bot') or {}
    window = bot.get('window_days', 90)
    ua = user_agent(bot.get('contact', ''))
    now = datetime.fromtimestamp(clock(), BERLIN)
    today = now.date()
    now_utc = now.astimezone(timezone.utc)
    started = monotonic()
    summary = {}
    for house in houses:
        if monotonic() - started >= run_seconds:
            break
        status = load_status(store)
        if (status.get(house['key']) or {}).get('date') == today.isoformat():
            continue
        entry = dict(date=today.isoformat(), at=now.astimezone(timezone.utc).isoformat(timespec='seconds'),
                     new=0, found=0, error=None, note=None)
        status[house['key']] = dict(entry, note='正在查看')
        store.set(STATUS_KEY, json.dumps(status, ensure_ascii=False))
        rows, fetcher = [], None
        try:
            if session is None:
                session = make_session()
            fetcher = Fetcher(session, ua, house['interval'], MAX_REQUESTS, sleep, monotonic)
            entry['note'] = ADAPTERS[house['key']](fetcher, today, window, rows)
        except Stop as e:
            entry['error'] = str(e)
        except FetchError as e:
            entry['error'] = '采集未完成：' + str(e)
        except ValueError as e:
            entry['error'] = str(e) or PARSE_FAILED
        except Exception:
            log.exception('拍卖行采集失败：%s', house['key'])
            entry['error'] = '采集未完成，明天再看'
        seen = set()
        for item in rows:
            if item['item_id'] in seen:
                continue
            seen.add(item['item_id'])
            if _ended(item.get('ends'), now_utc):
                continue
            try:
                if store.ingest(item, config) == 'new':
                    entry['new'] += 1
                entry['found'] += 1
            except Exception:
                log.exception('拍卖行拍品入库失败：%s', item.get('item_id'))
        entry['requests'] = fetcher.requests if fetcher else 0
        status = load_status(store)
        status[house['key']] = entry
        store.set(STATUS_KEY, json.dumps(status, ensure_ascii=False))
        summary[house['key']] = {k: entry[k] for k in ('new', 'found', 'error', 'note', 'requests')}
    return summary


def _ended(ends, now_utc):
    try:
        end = datetime.fromisoformat((ends or '').replace('Z', '+00:00'))
    except ValueError:
        return False
    return (end if end.tzinfo else end.replace(tzinfo=timezone.utc)) < now_utc


def status_view(store, root=ROOT, clock=time.time):
    """后台拍卖新品顶部的状态行：名单里会访问的各家 {key, name, checked_today, date, new, found, error, note}。"""
    from .config import auction_house_settings
    try:
        houses = auction_house_settings(root)['houses']
    except Exception:
        log.exception('拍卖行名单读取失败')
        return []
    today = datetime.fromtimestamp(clock(), BERLIN).date().isoformat()
    status = load_status(store)
    out = []
    for h in houses:
        if not runnable(h):
            continue
        st = status.get(h['key']) or {}
        out.append(dict(key=h['key'], name=h['name'], checked_today=st.get('date') == today, date=st.get('date'),
                        new=st.get('new') or 0, found=st.get('found') or 0, error=st.get('error'), note=st.get('note')))
    return out
