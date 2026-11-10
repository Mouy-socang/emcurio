"""故事 32：拍卖行自己的网站——名单（config/settings.yaml 的 auction_houses）上 status=allowed、enabled、有适配器的，
每柏林日最多看一次，找 window_days（90）天内开拍的场次，只读瓷器分类的列表页（Lempertz 读一个目录 JSON），
把拍品以纯拍卖身份（platform auction_house、buying AUCTION）入库，之后与 eBay 拍卖走同一套。

礼貌规则（Fetcher）：User-Agent 如实写 CurioObjectBot/0.1（可附 contact），不伪装浏览器；每次先读 robots.txt，禁止的路径不访问；
同一主机两次请求至少隔 interval 秒（名单里配置；robots 的 Crawl-delay 更长时按它）；每家每次最多 max_requests 个请求（名单里配置，缺省 MAX_REQUESTS）；
遇到 403、429 或验证页面，这家当天停下并记错，不重试、不绕过。不打开拍品页、不下载 PDF 图录与图片（入池存档的主图除外，见 vision）。

每个适配器分成纯解析函数（输入 HTML / JSON 文本，测试直接用样本）与取数函数（经 Fetcher）。
各家状态写 kv ah_status（JSON：key → {date, at, new, found, error, note}），后台拍卖新品顶部显示。"""
import html
import json
import logging
import re
import time
from datetime import date, datetime, time as dtime, timedelta, timezone
from urllib.parse import quote, unquote, unquote_plus, urljoin, urlsplit
from zoneinfo import ZoneInfo

import requests

from .config import HOUSE_MAX_REQUESTS, ROOT

log = logging.getLogger(__name__)
BERLIN = ZoneInfo('Europe/Berlin')
BOT = 'CurioObjectBot/0.1'
PLATFORM = 'auction_house'
MAX_REQUESTS = HOUSE_MAX_REQUESTS     # 每家每次的请求上限缺省值，与名单校验同一个常量
MAX_REDIRECTS = 3
TIMEOUT = 30
STATUS_KEY = 'ah_status'
# 只认验证页本身的标记（普通页面里的 reCAPTCHA 表单、文字不算）。
BLOCK_RE = re.compile(r'cf-chl|challenge-platform|verify you are human|<title>\s*Just a moment|<title>\s*Attention Required', re.I)
MAX_DELAY = 60            # robots Crawl-delay 的上限（秒）
RUN_SECONDS = 40 * 60     # run_due 每轮的总时限（故事 34 放宽到 40 分钟，仍短于采集锁的 3600 秒）：超过后其余拍卖行本轮不开始、不记为今天已看
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


class OutOfTime(LimitReached):
    """本轮总时限已到（run_due 传给 Fetcher 的 deadline）：这家停下，其余拍卖行本轮也不再开始。"""


class FetchError(Exception):
    """一次请求没拿到可用的页面（网络错误、非 200）。"""


class Robots:
    """robots.txt 规则（故事 34：自己解析，支持 Google 式的「*」通配与结尾「$」，如 Historia 的 Disallow: /*?）。
    先找点名我们的组（User-agent 是 CurioObjectBot 的前缀），没有时用「*」组；同组规则取匹配最长的一条，一样长时 Allow 优先。
    text 为 None 时全部放行（没有 robots.txt）。"""

    def __init__(self, text=None):
        self.allow_all = text is None
        self.groups = []
        if text is not None:
            self._parse(text)

    def _parse(self, text):
        group, agent_line = None, False
        for line in text.splitlines():
            line = line.split('#', 1)[0].strip()
            if ':' not in line:
                continue
            field, value = (x.strip() for x in line.split(':', 1))
            field = field.lower()
            if field == 'user-agent':
                if group is None or not agent_line:
                    group = dict(agents=[], rules=[], delay=None)
                    self.groups.append(group)
                group['agents'].append(value.lower())
                agent_line = True
                continue
            agent_line = False
            if group is None:
                continue
            if field in ('allow', 'disallow') and value:
                group['rules'].append((field == 'allow', value))
            elif field == 'crawl-delay':
                try:
                    group['delay'] = float(value)
                except ValueError:
                    pass

    def _groups(self, ua):
        name = (ua or '').split('/')[0].strip().lower()
        own = [g for g in self.groups if any(a != '*' and a.split('/')[0].strip() == name for a in g['agents'])]
        return own or [g for g in self.groups if '*' in g['agents']]

    @staticmethod
    def _match(pattern, path):
        anchored = pattern.endswith('$')
        body = unquote(pattern[:-1] if anchored else pattern)
        rx = '.*'.join(re.escape(part) for part in body.split('*')) + ('$' if anchored else '')
        return re.match(rx, path, re.S) is not None

    def can_fetch(self, ua, url):
        if self.allow_all:
            return True
        parts = urlsplit(url)
        path = unquote(parts.path or '/') + ('?' + unquote(parts.query) if parts.query or url.endswith('?') else '')
        best = None                      # (长度, 是否 Allow)
        for g in self._groups(ua):
            for allow, pattern in g['rules']:
                if self._match(pattern, path):
                    key = (len(pattern), allow)
                    if best is None or key > best:
                        best = key
        return best is None or best[1]

    def crawl_delay(self, ua):
        delays = [g['delay'] for g in self._groups(ua) if g['delay'] is not None]
        return max(delays) if delays else None


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

    def __init__(self, session, ua=BOT, interval=5.0, max_requests=MAX_REQUESTS, sleep=time.sleep, monotonic=time.monotonic, deadline=None):
        self.session, self.ua, self.interval, self.max_requests = session, ua, float(interval), max_requests
        self.deadline = deadline   # monotonic 时刻：过了就不再发请求（run_due 每轮的总时限）
        self.sleep, self.monotonic = sleep, monotonic
        self.requests = 0
        self.last = {}         # 主机 → 上次请求的 monotonic 时间
        self.delay = {}        # 主机 → 最短间隔（interval 与 robots Crawl-delay 取大）
        self.robots = {}       # origin → RobotFileParser

    def get(self, url):
        return self._send('GET', url)

    def get_bytes(self, url):
        """取页面的原始字节（自己判断编码时用，如 Leo Spik）。"""
        return self._send('GET', url, binary=True)

    def post_json(self, url, payload):
        return self._send('POST', url, json=payload)

    def _rules(self, url):
        parts = urlsplit(url)
        origin = parts.scheme + '://' + parts.netloc
        if origin not in self.robots:
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
                rp = Robots(text)
            elif 400 <= status < 500:
                rp = Robots(None)             # 没有 robots.txt（404 等）：按惯例视为不限制
            else:
                raise FetchError('robots.txt 读取失败（HTTP %d）' % status)
            self.robots[origin] = rp
            try:
                crawl = rp.crawl_delay(self.ua)
            except Exception:
                crawl = None
            self.delay[parts.hostname] = max(self.interval, min(float(crawl or 0), MAX_DELAY))
        return self.robots[origin]

    def _send(self, method, url, binary=False, **kw):
        for _ in range(MAX_REDIRECTS + 1):
            if not self._rules(url).can_fetch(self.ua, url):
                path = urlsplit(url).path or '/'
                raise RobotsDenied('robots 禁止：' + path)
            status, text, location = self._raw(method, url, with_location=True, binary=binary, **kw)
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

    def _raw(self, method, url, with_location=False, binary=False, **kw):
        if self.requests >= self.max_requests:
            raise LimitReached('已到每次 %d 个请求的上限，其余明天再看' % self.max_requests)
        host = urlsplit(url).hostname
        gap = self.delay.get(host, self.interval)
        if host in self.last:
            wait = gap - (self.monotonic() - self.last[host])
            if wait > 0:
                if self.deadline is not None and self.monotonic() + wait > self.deadline:
                    raise OutOfTime('本轮时限已到，其余明天再看')
                self.sleep(wait)
        if self.deadline is not None and self.monotonic() >= self.deadline:
            raise OutOfTime('本轮时限已到，其余明天再看')
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
        if binary:
            text = r.content if isinstance(getattr(r, 'content', None), bytes) else text.encode('utf-8')
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


# 列表页给的是缩略图（Mehlis 200×200、Lempertz 480、Wendl 420×280）；同一张图的大图地址只换尺寸段，不需要多发请求。
_BIG_IMAGE = ((re.compile(r'^(https://www\.mehlis\.eu/media/catalog/.+)\.(?:preview|medium)\.jpg$'), r'\1.jpg'),   # 原图约 2300×3500
              (re.compile(r'^(https://www\.lempertz\.com/lempertz_api/images/)(?:smedium|medium)/'), r'\1large/'),  # 约 1500 宽
              (re.compile(r'^(https://cdn\.aeman\.nl/AE_Wendl/image/)\d+/\d+/'), r'\1'),                           # 约 1280 宽
              # Historia（bidspirit）：各种缩放统一成 1000 宽（原图可能好几 MB，看图与存档都有大小上限）
              (re.compile(r'^(https://bidspirit-images\.global\.ssl\.fastly\.net/historia/cloned-images/\d+/\d+/)a_ignore_[a-z0-9_]+?_c_[a-z]+_(\w+\.(?:jpg|jpeg|png|webp))$'),
               r'\1a_ignore_q_80_w_1000_c_limit_\2'),
              (re.compile(r'^(https://(?:www\.)?leo-spik\.de/aa/bilder/)knopf/'), r'\1'),                         # Leo Spik：去掉 knopf/ 即大图
              # 故事 35：Bruun Rasmussen 列表给 470×470，同一张图有 1000×1000
              (re.compile(r'^(https://img\.bruun-rasmussen\.dk/.+)_470x470(\.\w+(?:\?.*)?)$'), r'\1_1000x1000\2'),
              # Döbritz：data-src 带 ?crop=True（裁成方图），去掉即原图
              (re.compile(r'^(https://auktionen\.doebritz\.de/[^?]+)\?crop=True$', re.I), r'\1'))


def big_image(url):
    """拍卖行首图换成清晰的大图地址；认不出的地址原样返回。"""
    for rx, repl in _BIG_IMAGE:
        if rx.match(url or ''):
            return rx.sub(repl, url)
    return url


def row(key, sale, lot, title, description, price, price_text, url, image, ends, currency='EUR', country='DE'):
    return dict(item_id='ah-%s-%s-%s' % (key, sale, lot), platform=PLATFORM, title=title[:500], description=description or '',
                price=price, currency=currency, price_text=price_text, buying='AUCTION', url=url, image=big_image(image or ''),
                ends=ends or '', country=country)


# 故事 35：只收标题或描述提到迈森 / KPM 的拍品（Bruun、Schuler、Döbritz、Dobiaschofsky 的瓷器分类里外厂多）。
MAKER_RE = re.compile(r'meissen|meißen|kpm|k\.p\.m\.|königliche porzellan|porzellanmanufaktur berlin', re.I)
MAKER_NOTE = '%d 天内的场次里没有提到迈森 / KPM 的未成交拍品'


def maker_wanted(*texts):
    return any(MAKER_RE.search(t or '') for t in texts)


def numeric_days(text):
    """「24.11.2026 – 27.11.2026」「So 18.10.2026 bis Mo 21.12.2026」→ 日期列表（按出现顺序）。"""
    out = []
    for d, m, y in re.findall(r'(?<!\d)(\d{1,2})\.(\d{1,2})\.(\d{4})(?!\d)', text_of(text)):
        try:
            out.append(date(int(y), int(m), int(d)))
        except ValueError:
            continue
    return out


def plain_number(text):
    """「1'000」「1’000.00」「100000」「1.000」→ 1000.0（瑞士撇号、德文点号都是千位分隔）；读不出 None。"""
    m = re.search(r"\d[\d'’.\s]*", html.unescape(text or ''))
    if not m:
        return None
    raw = re.sub(r"['’\s]", '', m[0]).rstrip('.')       # 「1'000.–」：结尾的「.–」「.-」不算数字
    if re.fullmatch(r'\d+\.\d{2}', raw):           # 「1100.00」：小数点
        raw = raw.split('.')[0]
    try:
        return float(raw.replace('.', ''))
    except ValueError:
        return None


def estimate_text(lo, hi, unit):
    if lo is None:
        return ''
    if hi and hi != lo:
        return '估价 %s–%s %s' % (euro_text(lo), euro_text(hi), unit)
    return '估价 %s %s' % (euro_text(lo), unit)


def _end_of(day):
    return utc_z(datetime.combine(day, dtime(23, 59)))


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


def collect_wendl(fetcher, today, window_days, out, now=None):
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


def collect_mehlis(fetcher, today, window_days, out, now=None):
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


def collect_lempertz(fetcher, today, window_days, out, now=None):
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


# ---------- Historia（故事 34） ----------
# robots.txt：Crawl-delay 30，禁止 /*? 与 /agb/——永远不发带「?」的网址（卡片、链接里带「?」的一律跳过）。
# www.historia.de 会 301 到裸域，直接用裸域。拍品按号排序、每页 52 件；只翻还没开拍的瓷器号段所在的页。

HISTORIA_BASE = 'https://historia.de'
HISTORIA_AUCTIONS = HISTORIA_BASE + '/auktionen/'
HISTORIA_PER_PAGE = 52
HISTORIA_BACK = 5            # 估算的页过了号段起点时，最多往回退几页
HISTORIA_IMAGE = 'https://bidspirit-images.global.ssl.fastly.net/'
HISTORIA_TITLE_MAX = 120
HISTORIA_MISSED = '号段起点没找到，部分拍品可能漏收'


def _attr(tag, name):
    m = re.search(r'\b' + name + r'\s*=\s*"([^"]*)"', tag, re.I)
    return html.unescape(m[1]).strip() if m else ''


def historia_sales(page):
    """/auktionen/ 的场次卡片 → [{sale, url, start, end}]。日期取卡片里 add-to-calendar-button 的 startDate / endDate
    （按它的 timeZone 换成柏林日期）；链接带「?」的卡片（往期）跳过。"""
    out, seen = [], set()
    for card in re.split(r'<div class="favorite-auction auction-item\b', page)[1:]:
        link = re.search(r'href="(https://historia\.de/auction/(\d+)-[^"/]*/?)"', card)
        button = re.search(r'<add-to-calendar-button\b([^>]*)>', card, re.S)
        if not link or not button or '?' in link[1] or link[1] in seen:
            continue
        attrs = button[1]
        try:
            zone = ZoneInfo(_attr(attrs, 'timeZone') or 'Europe/Berlin')
        except Exception:
            zone = BERLIN
        days = []
        for d, t in (('startDate', 'startTime'), ('endDate', 'endTime')):
            try:
                local = datetime.strptime(_attr(attrs, d) + ' ' + (_attr(attrs, t) or '00:00:00')[:8], '%Y-%m-%d %H:%M:%S')
            except ValueError:
                try:
                    local = datetime.strptime(_attr(attrs, d), '%Y-%m-%d')
                except ValueError:
                    break
            days.append(local.replace(tzinfo=zone).astimezone(BERLIN).date())
        if len(days) != 2:
            continue
        seen.add(link[1])
        url = link[1] if link[1].endswith('/') else link[1] + '/'
        out.append(dict(sale=int(link[2]), url=url, start=min(days), end=max(days)))
    return out


def historia_schedule(page):
    """拍卖页的日程块（data-time + 表行「Porzellan Teil II | 2830 - 3659 | ab 10:00 Uhr」）→
    [{category, lo, hi, at（UTC datetime）}]。at 取这一行自己的「HH:MM Uhr」（同一天，柏林时间），没写时取块的 data-time。"""
    rows = []
    blocks = re.split(r'(<div class="information-block__item\b[^>]*>)', page)
    for head, body in zip(blocks[1::2], blocks[2::2]):
        try:
            at = datetime.fromisoformat(_attr(head, 'data-time').replace('Z', '+00:00'))
        except ValueError:
            continue
        if at.tzinfo is None:
            at = at.replace(tzinfo=timezone.utc)
        for tr in re.findall(r'<tr>(.*?)</tr>', body.split('</table>')[0], re.S):
            cells = [text_of(td) for td in re.findall(r'<td[^>]*>(.*?)</td>', tr, re.S)]
            rng = re.search(r'(\d+)\s*-\s*(\d+)', cells[1]) if len(cells) >= 2 else None
            if not rng:
                continue
            own = at.astimezone(timezone.utc)
            clock = re.search(r'(\d{1,2})[.:](\d{2})\s*Uhr', cells[2]) if len(cells) >= 3 else None
            if clock:
                try:
                    local = datetime.combine(at.astimezone(BERLIN).date(), dtime(int(clock[1]), int(clock[2])))
                    own = local.replace(tzinfo=BERLIN).astimezone(timezone.utc)
                except ValueError:
                    pass
            rows.append(dict(category=cells[0], lo=int(rng[1]), hi=int(rng[2]), at=own))
    return rows


def historia_counts(page):
    """拍卖页侧栏的分类件数 {分类名: 件数}（「Porzellan Teil I <span>830</span>」），用来估算号段在第几页。"""
    out = {}
    for name, n in re.findall(r'<li class="[^"]*category_by-\d+[^"]*">\s*<a[^>]*>(.*?)<span>\s*(\d+)\s*</span>', page, re.S):
        out[text_of(name)] = int(n)
    return out


def historia_title(short, description):
    """截断的标题 → 描述里按逗号分段、至少盖过截断处的完整前几段（不超过 HISTORIA_TITLE_MAX 字）。"""
    parts, out = description.split(','), ''
    for part in parts:
        nxt = (out + ',' + part) if out else part
        if len(nxt.strip()) > HISTORIA_TITLE_MAX:
            break
        out = nxt
        if len(out.strip()) >= len(short):
            break
    out = out.strip()
    return out if len(out) >= len(short) else short


def historia_lots(page):
    """拍品页 → (拍品列表, 末页（从 1 起）, 卡片数)。每件取 bp-lot lot-item 块的 data-lot-index、data-content
    （URL 编码的 JSON：title / content）、详情链接、首图（bidspirit）、「Startpreis: EUR n」。
    找不到拍品列表（页面结构变了）时卡片数为 None。"""
    pages = [int(n) for n in re.findall(r'href="https://historia\.de/auction/[^"?]*/page/(\d+)/"', page)]
    last = max(pages + [1])
    if 'class="bp-lots ' not in page and 'id="single-lot-' not in page:
        return [], last, None
    lots, seen = [], set()
    chunks = re.split(r'<div id="single-lot-', page)[1:]
    for chunk in chunks:
        chunk = chunk.split('<script', 1)[0]
        number = re.search(r'data-lot-index="(\d+[A-Za-z]?)"', chunk)
        if not number or number[1] in seen:
            continue
        seen.add(number[1])
        title = description = ''
        content = re.search(r'data-content="([^"]*)"', chunk)
        if content:
            try:
                data = json.loads(unquote_plus(html.unescape(content[1])))
                title = str(data.get('title') or '').strip()
                description = text_of(str(data.get('content') or ''))
            except (ValueError, AttributeError):
                pass
        if title and description.startswith(title) and ',' in description:
            # data-content 的 title 截断在约 33 个字（"Große Henkelvase, Freiherr v. Sch"）；描述就是完整标题，按逗号取到截断处之后的那一段为止。
            title = historia_title(title, description)
        if not title:
            name = re.search(r'<div class="grid-title">(.*?)</div>', chunk, re.S)
            title = text_of(name[1]) if name else ''
        link = re.search(r'href="(https://historia\.de/auction/[^"?]+/)"', chunk)
        img = re.search(r'data-src="(' + re.escape(HISTORIA_IMAGE) + r'[^"?]+)"', chunk)
        start = re.search(r'Startpreis:\s*(?:<[^>]+>\s*)*EUR\s*([\d.,]+)', chunk)
        price = None
        if start:
            if re.fullmatch(r'\d{1,3}(?:,\d{3})+', start[1]):          # 样本里的「EUR 10,000」：英文千位分隔
                price = float(start[1].replace(',', ''))
            else:
                price = euro_number(start[1])                           # 「1.200」「1.200,50」：德文写法
        lots.append(dict(lot=number[1], n=int(re.match(r'\d+', number[1])[0]), title=title or number[1],
                         description=description, start=price, url=html.unescape(link[1]) if link else '',
                         image=html.unescape(img[1]) if img else ''))
    return lots, last, len(chunks)


def historia_row(sale, lot, at):
    price = lot['start']
    return row('historia', sale['sale'], _slug(lot['lot']), lot['title'], lot['description'], price,
               '起拍 %s €' % euro_text(price) if price is not None else '', lot['url'], lot['image'],
               at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'))


def _historia_guess(lo, schedule, counts, per_page):
    """号段起点 lo 估计在第几页：号比 lo 小的各分类件数之和（侧栏件数，没有时按号段长度）÷ 每页件数。"""
    before = sum(counts.get(r['category'], r['hi'] - r['lo'] + 1) for r in schedule if r['hi'] < lo)
    return before // max(per_page, 1) + 1


def _historia_segment(read, lo, hi, guess, last, add):
    """翻一个号段：从估算页起，过了起点就往回退（最多 HISTORIA_BACK 页），再往后翻到号段终点或没有拍品为止。
    read(页码) → (拍品, 该页给出的末页)；末页取读过各页的最大值。号段里的拍品逐页交给 add（中途到请求上限时，已翻到的不丢）。
    页面按拍品号排序：一页里出现比 hi 大的号，号段（含 3659a 这类带字母的）就翻完了。
    返回 (件数, 是否没找到号段起点)。"""
    page = max(1, min(guess, last))
    for _ in range(HISTORIA_BACK):
        lots, page_last = read(page)
        last = max(last, page_last)
        nums = [l['n'] for l in lots]
        if page > 1 and (not nums or min(nums) > lo):
            page -= 1
            continue
        break
    lots, _ = read(page)
    missed = page > 1 and bool(lots) and min(l['n'] for l in lots) > lo
    found = 0
    while page <= last:
        lots, page_last = read(page)
        last = max(last, page_last)
        if not lots:
            break
        for lot in lots:
            if lo <= lot['n'] <= hi:
                add(lot)
                found += 1
        if max(l['n'] for l in lots) > hi:
            break
        page += 1
    return found, missed


def collect_historia(fetcher, today, window_days, out, now=None):
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    sales = historia_sales(fetcher.get(HISTORIA_AUCTIONS))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found, notes = 0, []
    for sale in due:
        first = fetcher.get(sale['url'])
        schedule = historia_schedule(first)
        porcelain = [r for r in schedule if 'porzellan' in r['category'].lower()]
        pending = [r for r in porcelain if r['at'] > now]
        if not porcelain:
            notes.append('%d 场的日程里还没有瓷器号段' % sale['sale'])
            continue
        if not pending:
            notes.append('%d 场的瓷器号段都已开拍' % sale['sale'])
            continue
        cache = {}
        lots1, last, cards = historia_lots(first)
        if cards is None:
            raise ValueError(PARSE_FAILED)
        cache[1] = (lots1, last)

        def read(n, sale=sale, cache=cache):
            if n not in cache:
                lots, page_last, cards = historia_lots(fetcher.get(sale['url'] + 'page/%d/' % n))
                if cards is None:
                    raise ValueError(PARSE_FAILED)
                cache[n] = (lots, page_last)
            return cache[n]
        counts = historia_counts(first)
        for seg in sorted(pending, key=lambda r: r['lo']):
            guess = _historia_guess(seg['lo'], schedule, counts, HISTORIA_PER_PAGE)
            n, missed = _historia_segment(read, seg['lo'], seg['hi'], guess, last,
                                          lambda lot, sale=sale, seg=seg: out.append(historia_row(sale, lot, seg['at'])))
            found += n
            if missed:
                notes.append('%d 场 %s（%d–%d）%s' % (sale['sale'], seg['category'], seg['lo'], seg['hi'], HISTORIA_MISSED))
    if notes:
        return '；'.join(notes)                  # 部分场次收到了也照样留下其他场次的说明
    return None if found else _empty_note(window_days)


# ---------- Leo Spik（故事 34） ----------
# 静态网页、没有 robots.txt。只有「Aktuelle Auktion」（/aa/aa.html）的场次号是 termine 里 90 天内的场次、
# 且不是 Nachverkauf（拍后补售）时，才读瓷器一章 /aa/porz.html。

LEO_BASE = 'https://www.leo-spik.de'
LEO_TERMINE = LEO_BASE + '/termine.html'
LEO_AA = LEO_BASE + '/aa/aa.html'
LEO_PORZ = LEO_BASE + '/aa/porz.html'


def leo_decode(raw):
    """页面字节 → 文字：先按 UTF-8，失败再按 cp1252（latin-1 的超集，0x80 是 €；HTML 实体在取字段时反转义）。"""
    if isinstance(raw, str):
        return raw
    try:
        return raw.decode('utf-8')
    except UnicodeDecodeError:
        return raw.decode('cp1252', errors='replace')


def _no_comments(page):
    return re.sub(r'<!--.*?-->', ' ', page, flags=re.S)


def leo_sales(page):
    """termine.html：「<b>Auktion 700</b><br>03. - 05. Dezember 2026<br>」→ [{sale, start, end}]。"""
    out = []
    for m in re.finditer(r'<b>\s*Auktion\s+(\d+)\s*</b>\s*<br\s*/?>\s*([^<]+)', _no_comments(page), re.I):
        days = german_days(m[2])
        if days:
            out.append(dict(sale=int(m[1]), start=min(days), end=max(days)))
    return out


def leo_current(page):
    """/aa/aa.html → {sale（当前场次号或 None）, nachverkauf, porz_at（瓷器一章所在那天的开始时间，柏林当地；读不到 None）}。"""
    page = _no_comments(page)
    head = page.split('<table', 1)[0]
    sale = re.search(r'Auktion\s+(\d+)', text_of(head))
    porz_at = None
    link = re.search(r'href="porz\.html"', page)
    if link:
        for m in re.finditer(r'(\d{1,2})\.\s*(\d{1,2})\.\s*(\d{4})\s*,?\s*(?:<br\s*/?>)?\s*BEGINN:?\s*(\d{1,2})[.:](\d{2})',
                             page[:link.start()], re.I):
            try:
                porz_at = datetime(int(m[3]), int(m[2]), int(m[1]), int(m[4]), int(m[5]))
            except ValueError:
                pass
    return dict(sale=int(sale[1]) if sale else None, nachverkauf=bool(re.search(r'nachverkauf', text_of(head), re.I)),
                porz_at=porz_at)


def leo_lots(page):
    """porz.html → 拍品列表；找不到 #Pic / #Num / #Descr 结构时 None。三个相邻块：
    #Pic（详情链接、knopf 缩略图）、#Num（拍品号）、#Descr（<b>标题</b><br>厂家与年代<br>描述<br>(编号) 600,-- €）。"""
    blocks = re.findall(r'<div id="Pic"[^>]*>(.*?)</div>\s*<div id="Num"[^>]*>(.*?)</div>\s*<div id="Descr"[^>]*>(.*?)</div>', page, re.S)
    if not blocks:
        return None          # 没有 #Num 或有 #Num 却一块也解析不出：都按改版处理
    out = []
    for pic, num, descr in blocks:
        number = re.search(r'\d+[A-Za-z]?', text_of(num))
        if not number:
            continue
        title = re.search(r'<b>\s*([^<]*?\S[^<]*?)\s*</b>', descr)
        rest = descr[title.end():] if title else descr
        segs = [text_of(x) for x in re.split(r'<br\s*/?>', rest)]
        segs = [x for x in segs if x]
        estimate = None
        if segs:
            m = re.search(r'([\d.]+),--\s*(?:€|EUR)\s*$', segs[-1])
            if m:
                estimate = float(m[1].replace('.', ''))
                segs = segs[:-1]
        maker = segs[0] if len(segs) > 1 else ''
        body = ' '.join(segs[1:] if len(segs) > 1 else segs)
        link = re.search(r'''href=['"]([^'"]+\.html)['"]''', pic)
        img = re.search(r'''<img[^>]*src=['"]([^'"]+)['"]''', pic)
        out.append(dict(lot=number[0], title=text_of(title[1]) if title else number[0], maker=maker, body=body,
                        estimate=estimate, url=urljoin(LEO_PORZ, html.unescape(link[1])) if link else '',
                        image=urljoin(LEO_PORZ, html.unescape(img[1])) if img else ''))
    return out


def leo_row(sale, ends, lot):
    price = lot['estimate']
    maker, body = lot['maker'], lot['body']
    description = (maker + (' ' if maker.endswith('.') else '. ') + body) if maker and body else (maker or body)
    return row('leo_spik', sale, _slug(lot['lot']), lot['title'], description, price,
               '估价 %s €' % euro_text(price) if price is not None else '', lot['url'], lot['image'], ends)


def collect_leo_spik(fetcher, today, window_days, out, now=None):
    sales = leo_sales(leo_decode(fetcher.get_bytes(LEO_TERMINE)))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    current = leo_current(leo_decode(fetcher.get_bytes(LEO_AA)))
    if current['sale'] is None:
        raise ValueError('解析不到当前场次，可能改版')
    want = {s['sale']: s for s in due}
    upcoming = min(due, key=lambda s: s['start'])['sale']
    if current['sale'] not in want or current['nachverkauf']:
        if current['sale'] == upcoming:
            return '目录已是 %d 场的拍后补售，不再收' % current['sale']
        what = '%d 场的拍后补售' % current['sale'] if current['nachverkauf'] else '%d 场' % current['sale']
        return '目录还是 %s，%d 场图录未上线' % (what, upcoming)
    now = (now or datetime.now(timezone.utc)).astimezone(timezone.utc)
    if current['porz_at'] and current['porz_at'].replace(tzinfo=BERLIN) <= now:
        return '%d 场的瓷器一章已开拍' % current['sale']
    page = leo_decode(fetcher.get_bytes(LEO_PORZ))
    head = text_of(_no_comments(page).split('<div id="Pic"', 1)[0])
    shown = re.search(r'Auktion\s+(\d+)', head)
    if re.search(r'nachverkauf', head, re.I) or (shown and int(shown[1]) != current['sale']):
        return '瓷器一章还是 %s 场的拍后补售，%d 场未上线' % (shown[1] if shown else '上一', current['sale'])
    lots = leo_lots(page)
    if lots is None:
        raise ValueError(PARSE_FAILED)
    sale = want[current['sale']]
    ends = utc_z(current['porz_at'] or datetime.combine(sale['end'], dtime(23, 59)))
    out.extend(leo_row(current['sale'], ends, lot) for lot in lots)
    return None if lots else _empty_note(window_days)


# ---------- Bruun Rasmussen（故事 35） ----------
# 丹麦，DKK。robots 禁止 /m/search：只读场次列表与每场的瓷器分类（category_id=492），网址都带 cas=guest&locale=en。

BRUUN_BASE = 'https://bruun-rasmussen.dk'
BRUUN_AUCTIONS = BRUUN_BASE + '/m/auctions?cas=guest&locale=en'
BRUUN_LIST = BRUUN_BASE + '/m/auctions/{id}?category_id=492&cas=guest&locale=en'
COPENHAGEN = ZoneInfo('Europe/Copenhagen')
BRUUN_TITLE_MAX = 160
BRUUN_EMPTY = 'Did you not find what you were looking for'   # 分类里没有拍品时页面的提示（2026-11-10 实测）


def _iso(text):
    try:
        at = datetime.fromisoformat((text or '').strip())
    except ValueError:
        return None
    return at if at.tzinfo else at.replace(tzinfo=COPENHAGEN)


def bruun_sales(page):
    """/m/auctions：「<a href="/m/auctions/1009096"><h1>…标题…</h1></a>…<time datetime="2026-11-17T15:00:00+01:00">」
    → [{id, title, at（带时区）, start, end（哥本哈根日期）}]，同一场只取一次。"""
    out, seen = [], set()
    for m in re.finditer(r'href="/m/auctions/(\d+)"[^>]*>\s*<h1[^>]*>(.*?)</h1>\s*</a>(.*?)</div>', page, re.S):
        t = re.search(r'<time datetime="([^"]+)"', m[3])
        at = _iso(t[1]) if t else None
        if m[1] in seen or at is None:
            continue
        seen.add(m[1])
        day = at.astimezone(COPENHAGEN).date()
        out.append(dict(id=m[1], title=text_of(m[2]), at=at, start=day, end=day))
    return out


def _first_sentence(text, limit):
    parts = re.split(r'(?<=[a-z0-9\)]{2}\.)\s+(?=[A-Z0-9"“\'])', text, maxsplit=1)
    head = parts[0].strip()
    if len(head) > limit:
        head = head[:limit].rsplit(' ', 1)[0].rstrip(',;:') + '…'
    return head


def bruun_lots(page):
    """某场的瓷器分类页 → (拍品列表, 卡片数)。<li id="lot_…"> 里：header「936/274」、p.description、
    <currency-range from to currency> 或 <currency-amount amount currency> 估价、hammer-time 的 <time datetime>、/m/lots/ 链接、首图。
    已有成交价（lot-price 不空）的不取。找不到拍品列表（页面结构变了）时卡片数为 None。"""
    if 'class="lots ' not in page and 'class="lots"' not in page:
        if BRUUN_EMPTY in page:
            return [], 0                  # 这一场没有瓷器：页面不给列表，只给「建搜索提醒」
        return [], None
    lots = []
    chunks = [c for c in re.split(r'<li id="lot_', page)[1:] if not c.startswith('placeholder')]
    for chunk in chunks:
        number = re.search(r'<header[^>]*>\s*<div[^>]*>\s*(\d+)\s*/\s*(\d+[A-Za-z]?)\s*</div>', chunk)
        if not number:
            continue
        price = re.search(r'data-update-key="lot-price"[^>]*>(.*?)</p>', chunk, re.S)
        if price and text_of(price[1]):
            continue
        desc = re.search(r'<p class="description[^"]*">(.*?)</p>', chunk, re.S)
        description = text_of(desc[1]) if desc else ''
        rng = re.search(r'<currency-range\b([^>]*)>', chunk)
        amt = re.search(r'<currency-amount\b([^>]*)>', chunk)
        lo = hi = None
        currency = 'DKK'
        if rng:
            lo, hi = number_of(_attr(rng[1], 'from')), number_of(_attr(rng[1], 'to'))
            currency = _attr(rng[1], 'currency') or currency
        elif amt:
            lo = number_of(_attr(amt[1], 'amount'))
            currency = _attr(amt[1], 'currency') or currency
        hammer = re.search(r'<time datetime="([^"]+)"[^>]*data-update-key="hammer-time-datetime"', chunk) or \
            re.search(r'data-update-key="hammer-time".*?<time datetime="([^"]+)"', chunk, re.S)
        link = re.search(r'href="(/m/lots/[0-9A-Za-z]+)"', chunk)
        img = re.search(r'<img[^>]*src="((?:https:)?//img\.bruun-rasmussen\.dk/[^"]+)"', chunk)
        image = html.unescape(img[1]) if img else ''
        if image.startswith('//'):
            image = 'https:' + image
        lots.append(dict(sale=number[1], lot=number[2], title=_first_sentence(description, BRUUN_TITLE_MAX) or number[0],
                         description=description, lo=lo, hi=hi, currency=currency.upper(),
                         at=_iso(hammer[1]) if hammer else None, url=BRUUN_BASE + link[1] if link else '', image=image))
    return lots, len(chunks)


def bruun_total(page):
    """列表的 data-item-count（这场瓷器分类的总件数）；没有时 None。"""
    m = re.search(r'<ul class="lots[^"]*"[^>]*data-item-count="(\d+)"', page)
    return int(m[1]) if m else None


def bruun_row(sale, lot):
    # 没有落槌时间时按该场当天 23:59（哥本哈根）：用开场时间的话当天就会被当作已结束
    at = lot['at'] or datetime.combine(sale['end'], dtime(23, 59)).replace(tzinfo=COPENHAGEN)
    return row('bruun_rasmussen', lot['sale'], _slug(lot['lot']), lot['title'], lot['description'], lot['lo'],
               estimate_text(lot['lo'], lot['hi'], lot['currency']), lot['url'], lot['image'],
               at.astimezone(timezone.utc).strftime('%Y-%m-%dT%H:%M:%S.000Z'), currency=lot['currency'], country='DK')


def collect_bruun_rasmussen(fetcher, today, window_days, out, now=None):
    sales = bruun_sales(fetcher.get(BRUUN_AUCTIONS))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found, parsed, failed, broken, partial = 0, 0, [], 0, []
    for sale in sorted(due, key=lambda s: s['at']):
        try:
            page = fetcher.get(BRUUN_LIST.format(id=sale['id']))
        except FetchError as e:
            failed.append(e)              # 一场取不到不影响其他场；全部取不到才算这家出错
            continue
        lots, cards = bruun_lots(page)
        if cards is None:
            broken += 1
            continue
        parsed += 1
        total = bruun_total(page)
        if total is not None and total > cards:
            partial.append('%s 场只显示了 %d / %d 件' % (sale['id'], cards, total))   # 不为补齐多发请求
        for lot in lots:
            if maker_wanted(lot['title'], lot['description']):
                out.append(bruun_row(sale, lot))
                found += 1
    if failed and len(failed) == len(due):
        raise failed[-1]
    if not parsed:
        raise ValueError(PARSE_FAILED)
    notes = (['%d 场的瓷器分类没读到' % len(failed)] if failed else []) + \
        (['%d 场的瓷器分类页面结构变了' % broken] if broken else []) + partial
    if notes:
        return '；'.join(notes)
    return None if found else MAKER_NOTE % window_days


# ---------- Schuler（故事 35） ----------
# 瑞士苏黎世，CHF。迈森在「Tischkultur」分类；场次卡片的 h2 没有链接表示在线图录还没上线（卡片写「Online-Katalog ab …」）。

SCHULER_BASE = 'https://www.schulerauktionen.ch'
SCHULER_AUCTIONS = SCHULER_BASE + '/de/auctions'
SCHULER_LIST = SCHULER_BASE + '/de/auctions/{code}?category=tischkultur&page=1'
SCHULER_MAX_PAGES = 30


def schuler_sales(page):
    """/de/auctions 的卡片 → [{sale, code, linked, start, end, catalog（图录上线日或 None）}]。"""
    out = []
    for card in re.split(r'<div class="auction-date\b', page)[1:]:
        head = re.search(r'(?:<a href="/de/auctions/(a\d+)"[^>]*>\s*)?<h2[^>]*>(.*?)</h2>', card, re.S)
        if not head:
            continue
        name = text_of(head[2])
        code = re.search(r'\bA\s*(\d+)\b', name)
        dates = re.search(r'<p>([^<]*\d{4}[^<]*)</p>', card[head.end():])
        days = numeric_days(dates[1]) if dates else []
        if not code or not days:
            continue
        catalog = re.search(r'Online-Katalog ab\s*([\d.]+)', text_of(card))
        cat_days = numeric_days(catalog[1]) if catalog else []
        out.append(dict(sale=int(code[1]), code=(head[1] or 'a%s' % code[1]).lower(), linked=bool(head[1]),
                        start=min(days), end=max(days), catalog=cat_days[0] if cat_days else None))
    return out


def schuler_lots(page, code):
    """Tischkultur 列表页 → (拍品列表, 下一页网址或 None, 卡片数)。每张 .grid-item：h2「1000. 标题」、<small>开拍时间、
    「CHF 1'000 – 2'000」估价、window.open 的 /de/items/ 链接、lg_ 图片；已有 Zuschlag 的不取。
    找不到列表（页面结构变了）时卡片数为 None。"""
    nxt = None
    m = re.search(r'<a[^>]*rel="next"[^>]*href="([^"]+)"', page) or re.search(r'<a[^>]*href="([^"]+)"[^>]*rel="next"', page)
    if m:
        href = html.unescape(m[1])
        if href.startswith('/de/auctions/%s?' % code):
            nxt = SCHULER_BASE + href
    if 'data-grid-view' not in page and 'class="grid-item"' not in page:
        return [], nxt, None
    lots = []
    chunks = re.split(r'<div class="grid-item">', page)[1:]
    for chunk in chunks:
        head = re.search(r'<h2 class="title">(.*?)</h2>', chunk, re.S)
        number = re.match(r'\s*(\d+[A-Za-z]?)\.\s*(.*)', text_of(head[1])) if head else None
        if not number or 'Zuschlag' in chunk:
            continue
        at = None
        small = re.search(r'<small>\s*(\d{1,2})\.(\d{1,2})\.(\d{4}),?\s*(\d{1,2}):(\d{2})\s*</small>', chunk)
        if small:
            try:
                at = datetime(int(small[3]), int(small[2]), int(small[1]), int(small[4]), int(small[5]))
            except ValueError:
                at = None
        prices = re.search(r'<div class="prices[^"]*">\s*CHF([^<]*)</div>', chunk)
        lo = hi = None
        if prices:
            parts = re.split(r'\s+[–-]\s+', html.unescape(prices[1]).strip())   # 「1'000.– – 2'000.–」：只认两边有空格的破折号
            lo = plain_number(parts[0])
            hi = plain_number(parts[1]) if len(parts) > 1 else None
        link = re.search(r"window\.open\('(/de/items/[^'?]+)", html.unescape(chunk))
        img = re.search(r'<img[^>]*src="(https://images\.schulerauktionen\.ch/[^"]+)"', chunk)
        lots.append(dict(lot=number[1], title=number[2] or number[1], at=at, lo=lo, hi=hi,
                         url=SCHULER_BASE + link[1] if link else '', image=html.unescape(img[1]) if img else ''))
    return lots, nxt, len(chunks)


def schuler_row(sale, lot):
    ends = utc_z(lot['at']) if lot['at'] else _end_of(sale['end'])
    return row('schuler', sale['sale'], _slug(lot['lot']), lot['title'], '', lot['lo'], estimate_text(lot['lo'], lot['hi'], 'CHF'),
               lot['url'], lot['image'], ends, currency='CHF', country='CH')


def collect_schuler(fetcher, today, window_days, out, now=None):
    sales = schuler_sales(fetcher.get(SCHULER_AUCTIONS))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found, notes = 0, []
    for sale in sorted(due, key=lambda s: s['start']):
        if not sale['linked']:
            when = sale['catalog'].isoformat() if sale['catalog'] else '稍后'
            notes.append('A%d 场图录 %s 上线' % (sale['sale'], when))
            continue
        url, seen, first = SCHULER_LIST.format(code=sale['code']), set(), True
        while url and url not in seen and len(seen) < SCHULER_MAX_PAGES:
            seen.add(url)
            lots, nxt, cards = schuler_lots(fetcher.get(url), sale['code'])
            if cards is None:
                if first:
                    raise ValueError(PARSE_FAILED)
                break
            first = False
            for lot in lots:
                if maker_wanted(lot['title']):
                    out.append(schuler_row(sale, lot))
                    found += 1
            if not cards:
                break
            url = nxt
    if notes:
        return '；'.join(notes)
    return None if found else MAKER_NOTE % window_days


# ---------- Döbritz（故事 35） ----------
# 法兰克福。场次只在「Aktuelle Auktionen」；每场按关键词搜两次（meissen、kpm），Lstatus=0，按 pagenumber 翻到 data-maxpage。

DOEBRITZ_BASE = 'https://auktionen.doebritz.de'
DOEBRITZ_CURRENT = DOEBRITZ_BASE + '/de/Auktionen/Aktuelle'
DOEBRITZ_SEARCH = DOEBRITZ_BASE + '/de/objekte/au-{sale}/{slug}?Q={q}&Lstatus=0&pagenumber={page}'
DOEBRITZ_QUERIES = ('meissen', 'kpm')
DOEBRITZ_MAX_PAGES = 15


def doebritz_sales(page):
    """/de/Auktionen/Aktuelle 的卡片「/de/objekte/au-26/nachverkauf_…」「So 18.10.2026 bis Mo 21.12.2026」
    → [{sale, slug, title, nachverkauf, start, end}]，同一场只取一次。"""
    out, seen = [], set()
    for card in re.split(r'<div class="highlight-auction">', page)[1:]:
        link = re.search(r'href="/de/objekte/au-(\d+)/([^"?/#]+)', card)
        if not link or link[1] in seen:
            continue
        title = re.search(r'<h4[^>]*>(.*?)</h4>', card, re.S)
        days = numeric_days(card)
        if not days:
            continue
        seen.add(link[1])
        name = text_of(title[1]) if title else link[2]
        out.append(dict(sale=int(link[1]), slug=link[2], title=name, nachverkauf=bool(re.search('nachverkauf', name + ' ' + link[2], re.I)),
                        start=min(days), end=max(days)))
    return out


def doebritz_lots(page):
    """搜索结果页 → (拍品列表, 末页, 卡片数)。每块 #pos_N：catalogNumber、/de/l/<id>/ 链接、h5 标题、「Startpreis 1.000 EUR」、
    data-src 图片；标着「Verkauft」的不取。找不到 #lotlist（页面结构变了）时卡片数为 None。"""
    last = re.search(r'data-maxpage="(\d+)"', page)
    last = int(last[1]) if last else 1
    if 'id="lotlist"' not in page:
        return [], last, None
    lots = []
    chunks = re.split(r'<div id="pos_\d+"', page)[1:]
    for chunk in chunks:
        number = re.search(r'<span class="catalogNumber">\s*([^<]+?)\s*</span>', chunk)
        if not number or re.search(r'class="ribbon sold"|>\s*Verkauft\s*<', chunk):
            continue
        link = re.search(r'href="(/de/l/(\d+)/[^"?]*)', chunk)
        head = re.search(r'<h5>\s*<a[^>]*>(.*?)</a>', chunk, re.S)
        start = re.search(r'Startpreis\s*([\d.,]+)\s*(?:EUR|€)', chunk)
        img = re.search(r'data-src="([^"]+)"', chunk)
        image = html.unescape(img[1]) if img else ''
        if not image.lower().startswith('/lots/images/'):
            image = ''                              # /de/Media/LotImage 是「暂无图片」占位
        lots.append(dict(lot=text_of(number[1]), id=link[2] if link else '', title=text_of(head[1]) if head else text_of(number[1]),
                         start=euro_number(start[1]) if start else None,
                         url=DOEBRITZ_BASE + link[1] if link else '', image=DOEBRITZ_BASE + image if image else ''))
    return lots, last, len(chunks)


def doebritz_row(sale, lot):
    price = lot['start']
    if price is None:
        text = ''
    elif sale['nachverkauf']:
        text = '拍后补售 · 起价 %s €' % euro_text(price)
    else:
        text = '起拍 %s €' % euro_text(price)
    # 补售场里不同原场次的拍品可能同号（如两件 698）：编号带上网站的拍品 id
    lot_key = _slug(lot['lot']) + ('-' + lot['id'] if lot['id'] else '')
    return row('doebritz', sale['sale'], lot_key, lot['title'], '', price, text, lot['url'], lot['image'], _end_of(sale['end']))


def collect_doebritz(fetcher, today, window_days, out, now=None):
    sales = doebritz_sales(fetcher.get(DOEBRITZ_CURRENT))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found = 0
    for sale in due:
        seen, parsed = set(), 0
        for q in DOEBRITZ_QUERIES:
            page, last = 1, 1
            while page <= min(last, DOEBRITZ_MAX_PAGES):
                lots, page_last, cards = doebritz_lots(fetcher.get(DOEBRITZ_SEARCH.format(sale=sale['sale'], slug=sale['slug'], q=q, page=page)))
                if cards is None:
                    break                 # 没有 #lotlist（如零结果）：当作这个词没有结果；整场都读不出才算改版
                parsed += 1
                if not cards:
                    break
                last = max(last, page_last)
                for lot in lots:
                    key = lot['id'] or lot['lot']
                    if key in seen or not maker_wanted(lot['title']):
                        continue
                    seen.add(key)
                    out.append(doebritz_row(sale, lot))
                    found += 1
                page += 1
        if not parsed:
            raise ValueError(PARSE_FAILED)
    return None if found else MAKER_NOTE % window_days


# ---------- Dobiaschofsky（故事 35） ----------
# 瑞士伯尔尼，CHF。首页写「Letzte / Nächste Auktion」与图录页 dNNN.html；瓷器分类 kat=525，按 setp 翻页。页面是 latin-1（cp1252）。

DOBI_BASE = 'https://www.dobiaschofsky.com'
DOBI_HOME = DOBI_BASE + '/'
DOBI_LIST = DOBI_BASE + '/{page}.html?kat=525&setp={n}'
DOBI_MAX_PAGES = 20


def dobi_decode(raw):
    return raw if isinstance(raw, str) else raw.decode('cp1252', errors='replace')


def dobi_sales(page):
    """首页「Letzte Auktion / Nächste Auktion」块 → [{name, page（图录页 dNNN 或 None）, start, end}]。"""
    out = []
    for m in re.finditer(r'<li[^>]*>\s*(?:Letzte|N(?:ä|&auml;)chste) Auktion\s*<br\s*/?>(.*?)</li>(.*?)</ul>', page, re.S | re.I):
        name = re.search(r'<b>(.*?)</b>', m[1], re.S)
        days = german_days(m[1])
        if not days:
            continue
        link = re.search(r'<a href="/?(d\d+)\.html">\s*Katalog\s*</a>', m[2])
        out.append(dict(name=text_of(name[1]) if name else '', page=link[1] if link else None, start=min(days), end=max(days)))
    return out


def dobi_lots(page, n=1):
    """瓷器分类页 → (场次号或 None, 拍品列表, 有无下一页, 卡片数)。每件：form action（拍品页）、首图、h4 拍品号、h3、p、
    「Schätzung CHF 250」；有 Zuschlag 的不取。找不到列表（页面结构变了）时卡片数为 None。"""
    sale = re.search(r'<h2>\s*A-?(\d+)\b', page) or re.search(r'action="/d\d+-A(\d+)-', page)
    if 'DIV7114treffer' not in page:
        return (sale[1] if sale else None), [], False, None
    lots = []
    chunks = re.split(r'<form method=POST action="', page)[1:]
    chunks = [c for c in chunks if '<h4>' in c.split('</li>', 1)[0]]
    for chunk in chunks:
        chunk = chunk.split('</li>', 1)[0]
        info = re.search(r'<h4>(.*?)</h4>\s*<h3>(.*?)</h3>\s*(?:<p>(.*?)</p>)?', chunk, re.S)
        if not info or 'Zuschlag' in chunk:
            continue
        number = text_of(info[1])
        if not number:
            continue
        action = chunk.split('"', 1)[0].split('?', 1)[0]
        est = re.search(r'Schätzung</td>\s*<td>.*?CHF.*?</td>\s*<td[^>]*>([^<]*)</td>', chunk, re.S)
        img = re.search(r'<img src="(/abblot/[^"]+)"', chunk)
        head, body = text_of(info[2]), text_of(info[3] or '')
        lots.append(dict(lot=number, title=(head + ' – ' + body) if body else head, head=head, description=body,
                         estimate=plain_number(est[1]) if est else None,
                         url=DOBI_BASE + quote(html.unescape(action), safe="/-_.~&'()") if action.startswith('/') else '',
                         image=DOBI_BASE + img[1] if img else ''))
    pages = [int(x) for x in re.findall(r'[?&]setp=(\d+)', page)]
    return (sale[1] if sale else None), lots, any(x > n for x in pages), len(chunks)


def dobi_row(sale_no, sale, lot):
    price = lot['estimate']
    return row('dobiaschofsky', sale_no, _slug(lot['lot']), lot['title'], lot['description'], price,
               estimate_text(price, None, 'CHF'), lot['url'], lot['image'], _end_of(sale['end']), currency='CHF', country='CH')


def collect_dobiaschofsky(fetcher, today, window_days, out, now=None):
    sales = dobi_sales(dobi_decode(fetcher.get_bytes(DOBI_HOME)))
    if not sales:
        raise ValueError('解析不到场次，可能改版')
    due, later = _window(sales, today, window_days)
    if not due:
        return _next_note(later, window_days)
    found, notes = 0, []
    for sale in due:
        if not sale['page']:
            notes.append('%s 图录未上线' % (sale['name'] or sale['start'].isoformat()))
            continue
        n, more = 1, True
        while more and n <= DOBI_MAX_PAGES:
            sale_no, lots, more, cards = dobi_lots(dobi_decode(fetcher.get_bytes(DOBI_LIST.format(page=sale['page'], n=n))), n)
            if cards is None:
                if n == 1:
                    raise ValueError(PARSE_FAILED)
                break                     # 后面的页读不出：已收到的照样入库
            if not sale_no:
                raise ValueError(PARSE_FAILED)   # 没有场次号就不入库，免得同一件拍品换了编号
            for lot in lots:
                if maker_wanted(lot['head'], lot['description']):
                    out.append(dobi_row(sale_no, sale, lot))
                    found += 1
            if not cards:
                break
            n += 1
    if notes:
        return '；'.join(notes)
    return None if found else MAKER_NOTE % window_days


# ---------- 调度 ----------

ADAPTERS = {'wendl': collect_wendl, 'mehlis': collect_mehlis, 'lempertz': collect_lempertz,
            'leo_spik': collect_leo_spik, 'bruun_rasmussen': collect_bruun_rasmussen, 'schuler': collect_schuler,
            'doebritz': collect_doebritz, 'dobiaschofsky': collect_dobiaschofsky, 'historia': collect_historia}


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


def run_due(store, config, clock=time.time, session=None, sleep=None, monotonic=time.monotonic, run_seconds=RUN_SECONDS):
    """采集轮里调用：今天（柏林日期）还没看过的拍卖行各看一次。每家先记今天的日期（出错也不再重试），
    取到的拍品逐件 store.ingest（新货、价格或结束时间变化由 ingest 自己判断；结束时间已过的不入库，免得把已判下架的拍品改回在售）。
    一家出错不影响其他家。本轮用时超过 run_seconds 后，其余拍卖行不开始、也不记为今天已看（下轮再看）。
    返回 {key: {new, found, error, note, requests}}（只含本轮实际运行的）。"""
    sleep = sleep or time.sleep          # 调用时再取（测试替换 time.sleep 才生效）
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
        rows, fetcher, out_of_time = [], None, False
        try:
            if session is None:
                session = make_session()
            fetcher = Fetcher(session, ua, house['interval'], house.get('max_requests') or MAX_REQUESTS, sleep, monotonic,
                              deadline=started + run_seconds)
            entry['note'] = ADAPTERS[house['key']](fetcher, today, window, rows, now=now_utc)
        except OutOfTime as e:
            entry['error'] = str(e)
            out_of_time = True
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
        if out_of_time:
            break
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
