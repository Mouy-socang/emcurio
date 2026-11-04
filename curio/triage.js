/* 后台 01 新货（故事 7，CAP-6；故事 22 改为瀑布流）：多列大图卡片、往下滑续载（游标）、卡上留下 / 去掉、
   乐观写入与行内重试、撤销、已自动移出、货详情与放大层；以及墨墨的行为记录（看到、停留、点开详情及详情停留）。
   原生 JS，无构建、无外部库。所有写请求带 X-Curio-Token；留下 / 去掉 / 捞回 / 撤销带 request_id（服务端幂等）。 */
'use strict';
const $ = id => document.getElementById(id);

// 顶部分区栏固定在页顶；量出它的高度，让 01 的计数条贴在它下面、跳转定位不被它挡住
(() => {
  const tabs = document.querySelector('.tabs');
  if (!tabs) return;
  const set = () => document.documentElement.style.setProperty('--tabs-h', tabs.offsetHeight + 'px');
  set();
  if (window.ResizeObserver) new ResizeObserver(set).observe(tabs);
})();
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const rid = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2);
const LABEL = {keep: '留下', remove: '去掉'};
const FEED_BATCH = 40;

const cards = new Map();   // item_id -> {card, li}：瀑布流里的卡片（含正在淡出、尚未确认的）
const handled = new Set(); // 本地已留下 / 去掉（含未确认）的货，续载时排除
const failed = new Map();  // request_id -> {action, id}：写入失败、待重试
let newCount = null, arCount = null, inflight = 0, chain = Promise.resolve();
let undoRetryRid = null, undoBusy = false;
let cursor = null, more = true, feedLoading = false, feedGen = 0, feedLoaded = false;

/* ---------- 网络 ---------- */
async function post(path, body) {
  let r;
  try {
    r = await fetch(path, {method: 'POST', headers: {'Content-Type': 'application/json', 'X-Curio-Token': token}, body: JSON.stringify(body)});
  } catch (e) { const err = new Error('network'); err.network = true; throw err; }
  let data = {};
  try { data = await r.json(); } catch (e) { /* 非 JSON 响应 */ }
  return {ok: r.ok, status: r.status, data};
}
async function get(path) {
  const r = await fetch(path);
  const data = await r.json();
  if (!r.ok) throw new Error(data.error || '读取失败');
  return data;
}
// 写请求按顺序发出：撤销总是作用于之前已发出的动作。
function send(body) {
  inflight++;
  const p = chain.then(() => post('/api/triage/action', body));
  chain = p.catch(() => {});
  return p.finally(() => { inflight--; });
}

/* ---------- 朗读与提示 ---------- */
function say(text) { const el = $('live'); el.textContent = ''; setTimeout(() => { el.textContent = text; }, 30); }
function shout(text) { const el = $('alert'); el.textContent = ''; setTimeout(() => { el.textContent = text; }, 30); }
function showMsg(text) { $('msg').textContent = text; $('msg').hidden = false; }
function hideMsg() { $('msg').hidden = true; }
function setNet(on) { $('net').hidden = !on; }
function updateFail() {
  $('failN').textContent = failed.size;
  const hide = failed.size === 0;
  if (hide && document.activeElement === $('retry')) $('undo').focus();
  $('fail').hidden = hide;
}

/* ---------- 瀑布流卡片 ---------- */
function setLang(el, lang) { if (lang) el.setAttribute('lang', lang); else el.removeAttribute('lang'); }
function tagEl(text) {
  const s = document.createElement('span'); s.className = 'tag';
  if (text.includes('Kleinanzeigen')) { s.append('仅后台 · '); const k = document.createElement('span'); k.lang = 'de'; k.textContent = 'Kleinanzeigen'; s.append(k); }
  else s.textContent = text;
  return s;
}
function counts() {
  $('cNew').textContent = newCount == null ? '—' : newCount;
  $('arN').textContent = arCount == null ? '—' : arCount;
  $('arN2').textContent = arCount == null ? '' : arCount;
  $('seenN').textContent = seenCount == null ? '—' : seenCount;
  $('seenN2').textContent = seenCount == null ? '' : seenCount;
  $('emptySeenN').textContent = seenCount == null ? '' : seenCount;
  $('emptySeen').hidden = !(seenCount > 0);
  if ($('aucN')) {          // 故事 20：拍卖新品 N 件（24 小时内结束 M 件）
    $('aucN').textContent = aucCount == null ? '—' : aucCount;
    $('aucSoonN').textContent = aucSoon == null ? '—' : aucSoon;
    $('aucN2').textContent = aucCount == null ? '' : aucCount;
  }
}
function cardEl(c) {
  const li = document.createElement('li'); li.className = 'fc'; li.dataset.id = c.item_id;
  const ph = document.createElement('button'); ph.type = 'button'; ph.className = 'fph'; ph.setAttribute('aria-label', c.name + '，打开货详情');
  if (c.image) {
    const im = document.createElement('img'); im.alt = ''; im.loading = 'lazy'; im.decoding = 'async';
    // 图片按原比例显示（夹在 3:5–5:3 之间，避免极端长图）；加载前用 4:5 占位，加载后重新排版。
    im.onload = () => { const r = im.naturalWidth / im.naturalHeight; if (r > 0) ph.style.aspectRatio = String(Math.min(5 / 3, Math.max(0.6, r))); layoutSoon(); };
    im.onerror = () => { im.remove(); ph.classList.add('noimg'); ph.textContent = '图片未能加载'; layoutSoon(); };
    im.src = c.image; ph.append(im);
  } else { ph.classList.add('noimg'); ph.textContent = '暂无图片'; }
  const meta = document.createElement('div'); meta.className = 'fmeta';
  const h = document.createElement('h2'); const t = document.createElement('span'); t.textContent = c.name; setLang(t, c.name_lang); h.append(t);
  (c.tags || []).forEach(x => h.append(tagEl(x)));
  const pr = document.createElement('p'); pr.className = 'fpr'; const pm = document.createElement('span'); pm.className = 'mono'; pm.textContent = c.price; pr.append(pm);
  const src = document.createElement('p'); src.className = 'fsrc'; src.textContent = c.platform;
  if (c.code) { src.append(' · '); const cd = document.createElement('span'); cd.className = 'mono'; cd.textContent = c.code; src.append(cd); }
  meta.append(h, pr, src);
  // 故事 27：分数与理由先占位不显示（不让分数先入为主），墨墨点要或这张卡记为看到之后才显示（revealScore）。
  if (c.score != null) {
    const sc = document.createElement('p'); sc.className = 'fsc';
    const sm = document.createElement('span'); sm.className = 'mono'; sm.textContent = c.score; sc.append('分数 ', sm);
    if (c.score_reason) sc.append(' · ' + c.score_reason);
    if (!scoreOpen(c.item_id)) sc.classList.add('veil');
    meta.append(sc);
  }
  // 故事 21：看图识别的功能 · 器型 · 图案（不随分数遮挡，直接显示）。
  if (c.vision_line) { const vl = document.createElement('p'); vl.className = 'vis'; vl.textContent = c.vision_line; meta.append(vl); }
  const acts = document.createElement('div'); acts.className = 'facts';
  const rm = document.createElement('button'); rm.type = 'button'; rm.className = 'btn'; rm.dataset.act = 'remove'; rm.textContent = '去掉';
  const kp = document.createElement('button'); kp.type = 'button'; kp.className = 'btn primary'; kp.dataset.act = 'keep'; kp.textContent = '留下';
  rm.setAttribute('aria-label', '去掉 ' + c.name); kp.setAttribute('aria-label', '留下 ' + c.name);
  acts.append(rm, kp);
  const msg = document.createElement('p'); msg.className = 'im fmsg'; msg.hidden = true;
  li.append(ph, meta, acts, msg);
  ph.onclick = () => openDetail(c.item_id, 'new', ph);
  rm.onclick = () => act('remove', c.item_id);
  kp.onclick = () => act('keep', c.item_id);
  return li;
}
// 01 分数「先选后显示」：墨墨对这张卡点过要或去掉（含未确认），或本次打开里已记为看到，才显示分数与理由。
function scoreOpen(id) { return handled.has(id) || beh.isSeen(id); }
function revealScore(id) {
  const e = cards.get(id); if (!e) return;
  const sc = e.li.querySelector('.fsc'); if (sc) sc.classList.remove('veil');
}
// 放进瀑布流：front=true 放最前（捞回、撤销回来的货），否则接在末尾（续载）。
function addCard(c, front) {
  const old = cards.get(c.item_id);
  if (old) { old.li.remove(); beh.unwatch(old.li); }
  const li = cardEl(c);
  cards.set(c.item_id, {card: c, li});
  if (front) $('feed').prepend(li); else $('feed').append(li);
  beh.watch(li);
  layoutSoon();
  return li;
}
function dropCard(id) {
  const e = cards.get(id); if (!e) return;
  cards.delete(id); beh.unwatch(e.li); e.li.remove(); layoutSoon();
}
// 卡片淡出（留下 / 去掉后），淡出后不占位；写入确认前仍留在 DOM，失败时原位放回。
function fadeOut(li) {
  li.classList.add('out');
  const done = () => { if (li.classList.contains('out')) { li.hidden = true; layoutSoon(); } };
  if (reduced()) done(); else setTimeout(done, 240);
}
function fadeIn(li) { li.classList.remove('out'); li.hidden = false; layoutSoon(); }
function visibleCards() { return [...$('feed').children].filter(li => !li.hidden && !li.classList.contains('out')); }
function updateEmpty() {
  $('empty').hidden = !(feedLoaded && !more && !visibleCards().length && $('loadErr').hidden);
  $('emptySeen').hidden = !(seenCount > 0);      // 还有看过未选的货：空状态里给入口
}

/* 排版：DOM 顺序 = 阅读顺序 = Tab 顺序；按顺序依次放入当前较短的一列（不用 CSS columns，免得打乱顺序）。 */
let layoutPending = false;
function layoutSoon() {
  if (layoutPending) return;
  layoutPending = true;
  requestAnimationFrame(() => { layoutPending = false; layout(); });
}
function layout() {
  const feed = $('feed'), w = feed.clientWidth;
  if (!w) return;
  const n = w >= 900 ? 4 : w >= 600 ? 3 : 2, gap = w >= 600 ? 20 : 12, cw = (w - gap * (n - 1)) / n;
  const hs = new Array(n).fill(0);
  for (const li of feed.children) {
    if (li.hidden) continue;
    li.style.width = cw + 'px';
    const k = hs.indexOf(Math.min(...hs));
    li.style.left = k * (cw + gap) + 'px'; li.style.top = hs[k] + 'px';
    hs[k] += li.offsetHeight + gap + 8;
  }
  feed.style.height = Math.max(0, ...hs) + 'px';
  updateEmpty();
  nearEnd();
}
// 末尾已接近可视区时续载（含删掉卡片后页面变短的情况）。
function nearEnd() {
  if (!more || feedLoading || $('triageView').hidden || !$('loadErr').hidden) return;
  if ($('feedEnd').getBoundingClientRect().top < window.innerHeight + 800) loadMore();
}

async function loadMore() {
  if (feedLoading || !more) return false;
  feedLoading = true;
  const gen = feedGen;
  $('feedMore').hidden = false; $('loadErr').hidden = true;
  try {
    const d = await get('/api/triage/feed?limit=' + FEED_BATCH + '&session=' + encodeURIComponent(beh.session() || '') + (cursor ? '&cursor=' + encodeURIComponent(cursor) : ''));
    if (gen !== feedGen) return false;
    showHeader(d.header); seenCount = d.counts.seen; counts();
    d.items.forEach(c => { if (!cards.has(c.item_id) && !handled.has(c.item_id)) addCard(c, false); });
    cursor = d.next_cursor; more = !!cursor; feedLoaded = true;
    // 有写入在路上或未保存时，这份计数可能已过时，不采用。
    if (!inflight && !failed.size) { newCount = d.counts.new; arCount = d.counts.auto_removed; }
    if (!inflight && !failed.size && d.counts.auction != null) { aucCount = d.counts.auction; aucSoon = d.counts.auction_soon; }
    counts();
    return true;
  } catch (e) {
    if (gen === feedGen) { $('loadErr').hidden = false; shout('未能加载新货'); }
    return false;
  } finally {
    if (gen === feedGen) { feedLoading = false; $('feedMore').hidden = true; layoutSoon(); }
  }
}
// 从头重新读取瀑布流（重试、网络恢复）。
function load() {
  feedGen++; feedLoading = false; cursor = null; more = true;
  for (const id of [...cards.keys()]) if (!handled.has(id)) dropCard(id);
  return loadMore();
}
function reloadFeed() { return feedLoaded || cards.size ? loadMore() : load(); }

/* 页头"今日已留下 N 件 · 商品池共 M 件"（故事 22）：读瀑布流时带回；留下 / 去掉 / 撤销 / 捞回写入后稍等再读一次。 */
let seenCount = null, headerTimer = null;
function showHeader(h) { if (!h) return; $('kToday').textContent = h.kept_today; $('kPool').textContent = h.pool; }
function refreshHeader() {
  clearTimeout(headerTimer);
  headerTimer = setTimeout(async () => {
    try { showHeader(await get('/api/triage/header')); } catch (e) { /* 下次再读 */ }
    if (window.curioAgree) window.curioAgree.refreshCount();     // 07 符合度的分区计数（只读今天的摘要）
  }, 400);
}
function sync(d) {
  refreshHeader();
  // 01 计数 = 瀑布流能看到的件数：服务端的新货件数减去「看过即不要」排除的。
  if (!inflight && !failed.size && d.remaining != null) { newCount = Math.max(0, d.remaining - (seenCount || 0)); arCount = d.auto_removed; counts(); }
}
function cardMsg(id, text, retryRid) {
  const e = cards.get(id); if (!e) return;
  const m = e.li.querySelector('.fmsg'); m.textContent = text;
  if (retryRid) {
    m.append(' · ');
    const b = document.createElement('button'); b.type = 'button'; b.className = 'lnk lnk-pad'; b.textContent = '重试';
    b.onclick = () => retryOne(retryRid); m.append(b);
  }
  m.hidden = false; layoutSoon();
}
function clearCardMsg(id) { const e = cards.get(id); if (e) { e.li.querySelector('.fmsg').hidden = true; } }
// 处理后焦点去处：原卡片之后那张卡的同一个按钮；没有则"撤销上一步"。
function nextFocus(li, which) {
  let n = li.nextElementSibling;
  while (n && (n.hidden || n.classList.contains('out'))) n = n.nextElementSibling;
  if (!n) { n = li.previousElementSibling; while (n && (n.hidden || n.classList.contains('out'))) n = n.previousElementSibling; }
  return n ? n.querySelector('[data-act="' + which + '"]') : $('undo');
}

function act(action, id) {
  const e = cards.get(id);
  if (!e || e.li.classList.contains('out')) return;
  hideMsg();
  for (const [k, f] of failed) if (f.id === id) failed.delete(k);  // 重新处理：旧的未保存动作作废
  updateFail(); clearCardMsg(id);
  const hadFocus = e.li.contains(document.activeElement);
  const target = hadFocus ? nextFocus(e.li, action) : null;
  handled.add(id);
  if (action === 'keep') revealScore(id);
  fadeOut(e.li);
  if (target) target.focus();
  newCount = Math.max(0, (newCount || 0) - 1); counts();
  say('已' + LABEL[action] + '，剩余 ' + newCount + ' 件');
  dispatch(action, id, rid());
}
function markFailed(action, id, r, network) {
  failed.set(r, {action, id});
  const e = cards.get(id);
  if (e && handled.has(id)) { handled.delete(id); fadeIn(e.li); newCount = (newCount || 0) + 1; counts(); }
  cardMsg(id, '未保存', r);
  updateFail();
  if (network) setNet(true);
  shout(failed.size + ' 件未保存');
}
// 服务端拒绝（4xx）：该件没有被处理，原位放回、计数加回。
function restore(id, text) {
  const e = cards.get(id);
  if (e && handled.has(id)) { fadeIn(e.li); newCount = (newCount || 0) + 1; counts(); }
  handled.delete(id);
  cardMsg(id, text);
}
async function dispatch(action, id, r) {
  try {
    const res = await send({action, item_id: id, request_id: r});
    if (res.ok) {
      failed.delete(r); updateFail(); setNet(false);
      if (handled.has(id)) dropCard(id);
      sync(res.data); return true;
    }
    if (res.status >= 500) { markFailed(action, id, r, false); return false; }
    failed.delete(r); updateFail();
    restore(id, '未保存：' + (res.data.error || '请刷新本页后重试')); shout('未保存');
    return false;
  } catch (e) { markFailed(action, id, r, true); return false; }
}
// 重试一件：同一 request_id 重发（服务端幂等），卡片先淡出。
function retryOne(r) {
  const f = failed.get(r); if (!f) return Promise.resolve(true);
  const e = cards.get(f.id);
  if (e && !handled.has(f.id)) {
    const hadFocus = e.li.contains(document.activeElement), target = hadFocus ? nextFocus(e.li, f.action) : null;
    handled.add(f.id); clearCardMsg(f.id); fadeOut(e.li); if (target) target.focus();
    newCount = Math.max(0, (newCount || 0) - 1); counts();
  }
  failed.delete(r); updateFail();
  return dispatch(f.action, f.id, r);
}
async function retry() {
  const results = await Promise.all([...failed.keys()].map(retryOne));
  if (results.every(Boolean)) { setNet(false); say('已保存'); }
}
async function netRetry() {
  if (failed.size) return retry();
  if (await reloadFeed()) setNet(false);
}

async function undo() {
  if (undoBusy) return;
  undoBusy = true;
  try { await undoOnce(); } finally { undoBusy = false; }
}
async function undoOnce() {
  hideMsg();
  const r = undoRetryRid || rid();
  undoRetryRid = r;
  let res;
  try { res = await send({action: 'undo', request_id: r}); }
  catch (e) { setNet(true); showMsg('未能撤销，请重试'); shout('网络已断开，操作未保存'); return; }
  setNet(false);
  if (res.status >= 500) { showMsg('未能撤销，请重试'); shout('未能撤销'); return; }
  undoRetryRid = null;
  if (!res.ok) { showMsg(res.data.error || '没有可撤销的操作'); say(res.data.error || '没有可撤销的操作'); return; }
  const d = res.data, c = d.card;
  const id = c ? c.item_id : d.item_id, e = cards.get(id);
  const had = !!e && !handled.has(id);
  if (d.state === 'new' && c && d.auction) {
    // 故事 20：回到新货的纯拍卖不进瀑布流，回到拍卖新品（列表开着时重读）。
    if (e) dropCard(id);
    if (had) newCount = Math.max(0, (newCount || 0) - 1);
    aucCount = (aucCount || 0) + 1;
    if (!$('aucView').hidden) loadAuc(true);
  } else if (d.state === 'new' && c) {
    // 回到新货：放在瀑布流最前。
    handled.delete(id);
    addCard(c, true);
    if (!had) newCount = (newCount || 0) + 1;
  } else {
    // 撤销后不在新货（如撤销捞回 → 自动移出、撤销去掉 → 留下）：从瀑布流拿掉
    if (had) newCount = Math.max(0, (newCount || 0) - 1);
    if (e) dropCard(id);
    if (d.state === 'auto_removed') arCount = (arCount || 0) + 1;
  }
  sync(d); counts(); updateEmpty();
  const text = '已撤销，回到 ' + (c ? c.name : '上一步');
  showMsg(text); say(text);
}

/* ---------- 行为记录（故事 22）：只在本机 curio.db（owner_events），不上线、不进 GoodCustomer ----------
   seen：卡片 ≥50% 进入视口且持续 ≥0.6 秒，每件每次打开 01 记一次；
   dwell：卡片 ≥50% 在视口内的一段停留秒数，离开视口、页面隐藏或打开详情时上报（短于 0.6 秒的不报；服务端截到 1–600）；
   open：从瀑布流打开详情；detail_dwell：详情停留秒数，关闭时上报（页面隐藏时先报已停留的部分；服务端截到 1–1800）。
   留下 / 去掉不在这里记（以 triage_events 为准）。每批 ≤100 条；失败的留在页内队列下次重发；页面离开时 sendBeacon 补发。 */
const beh = (function () {
  const MAX_Q = 2000, BATCH = 100, SEEN_MS = 600, MIN_DWELL = 0.6;
  const q = [];
  let session = null, seen = new Set(), sending = null, stopped = false, flushTimer = null, paused = false, detailOpen = null;
  const since = new Map(), timers = new Map();
  const io = 'IntersectionObserver' in window ? new IntersectionObserver(onIo, {threshold: [0, 0.5]}) : null;
  function push(type, id, params) {
    if (!session) return;
    q.push({event_id: rid(), type, item_id: id, client_ts: new Date().toISOString(), session_id: session, params: params || null});
    if (q.length > MAX_Q) q.splice(0, q.length - MAX_Q);
    if (q.length >= 20) flush(); else schedule();
  }
  function schedule(ms) { if (!flushTimer) flushTimer = setTimeout(() => { flushTimer = null; flush(); }, ms || 5000); }
  function take(batch) { const s = new Set(batch); for (let i = q.length - 1; i >= 0; i--) if (s.has(q[i])) q.splice(i, 1); }
  // 发一批；已有一批在路上时返回那一批的 promise（flushAll 可等它）。结果 true = 这批已入库或已丢弃。
  function flush() {
    if (sending) return sending;
    if (!q.length || stopped) return Promise.resolve(!q.length);
    sending = (async () => {
      const batch = q.slice(0, BATCH);
      try {
        const res = await post('/api/owner-events', {events: batch});
        // 400：整批格式不对，重发也不会好，丢掉；403：令牌已失效（后台重启过），停止重发并提示刷新；5xx：稍后重发。
        if (res.ok || res.status === 400) { take(batch); return true; }
        if (res.status === 403) { stopped = true; showMsg(res.data.error || '请刷新本机审核页后重试'); return false; }
      } catch (e) { /* 网络失败：留在队列里 */ }
      return false;
    })();
    sending.then(ok => {
      sending = null;
      if (ok && q.length) schedule(0); else if (!ok && !stopped) schedule(30000);
    });
    return sending;
  }
  function beacon() {
    if (!navigator.sendBeacon) return;
    while (q.length) {
      const batch = q.slice(0, BATCH);
      // sendBeacon 不能带自定义请求头：令牌放在请求体（服务端同样校验令牌与 Origin）。
      if (!navigator.sendBeacon('/api/owner-events', JSON.stringify({token, events: batch}))) break;
      take(batch);
    }
  }
  function onIo(entries) {
    const t = performance.now();
    for (const en of entries) {
      const id = en.target.dataset.id; if (!id) continue;
      // 比视口高两倍以上的卡片到不了 50%：可见部分占视口一半以上也算。
      const big = en.intersectionRect.height >= 0.5 * window.innerHeight;
      if (en.isIntersecting && (en.intersectionRatio >= 0.5 || big) && !en.target.hidden) enter(id, t); else leave(id, t);
    }
  }
  function enter(id, t) {
    if (paused || document.hidden || since.has(id)) return;
    since.set(id, t);
    if (!seen.has(id) && !timers.has(id)) {
      timers.set(id, setTimeout(() => {
        timers.delete(id);
        if (since.has(id) && !seen.has(id)) { seen.add(id); push('seen', id); revealScore(id); }
      }, SEEN_MS));
    }
  }
  function leave(id, t) {
    clearTimeout(timers.get(id)); timers.delete(id);
    const s = since.get(id); if (s == null) return;
    since.delete(id);
    const sec = (t - s) / 1000;
    if (sec >= MIN_DWELL) push('dwell', id, {seconds: Math.round(sec * 10) / 10});
  }
  function leaveAll() { const t = performance.now(); for (const id of [...since.keys()]) leave(id, t); for (const [id, h] of timers) { clearTimeout(h); timers.delete(id); } }
  // 重新观察所有卡片：IntersectionObserver 对新观察的元素立即回报一次当前可见情况。
  function rewatch() { if (!io) return; for (const li of $('feed').children) { io.unobserve(li); io.observe(li); } }
  function pause() { leaveAll(); paused = true; }
  function resume() { paused = false; if (!document.hidden) rewatch(); }
  function detailSecs() { return detailOpen && detailOpen.t != null ? (performance.now() - detailOpen.t) / 1000 : null; }
  document.addEventListener('visibilitychange', () => {
    if (document.hidden) {
      leaveAll();
      const s = detailSecs(); if (s != null) { push('detail_dwell', detailOpen.id, {seconds: Math.round(s * 10) / 10}); detailOpen.t = null; }
      beacon();
    } else {
      if (detailOpen) detailOpen.t = performance.now();
      if (!paused) rewatch();
      if (q.length) schedule(1000);
    }
  });
  window.addEventListener('pagehide', () => {
    leaveAll();
    const s = detailSecs(); if (s != null) { push('detail_dwell', detailOpen.id, {seconds: Math.round(s * 10) / 10}); detailOpen.t = null; }
    beacon();
  });
  // 从往返缓存（bfcache）恢复：详情开着则重新计时（关闭时照常恢复记录），否则恢复记录并重新观察卡片。
  window.addEventListener('pageshow', e => {
    if (!e.persisted) return;
    if (detailOpen) detailOpen.t = performance.now();
    else { paused = false; rewatch(); }
  });
  return {
    // 每次打开 01 生成新的 session_id；"看到"按本次打开去重。
    start() { leaveAll(); session = rid(); seen = new Set(); rewatch(); },
    watch(li) { if (io) io.observe(li); },
    unwatch(li) { if (io) { io.unobserve(li); leave(li.dataset.id, performance.now()); } },
    open(id) { pause(); push('open', id); detailOpen = {id, t: performance.now()}; },
    close() {
      if (!detailOpen) return;
      const s = detailSecs(); if (s != null) push('detail_dwell', detailOpen.id, {seconds: Math.round(s * 10) / 10});
      detailOpen = null; resume();
    },
    session() { return session; },
    isSeen(id) { return seen.has(id); },
    // 换打开（离开 01 再回来）前把排队的事件尽量发出，服务端才知道哪些货"上次看过"。
    // 等在路上的那批，再把队列发完；某批确实失败（网络、5xx、403）就停下。
    async flushAll() { while (q.length && !stopped) { if (!(await flush())) return false; } return true; },
    flush, beacon, _queue: q
  };
})();
// 01 是否为当前分区（与 ranking.js 的分区切换同一口径）。
const onNewTab = () => !['#pool', '#rank', '#match', '#rules', '#guests'].includes(location.hash);
let wasNew = onNewTab();
window.addEventListener('hashchange', () => {
  const now = onNewTab();
  if (now && !wasNew) {
    // 重新打开 01：新的 session；上次看过没留下的货不再出现，所以从头重读瀑布流。
    beh.start(); hideMsg();
    beh.flushAll().then(() => { if (feedLoaded) load(); else layoutSoon(); });
  }
  if (!now && wasNew) beh.flush();
  wasNew = now;
});
window.addEventListener('resize', layoutSoon);
if ('IntersectionObserver' in window) {
  new IntersectionObserver(es => { if (es.some(e => e.isIntersecting) && $('loadErr').hidden) loadMore(); }, {rootMargin: '0px 0px 800px 0px'}).observe($('feedEnd'));
}

/* ---------- 已自动移出 ---------- */
let arOffset = 0;
function fmtTime(iso) {
  const d = new Date(iso); if (isNaN(d)) return '';
  const p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}
function arRow(c) {
  const li = document.createElement('li'); li.dataset.id = c.item_id;
  const th = document.createElement('button'); th.type = 'button'; th.className = 'th'; th.setAttribute('aria-label', c.name + '，打开货详情');
  if (c.image) { const im = document.createElement('img'); im.src = c.image; im.alt = ''; th.append(im); }
  const body = document.createElement('div');
  const t = document.createElement('button'); t.type = 'button'; t.className = 't'; t.textContent = c.name; setLang(t, c.name_lang);
  if (c.tags && c.tags.length) c.tags.forEach(x => t.append(tagEl(x)));
  const p = document.createElement('p'); p.className = 'p';
  const pm = document.createElement('span'); pm.className = 'mono'; pm.textContent = c.price; p.append(pm, ' · 来源 ' + c.platform);
  const reason = document.createElement('p'); reason.className = 'r'; reason.textContent = c.reason || '（无理由）'; setLang(reason, c.reason_lang);
  const when = document.createElement('p'); when.className = 'when'; when.textContent = '移出于 ' + fmtTime(c.removed_at);
  const btn = document.createElement('button'); btn.type = 'button'; btn.className = 'btn rescue'; btn.textContent = '捞回';
  const msg = document.createElement('p'); msg.className = 'im'; msg.hidden = true;
  body.append(t, p, reason, when, btn, msg);
  li.append(th, body);
  li._card = c;
  th.onclick = () => openDetail(c.item_id, 'rescue', th);
  t.onclick = () => openDetail(c.item_id, 'rescue', t);
  btn.onclick = () => rescueRow(li);
  return li;
}
async function loadAr(reset) {
  if (reset) { arOffset = 0; $('arList').textContent = ''; }
  $('arErr').hidden = true;
  try {
    const d = await get('/api/triage/auto-removed?limit=50&offset=' + arOffset);
    const shown = new Set([...$('arList').children].map(li => li.dataset.id));
    d.items.forEach(c => { if (!shown.has(c.item_id)) $('arList').append(arRow(c)); });
    arOffset += d.items.length; arCount = d.total; counts();
    $('arMore').hidden = arOffset >= d.total;
    $('arEmpty').hidden = $('arList').children.length > 0;
  } catch (e) { $('arErr').hidden = false; shout('未能读取列表'); }
}
function openAr() {
  hideMsg();
  $('triageView').hidden = true; $('arView').hidden = false;
  document.title = '已自动移出 · 后台';
  loadAr(true); $('arTitle').focus();
}
function closeAr() {
  $('arView').hidden = true; $('triageView').hidden = false;
  document.title = '新货 · 后台';
  layoutSoon();
  $('openAr').focus();
}
// 捞回成功：在售的货放到瀑布流最前，计数随之更新。
function applyRescued(d, fallback) {
  const card = d.card || fallback;
  handled.delete(card.item_id);
  if (cards.has(card.item_id)) dropCard(card.item_id);
  if (card.status === 'active') { addCard(card, true); newCount = (newCount || 0) + 1; }   // 分数同样先选后显示
  arCount = Math.max(0, (arCount || 0) - 1);
  setNet(false); sync(d); counts(); updateEmpty();
  return card;
}
async function rescueRow(li) {
  if (li.dataset.busy) return;
  const c = li._card, btn = li.querySelector('.rescue'), msg = li.querySelector('.im');
  const r = li.dataset.rid || rid(); li.dataset.rid = r;
  li.dataset.busy = '1'; btn.setAttribute('aria-busy', 'true');
  let res;
  try { res = await send({action: 'rescue', item_id: c.item_id, request_id: r}); }
  catch (e) { res = null; setNet(true); }
  delete li.dataset.busy; btn.removeAttribute('aria-busy');
  if (!res || res.status >= 500 || !res.ok) {
    msg.textContent = '';
    msg.append(res && !res.ok && res.status < 500 ? '未能捞回：' + (res.data.error || '请刷新') + ' · ' : '未能捞回 · ');
    const again = document.createElement('button'); again.type = 'button'; again.className = 'lnk lnk-pad'; again.textContent = '重试';
    again.onclick = () => rescueRow(li); msg.append(again); msg.hidden = false;
    shout('未能捞回，请重试'); btn.focus();
    return;
  }
  const card = applyRescued(res.data, c);
  arOffset = Math.max(0, arOffset - 1);
  const next = li.nextElementSibling;
  li.remove();
  $('arEmpty').hidden = $('arList').children.length > 0;
  say('已捞回 ' + card.name);
  if (next) next.querySelector('.rescue').focus(); else $('arTitle').focus();
}

/* ---------- 看过未选（故事 22） ----------
   之前的打开里看过、没留下的货（不改拣选状态，只是不再进瀑布流）。按最近看到时间新到旧，游标续载。
   「放回」记一条 restore 行为事件（event_id 固定，重试幂等），该件回到瀑布流最前；也可直接「留下」。 */
let seenCursor = null, seenGen = 0;
function seenRow(c) {
  const li = document.createElement('li'); li.dataset.id = c.item_id; li._card = c;
  const th = document.createElement('button'); th.type = 'button'; th.className = 'th'; th.setAttribute('aria-label', c.name + '，打开货详情');
  if (c.image) { const im = document.createElement('img'); im.src = c.image; im.alt = ''; im.loading = 'lazy'; th.append(im); }
  const body = document.createElement('div');
  const t = document.createElement('button'); t.type = 'button'; t.className = 't'; t.textContent = c.name; setLang(t, c.name_lang);
  (c.tags || []).forEach(x => t.append(tagEl(x)));
  const p = document.createElement('p'); p.className = 'p';
  const pm = document.createElement('span'); pm.className = 'mono'; pm.textContent = c.price; p.append(pm, ' · ' + c.platform);
  if (c.code) { p.append(' · '); const cd = document.createElement('span'); cd.className = 'mono'; cd.textContent = c.code; p.append(cd); }
  const when = document.createElement('p'); when.className = 'when'; when.textContent = '看到于 ' + fmtTime(c.seen_at);
  const acts = document.createElement('div'); acts.className = 'acts2';
  const back = document.createElement('button'); back.type = 'button'; back.className = 'btn'; back.dataset.k = 'restore'; back.textContent = '放回';
  const keep = document.createElement('button'); keep.type = 'button'; keep.className = 'btn primary'; keep.dataset.k = 'keep'; keep.textContent = '留下';
  back.setAttribute('aria-label', '放回 ' + c.name); keep.setAttribute('aria-label', '留下 ' + c.name);
  acts.append(back, keep);
  const msg = document.createElement('p'); msg.className = 'im'; msg.hidden = true;
  body.append(t, p, when, acts, msg); li.append(th, body);
  th.onclick = () => openDetail(c.item_id, 'seen', th);
  t.onclick = () => openDetail(c.item_id, 'seen', t);
  back.onclick = () => seenRestore(li);
  keep.onclick = () => seenKeep(li);
  return li;
}
async function loadSeen(reset) {
  if (reset) { seenGen++; seenCursor = null; $('seenList').textContent = ''; }
  const gen = seenGen;
  $('seenErr').hidden = true;
  try {
    const d = await get('/api/triage/seen?limit=' + FEED_BATCH + '&session=' + encodeURIComponent(beh.session() || '') + (seenCursor ? '&cursor=' + encodeURIComponent(seenCursor) : ''));
    if (gen !== seenGen) return;
    const shown = new Set([...$('seenList').children].map(li => li.dataset.id));
    d.items.forEach(c => { if (!shown.has(c.item_id)) $('seenList').append(seenRow(c)); });
    seenCursor = d.next_cursor; seenCount = d.total; counts();
    $('seenMore').hidden = !seenCursor;
    $('seenEmpty').hidden = $('seenList').children.length > 0;
  } catch (e) { if (gen === seenGen) { $('seenErr').hidden = false; shout('未能加载看过未选'); } }
}
function openSeen() {
  hideMsg();
  $('triageView').hidden = true; $('seenView').hidden = false;
  document.title = '看过未选 · 后台';
  loadSeen(true); $('seenTitle').focus();
}
function closeSeen() {
  $('seenView').hidden = true; $('triageView').hidden = false;
  document.title = '新货 · 后台';
  layoutSoon();
  $('openSeen').focus();
}
function seenRowMsg(li, text, retryFn) {
  const msg = li.querySelector('.im'); msg.textContent = text;
  if (retryFn) { msg.append(' · '); const b = document.createElement('button'); b.type = 'button'; b.className = 'lnk lnk-pad'; b.textContent = '重试'; b.onclick = retryFn; msg.append(b); }
  msg.hidden = false;
}
// 行离开列表：焦点到下一行的同一按钮，没有则列表标题。
function seenRemoveRow(li, k) {
  const next = li.nextElementSibling || li.previousElementSibling;
  li.remove();
  seenCount = Math.max(0, (seenCount || 0) - 1); counts();
  $('seenEmpty').hidden = $('seenList').children.length > 0;
  (next ? next.querySelector('[data-k="' + k + '"]') : $('seenTitle')).focus();
}
async function seenRestore(li) {
  if (li.dataset.busy) return;
  const c = li._card;
  li.dataset.eid = li.dataset.eid || rid();
  li.dataset.busy = '1'; li.querySelector('.im').hidden = true;
  let res = null;
  try {
    res = await post('/api/owner-events', {events: [{event_id: li.dataset.eid, type: 'restore', item_id: c.item_id,
      client_ts: new Date().toISOString(), session_id: beh.session() || rid(), params: null}]});
  } catch (e) { setNet(true); }
  delete li.dataset.busy;
  if (!res || !res.ok || res.data.dropped) {
    seenRowMsg(li, res && res.data && res.data.dropped ? '未能放回：这件货已不在新货里' : '未能放回', res && res.data && res.data.dropped ? null : () => seenRestore(li));
    shout('未能放回，请重试'); return;
  }
  setNet(false);
  // 先放到瀑布流最前（只是本页的即时反馈；之后重读时按上架时间排在原位）。
  const card = Object.assign({}, c); delete card.seen_at;
  if (!cards.has(c.item_id)) addCard(card, true);
  newCount = (newCount || 0) + 1;
  seenRemoveRow(li, 'restore');
  updateEmpty();
  say('已放回 ' + c.name);
}
async function seenKeep(li) {
  if (li.dataset.busy) return;
  const c = li._card;
  li.dataset.rid = li.dataset.rid || rid();
  li.dataset.busy = '1'; li.querySelector('.im').hidden = true;
  let res = null;
  try { res = await send({action: 'keep', item_id: c.item_id, request_id: li.dataset.rid}); } catch (e) { setNet(true); }
  delete li.dataset.busy;
  if (!res || res.status >= 500) { seenRowMsg(li, '未保存', () => seenKeep(li)); shout('未保存，请重试'); return; }
  if (!res.ok) { delete li.dataset.rid; seenRowMsg(li, '未保存：' + (res.data.error || '请刷新本页后重试')); shout('未保存'); return; }
  setNet(false);
  // 看过未选里的货不计入 01 计数；瀑布流里若也有这张卡（如刚放回），一并拿掉。
  const fe = cards.get(c.item_id);
  if (fe) { if (!handled.has(c.item_id)) newCount = Math.max(0, (newCount || 0) - 1); dropCard(c.item_id); }
  seenRemoveRow(li, 'keep');
  sync(res.data); counts();
  say('已留下 ' + c.name);
}

/* ---------- 拍卖新品（故事 20） ----------
   eBay 纯拍卖的新货不进瀑布流，单列在这里：未结束的按结束时间近到远，已过结束时间的在后（"已结束 · 待复查"）。
   剩余时间在本页按 ends 计算（<24 小时醒目）；留下 / 去掉走拣选动作（request_id 幂等），不记行为事件；
   「转为普通货」后回到新货瀑布流（POST /api/auction/convert）。 */
let aucCount = null, aucSoon = null, aucCursor = null, aucGen = 0;
const DAY_MS = 86400000;
function aucLeft(c) {
  // 返回 {text, soon, ended}：剩余时间（前端按 ends 计算）。
  const end = c.ends ? new Date(c.ends) : null;
  if (!end || isNaN(end)) return {text: '结束时间未知', soon: false, ended: false};
  const ms = end - Date.now();
  if (c.ended || ms <= 0) return {text: '已结束 · 待复查', soon: false, ended: true};
  const m = Math.floor(ms / 60000), d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  const text = '剩余 ' + (d ? d + ' 天 ' + h + ' 小时' : h ? h + ' 小时 ' + mm + ' 分' : Math.max(1, mm) + ' 分');
  return {text, soon: ms < DAY_MS, ended: false};
}
// 结束时间按柏林时间显示（"11月5日 21:05"，与 02 / 03 一致）；读不出时为空。
function berlinTime(iso) {
  const d = iso ? new Date(iso) : null; if (!d || isNaN(d)) return '';
  try {
    const f = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Europe/Berlin', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).formatToParts(d);
    const g = t => (f.find(x => x.type === t) || {}).value || '';
    return g('month') + '月' + g('day') + '日 ' + g('hour') + ':' + g('minute');
  } catch (e) { return ''; }
}
function aucRow(c) {
  const li = document.createElement('li'); li.dataset.id = c.item_id; li._card = c;
  const th = document.createElement('button'); th.type = 'button'; th.className = 'th'; th.setAttribute('aria-label', c.name + '，打开货详情');
  if (c.image) { const im = document.createElement('img'); im.src = c.image; im.alt = ''; im.loading = 'lazy'; th.append(im); }
  const body = document.createElement('div');
  const t = document.createElement('button'); t.type = 'button'; t.className = 't'; t.textContent = c.name; setLang(t, c.name_lang);
  const parts = [t];
  if (c.title && c.title !== c.name) { const st = document.createElement('p'); st.className = 'r'; st.textContent = c.title; setLang(st, c.title_lang); parts.push(st); }
  const p = document.createElement('p'); p.className = 'p';
  const pm = document.createElement('span'); pm.className = 'mono'; pm.textContent = c.bid || c.price; p.append('最近出价 ', pm, ' · ' + c.platform);
  parts.push(p);
  const left = aucLeft(c);
  const when = document.createElement('p'); when.className = 'when';
  const lt = document.createElement('span'); lt.className = left.ended ? 'ended' : 'left' + (left.soon ? ' soon' : ''); lt.textContent = left.text;
  when.append(lt);
  if (berlinTime(c.ends)) when.append(' · 结束于 ' + berlinTime(c.ends));
  parts.push(when);
  if (c.score != null) {
    const sc = document.createElement('p'); sc.className = 'r';
    const sm = document.createElement('span'); sm.className = 'mono'; sm.textContent = c.score;
    sc.append('综合分 ', sm); if (c.score_reason) sc.append(' · ' + c.score_reason);
    parts.push(sc);
  }
  if (c.url) {
    const lp = document.createElement('p'); const a = document.createElement('a'); a.className = 'lnk lnk-pad';
    a.href = c.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = 'eBay 原链接'; lp.append(a); parts.push(lp);
  }
  const acts = document.createElement('div'); acts.className = 'acts2';
  const keep = document.createElement('button'); keep.type = 'button'; keep.className = 'btn primary'; keep.dataset.k = 'keep'; keep.textContent = '留下';
  const rm = document.createElement('button'); rm.type = 'button'; rm.className = 'btn'; rm.dataset.k = 'remove'; rm.textContent = '去掉';
  keep.setAttribute('aria-label', '留下 ' + c.name); rm.setAttribute('aria-label', '去掉 ' + c.name);
  acts.append(keep, rm);
  const cv = document.createElement('button'); cv.type = 'button'; cv.className = 'btn'; cv.dataset.k = 'convert'; cv.textContent = '转为普通货';
  cv.setAttribute('aria-label', c.name + ' 转为普通货');
  const cvp = document.createElement('div'); cvp.className = 'acts2'; cvp.style.gridTemplateColumns = '1fr'; cvp.style.marginTop = '8px'; cvp.append(cv);
  const msg = document.createElement('p'); msg.className = 'im'; msg.hidden = true; msg.setAttribute('role', 'alert');
  parts.push(acts, cvp, msg);
  body.append(...parts); li.append(th, body);
  th.onclick = () => openDetail(c.item_id, 'auction', th);
  t.onclick = () => openDetail(c.item_id, 'auction', t);
  keep.onclick = () => aucAct(li, 'keep');
  rm.onclick = () => aucAct(li, 'remove');
  cv.onclick = () => aucConvert(li);
  return li;
}
async function loadAuc(reset) {
  if (reset) { aucGen++; aucCursor = null; $('aucList').textContent = ''; }
  const gen = aucGen;
  $('aucErr').hidden = true;
  try {
    const d = await get('/api/auctions?limit=' + FEED_BATCH + (aucCursor ? '&cursor=' + encodeURIComponent(aucCursor) : ''));
    if (gen !== aucGen) return;
    const shown = new Set([...$('aucList').children].map(li => li.dataset.id));
    d.items.forEach(c => { if (!shown.has(c.item_id)) $('aucList').append(aucRow(c)); });
    aucCursor = d.next_cursor; aucCount = d.counts.auction; aucSoon = d.counts.auction_soon; counts();
    $('aucMore').hidden = !aucCursor;
    $('aucEmpty').hidden = $('aucList').children.length > 0;
  } catch (e) { if (gen === aucGen) { $('aucErr').hidden = false; shout('未能加载拍卖新品'); } }
}
function openAuc() {
  hideMsg();
  $('triageView').hidden = true; $('aucView').hidden = false;
  document.title = '拍卖新品 · 后台';
  loadAuc(true); $('aucTitle').focus();
}
function closeAuc() {
  $('aucView').hidden = true; $('triageView').hidden = false;
  document.title = '新货 · 后台';
  layoutSoon();
  $('openAuc').focus();
}
function aucRemoveRow(li, k) {
  const c = li._card;
  const next = li.nextElementSibling || li.previousElementSibling;
  li.remove();
  aucCount = Math.max(0, (aucCount || 0) - 1);
  if (aucLeft(c).soon) aucSoon = Math.max(0, (aucSoon || 0) - 1);
  counts();
  $('aucEmpty').hidden = $('aucList').children.length > 0;
  (next ? next.querySelector('[data-k="' + k + '"]') : $('aucTitle')).focus();
}
async function aucAct(li, action) {
  if (li.dataset.busy) return;
  const c = li._card;
  if (li.dataset.ract !== action) { li.dataset.ract = action; li.dataset.rid = rid(); }
  li.dataset.busy = '1'; li.querySelector('.im').hidden = true;
  let res = null;
  try { res = await send({action, item_id: c.item_id, request_id: li.dataset.rid}); } catch (e) { setNet(true); }
  delete li.dataset.busy;
  if (!res || res.status >= 500) { seenRowMsg(li, '未保存', () => aucAct(li, action)); shout('未保存，请重试'); return; }
  if (!res.ok) { delete li.dataset.ract; seenRowMsg(li, '未保存：' + (res.data.error || '请刷新本页后重试')); shout('未保存'); return; }
  setNet(false);
  aucRemoveRow(li, action);
  sync(res.data); counts();
  say((action === 'keep' ? '已留下 ' : '已去掉 ') + c.name);
}
async function aucConvert(li) {
  if (li.dataset.busy) return;
  const c = li._card;
  li.dataset.cid = li.dataset.cid || rid();
  li.dataset.busy = '1'; li.querySelector('.im').hidden = true;
  let res = null;
  try { res = await post('/api/auction/convert', {item_id: c.item_id, on: true, request_id: li.dataset.cid}); } catch (e) { setNet(true); }
  delete li.dataset.busy;
  if (!res || res.status >= 500) { seenRowMsg(li, '未保存', () => aucConvert(li)); shout('未保存，请重试'); return; }
  if (!res.ok) { delete li.dataset.cid; seenRowMsg(li, '未保存：' + (res.data.error || '请刷新本页后重试')); shout('未保存'); return; }
  setNet(false);
  const d = res.data;
  // 转为普通货的新货回到瀑布流（本页先放在最前；之后重读时按上架时间排在原位）。
  if (d.state === 'new' && d.card && d.card.status === 'active' && !cards.has(c.item_id)) addCard(d.card, true);
  aucRemoveRow(li, 'convert');
  // 计数同 loadMore：有写入在路上或未保存时不采用服务器值；01 计数 = 新货件数减去「看过即不要」排除的。
  if (!inflight && !failed.size && d.counts) {
    newCount = Math.max(0, d.counts.new - (seenCount || 0)); aucCount = d.counts.auction; aucSoon = d.counts.auction_soon; counts();
  }
  updateEmpty();
  say('已转为普通货，回到新货：' + c.name);
}

/* ---------- 货详情 ---------- */
let detail = null, detailMode = 'new', detailOpener = null, gi = 0, savedComment = '', savedAlts = [];
function focusables(root) {
  return [...root.querySelectorAll('button,a[href],input,textarea,select,[tabindex]:not([tabindex="-1"])')].filter(el => !el.hidden && el.offsetParent !== null);
}
async function openDetail(id, mode, opener) {
  let d;
  try { d = await get('/api/triage/item?id=' + encodeURIComponent(id)); }
  catch (e) {
    if (!$('aucView').hidden) { $('aucMsg').textContent = '未能打开货详情，请重试'; $('aucMsg').hidden = false; }
    else if ($('arView').hidden) showMsg('未能打开货详情，请重试');
    else { $('arMsg').textContent = '未能打开货详情，请重试'; $('arMsg').hidden = false; }
    shout('未能打开货详情'); return;
  }
  $('arMsg').hidden = true; $('aucMsg').hidden = true;
  detail = d; detailMode = mode; detailOpener = opener;
  beh.close();
  if (mode === 'new' && cards.has(id)) beh.open(id);      // 行为记录：从瀑布流打开详情
  $('dCode').textContent = d.code || '';
  const nm = $('dName'); nm.textContent = d.name; setLang(nm, d.name_lang);
  const tags = $('dTags'); tags.textContent = '';
  const pr = document.createElement('span'); pr.className = 'mono'; pr.textContent = d.price; tags.append(pr);
  (d.tags || []).forEach(x => tags.append(tagEl(x)));
  // 故事 21：看图识别一行（在标准名称附近；没看过不显示）。
  const vis = $('dVision'); vis.textContent = d.vision_line || ''; vis.hidden = !d.vision_line;
  vis.title = 'AI 看图识别，仅供参考'; vis.setAttribute('aria-label', d.vision_line ? d.vision_line + '（AI 看图识别，仅供参考）' : '');
  // 图库
  const th = $('thumbs'); th.textContent = '';
  d.images.forEach((im, n) => {
    const li = document.createElement('li'), b = document.createElement('button');
    b.type = 'button'; b.setAttribute('aria-label', '第 ' + (n + 1) + ' 张');
    const i = document.createElement('img'); i.src = im.src; i.alt = ''; b.append(i);
    b.onclick = () => setGi(n); li.append(b); th.append(li);
  });
  const many = d.images.length > 1, any = d.images.length > 0;
  $('gPrev').hidden = $('gNext').hidden = !many; $('gZoom').hidden = $('gPg').hidden = !any; $('thumbs').hidden = !many;
  $('gMain').hidden = !any;
  let none = $('gal').querySelector('.noimg');
  if (!any && !none) { none = document.createElement('p'); none.className = 'noimg'; none.textContent = '暂无图片'; $('gal').append(none); }
  if (any && none) none.remove();
  gi = 0; if (any) setGi(0);
  // 01 卖家原文：照录、带 lang、不高亮
  const desc = $('dDesc'); desc.textContent = '';
  if (d.description) { const p = document.createElement('p'); p.textContent = d.description; setLang(p, d.description_lang); desc.append(p); }
  else { const p = document.createElement('p'); p.style.color = 'var(--muted)'; p.textContent = '卖家未提供描述'; desc.append(p); }
  // 02 来源与判断
  $('dPlat').textContent = d.platform;
  const u = $('dUrl'); u.textContent = '';
  if (d.url) { const a = document.createElement('a'); a.className = 'lnk lnk-pad'; a.href = d.url; a.target = '_blank'; a.rel = 'noopener noreferrer'; a.textContent = '原平台链接'; u.append(a); }
  else u.textContent = '—';
  $('dReason').textContent = d.agent_reason || '—';
  const sc = $('dScore'); sc.textContent = '';
  // 故事 27：从 01 瀑布流打开的详情同样「先选后显示」——点过要或已记为看到之后才显示分数与理由。
  if (mode === 'new' && cards.has(id) && !scoreOpen(id)) sc.textContent = '看到这件货之后显示';
  else if (d.score != null) { const m = document.createElement('span'); m.className = 'mono'; m.textContent = d.score; sc.append(m); if (d.score_reason) sc.append(' · ' + d.score_reason); }
  else sc.textContent = '暂无分数';
  // 03 费用档位（手动值优先，可恢复自动）
  renderFee(d); $('dFeeMsg').hidden = true;
  // 04 点评
  $('dComment').value = savedComment = d.comment || ''; $('dCommentMsg').hidden = true;
  // 05 替代文字
  const alts = $('dAlts'); alts.textContent = ''; savedAlts = d.images.map(im => im.alt);
  if (!d.images.length) { const p = document.createElement('p'); p.style.color = 'var(--muted)'; p.textContent = '暂无图片'; alts.append(p); }
  d.images.forEach((im, n) => {
    const f = document.createElement('div'); f.className = 'field';
    const l = document.createElement('label'); l.className = 'flabel'; l.htmlFor = 'dAlt' + n;
    l.append('第 '); const m = document.createElement('span'); m.className = 'mono'; m.textContent = n + 1; l.append(m, ' 张');
    const inp = document.createElement('input'); inp.className = 'ti'; inp.maxLength = 500; inp.id = 'dAlt' + n; inp.value = im.alt;
    const msg = document.createElement('p'); msg.className = 'im'; msg.hidden = true;
    inp.addEventListener('blur', () => saveAlt(n));
    f.append(l, inp, msg); alts.append(f);
  });
  $('dPrimary').textContent = mode === 'rescue' ? '捞回' : '留下';
  setPrimaryReason(null);
  if ((mode === 'pool' || mode === 'rank') && window.curioRank) {
    // 02 商品池 / 03 榜打开的货详情：主按钮为"加入榜"（或"移出榜"），不能上榜时 aria-disabled 并写明原因。
    const p = window.curioRank.primary(d);
    $('dPrimary').textContent = p.label; setPrimaryReason(p.reason);
  }
  $('dRecheck').textContent = '复查'; $('dRecheck').removeAttribute('aria-disabled');
  $('dRecheckMsg').hidden = true; $('dPrimaryMsg').hidden = true;
  // 标记售出（故事 13）：只对留下的货（商品池里）出现，需确认。
  $('dSoldRow').hidden = d.state !== 'kept' || !!d.sold; $('dSoldConfirm').hidden = true;
  $('dSoldMsg').textContent = d.sold ? '已标记售出（可在 02 商品池"已移出"里撤销）' : ''; $('dSoldMsg').hidden = !d.sold;
  $('dSold').setAttribute('aria-expanded', 'false');
  $('detail').hidden = false; $('app').inert = true; $('detail').scrollTop = 0;
  document.title = d.name + ' · 后台';
  $('dName').focus();
}
function setPrimaryReason(reason) {
  const b = $('dPrimary'), why = $('dPrimaryWhy');
  if (reason) { b.setAttribute('aria-disabled', 'true'); b.setAttribute('aria-describedby', 'dPrimaryWhy'); why.textContent = reason; why.hidden = false; }
  else { b.removeAttribute('aria-disabled'); b.removeAttribute('aria-describedby'); why.hidden = true; }
}
// 当前分区的页面标题与回落焦点（01 新货 / 已自动移出 / 02 商品池 / 03 榜）。
function viewTitle() {
  if (!$('matchView').hidden) return '匹配 · 后台';
  if (!$('rulesView').hidden) return '待审核 · 后台';
  if (!$('guestsView').hidden) return '客人 · 后台';
  if (!$('agreeView').hidden) return '符合度 · 后台';
  if (!$('poolView').hidden) return '商品池 · 后台';
  if (!$('rankView').hidden) return 'R12 · 后台';
  if (!$('seenView').hidden) return '看过未选 · 后台';
  if (!$('aucView').hidden) return '拍卖新品 · 后台';
  return $('arView').hidden ? '新货 · 后台' : '已自动移出 · 后台';
}
function viewFocus() {
  if (!$('matchView').hidden) return $('h04');
  if (!$('rulesView').hidden) return $('h05');
  if (!$('guestsView').hidden) return $('h06');
  if (!$('poolView').hidden) return $('h02');
  if (!$('rankView').hidden) return $('h03');
  if (!$('seenView').hidden) return $('seenTitle');
  if (!$('aucView').hidden) return $('aucTitle');
  return $('arView').hidden ? $('h01') : $('arTitle');
}
// 关闭前等未保存的点评 / 替代文字写完；任一失败则不关闭，字段下保留"未保存 · 重试"。
async function closeDetail(refocus = true) {
  if ($('detail').hidden) return false;
  const saves = [];
  if ($('dComment').value !== savedComment) saves.push(saveComment());
  if (detail) detail.images.forEach((_, n) => { const el = $('dAlt' + n); if (el && el.value !== savedAlts[n]) saves.push(saveAlt(n)); });
  const ok = (await Promise.all(saves)).every(Boolean);
  if (!ok || $('detail').hidden) return false;
  $('detail').hidden = true; $('app').inert = false;
  beh.close();                                            // 行为记录：详情停留秒数
  document.title = viewTitle();
  if (refocus) {
    const o = detailOpener;
    if (o && o.isConnected && o.offsetParent !== null) o.focus(); else viewFocus().focus();
  }
  window.dispatchEvent(new Event('curio:detail-closed'));
  return true;
}
function setGi(n) {
  const ims = detail.images; if (!ims.length) return;
  gi = (n + ims.length) % ims.length;
  const im = ims[gi], alt = ($('dAlt' + gi) || {}).value || im.alt;
  $('gImg').src = im.src; $('gImg').alt = alt;
  $('gMain').setAttribute('aria-label', alt + '，放大查看');
  $('gPg').textContent = (gi + 1) + ' / ' + ims.length;
  [...$('thumbs').querySelectorAll('button')].forEach((b, k) => b.setAttribute('aria-current', k === gi ? 'true' : 'false'));
  if (!$('zoom').hidden) { $('zImg').src = im.src; $('zImg').alt = alt; $('zPg').textContent = $('gPg').textContent; zReset(); }
}
/* ---------- 03 费用档位（故事 12） ---------- */
function feeText(t) { return t.tier === 'ask' ? '请咨询' : t.tier + ' 档 · 约 €' + t.amount + ' · ' + t.label; }
function renderFee(d) {
  const f = d.fee, box = $('dFeeOpts'); if (!f) return;
  const hadFocus = box.contains(document.activeElement);
  $('dFeeAuto').textContent = '推断：' + feeText(f.auto) + '（' + f.auto.reason + '）';
  box.textContent = '';
  (d.fee_choices || []).forEach(ch => {
    const l = document.createElement('label'); l.className = 'feeopt';
    const r = document.createElement('input'); r.type = 'radio'; r.name = 'dFee'; r.value = ch.tier; r.checked = ch.tier === f.tier;
    // 方向键在单选组里移动也会触发 change：短暂停顿后只保存最后选中的那一档。
    r.addEventListener('change', () => { if (r.checked) queueFee(ch.tier); });
    const k = document.createElement('span'); k.className = 'mono'; k.textContent = ch.tier === 'ask' ? '请咨询' : ch.tier + ' 档';
    const t = document.createElement('span'); t.textContent = ch.tier === 'ask' ? '尺寸超出或特别易碎' : '约 €' + ch.amount + ' · ' + ch.label;
    l.append(r, k, t); box.append(l);
  });
  if (hadFocus) { const c = box.querySelector('input:checked') || box.querySelector('input'); if (c) c.focus(); }
  $('dFeeNow').textContent = f.manual ? '当前：手动改为 ' + feeText(f) + '（客人页以此为准）' : '当前：按推断';
  $('dFeeReset').hidden = !f.manual;
}
let feeTimer = null, feeSeq = 0;
function queueFee(tier) {
  const id = detail.item_id;          // 计时期间换了货详情也只存到原来那件
  clearTimeout(feeTimer);
  feeTimer = setTimeout(() => saveFee(tier, id), 400);
}
async function saveFee(tier, id = detail.item_id) {
  clearTimeout(feeTimer);
  const msg = $('dFeeMsg'), before = detail && detail.fee, seq = ++feeSeq;
  const current = () => detail && detail.item_id === id && seq === feeSeq;     // 晚到的旧响应不覆盖新选择
  try {
    const res = await post('/api/triage/fee', {item_id: id, tier, request_id: rid()});
    if (!res.ok) throw new Error(res.data.error || '');
    if (current()) { detail.fee = res.data.fee; renderFee(detail); fieldMsg(msg, '已保存'); }
    setNet(false); if (current()) say('已保存');
    return true;
  } catch (e) {
    if (current()) { renderFee(Object.assign({}, detail, {fee: before})); fieldMsg(msg, saveError(e), () => saveFee(tier, id)); shout('未保存，请重试'); }
    return false;
  }
}
function fieldMsg(el, text, retryFn) {
  el.textContent = text;
  if (retryFn) { el.append(' · '); const b = document.createElement('button'); b.type = 'button'; b.className = 'lnk lnk-pad'; b.textContent = '重试'; b.onclick = retryFn; el.append(b); }
  el.hidden = false;
}
function saveError(e) { return e && e.message ? '未保存（' + e.message + '）' : '未保存'; }
async function saveComment() {
  const id = detail.item_id, text = $('dComment').value, msg = $('dCommentMsg');
  try {
    const res = await post('/api/triage/comment', {item_id: id, text});
    if (!res.ok) throw new Error(res.data.error || '');
    if (detail && detail.item_id === id) { savedComment = text; fieldMsg(msg, '已保存'); }
    setNet(false); say('已保存');
    return true;
  } catch (e) {
    if (detail && detail.item_id === id) fieldMsg(msg, saveError(e), saveComment);
    shout('未保存，请重试');
    return false;
  }
}
async function saveAlt(n) {
  const id = detail.item_id, el = $('dAlt' + n); if (!el) return true;
  const text = el.value, msg = el.parentNode.querySelector('.im');
  if (text === savedAlts[n]) return true;
  try {
    const res = await post('/api/triage/alt', {item_id: id, index: n, text});
    if (!res.ok) throw new Error(res.data.error || '');
    if (detail && detail.item_id === id) {
      savedAlts[n] = text; fieldMsg(msg, '已保存');
      if (!text.trim()) { el.value = savedAlts[n] = detail.images[n].default_alt; }
      if (n === gi) setGi(gi);
    }
    setNet(false); say('已保存');
    return true;
  } catch (e) {
    if (detail && detail.item_id === id) fieldMsg(msg, saveError(e), () => saveAlt(n));
    shout('未保存，请重试');
    return false;
  }
}
async function recheck() {
  const b = $('dRecheck'); if (b.getAttribute('aria-disabled') === 'true') return;
  const id = detail.item_id, msg = $('dRecheckMsg');
  b.textContent = '正在确认…'; b.setAttribute('aria-disabled', 'true'); msg.hidden = true;
  let text = '暂时无法确认，请稍后再试';
  try {
    const res = await post('/api/triage/recheck', {item_id: id});
    if (res.ok && res.data.outcome === 'active') text = '仍在售';
    else if (res.ok && res.data.outcome === 'gone') {
      text = '已下架';
      // 已下架的货不再留在新货队列
      if (cards.has(id) && !handled.has(id)) { dropCard(id); newCount = Math.max(0, (newCount || 0) - 1); counts(); }
    }
  } catch (e) { /* 保持“暂时无法确认” */ }
  b.textContent = '复查'; b.removeAttribute('aria-disabled');
  if (detail && detail.item_id === id) { msg.textContent = text; msg.hidden = false; }
}
async function markSold() {
  const b = $('dSoldYes'); if (b.getAttribute('aria-busy') === 'true') return;
  const id = detail.item_id, msg = $('dSoldMsg');
  const r = b.dataset.rid || rid(); b.dataset.rid = r;
  b.setAttribute('aria-busy', 'true'); msg.hidden = true;
  let res = null;
  try { res = await post('/api/pool/action', {action: 'mark_sold', item_id: id, request_id: r}); }
  catch (e) { setNet(true); }
  b.removeAttribute('aria-busy');
  if (!detail || detail.item_id !== id) return;
  if (!res || !res.ok) {
    if (res && res.status < 500) delete b.dataset.rid;
    msg.textContent = res && res.status < 500 && res.data && res.data.error ? '未保存：' + res.data.error : '未保存，请重试';
    msg.hidden = false; shout('未保存，请重试'); return;
  }
  delete b.dataset.rid; setNet(false);
  detail.state = res.data.state; detail.sold = !!res.data.sold;
  if (window.curioRank && window.curioRank.reload) window.curioRank.reload();
  $('dSoldConfirm').hidden = true; $('dSoldRow').hidden = true;
  msg.textContent = '已标记售出' + (res.data.left_rank ? '，并已移出 R12' : '') + '（可在 02 商品池"已移出"里撤销）';
  msg.hidden = false; say('已标记售出');
  if (detailMode === 'pool' || detailMode === 'rank') { $('dPrimary').textContent = '加入 R12'; setPrimaryReason('不在商品池，不能加入 R12'); }
}
function primaryMsg(text) { const m = $('dPrimaryMsg'); m.textContent = text; m.hidden = false; shout(text); }
async function primary() {
  const id = detail.item_id;
  if (detailMode === 'pool' || detailMode === 'rank') {
    const b = $('dPrimary');
    if (b.getAttribute('aria-disabled') === 'true') { say($('dPrimaryWhy').textContent); return; }
    if (b.getAttribute('aria-busy') === 'true' || !window.curioRank) return;
    b.setAttribute('aria-busy', 'true');
    const text = await window.curioRank.fromDetail(detail);
    b.removeAttribute('aria-busy');
    if (detail && detail.item_id === id) {
      const m = $('dPrimaryMsg'); m.textContent = text; m.hidden = false;
      const p = window.curioRank.primary(detail); b.textContent = p.label; setPrimaryReason(p.reason);
    }
    return;
  }
  if (detailMode === 'auction') {
    const li = [...$('aucList').children].find(x => x.dataset.id === id);
    if (li && await closeDetail(false)) { li.querySelector('[data-k="keep"]').focus(); aucAct(li, 'keep'); }
    return;                                // 列表里已没有这一行：不落到其他模式
  }
  if (detailMode === 'seen') {
    const li = [...$('seenList').children].find(x => x.dataset.id === id);
    if (li) { if (await closeDetail(false)) { li.querySelector('[data-k="keep"]').focus(); seenKeep(li); } return; }
  }
  if (detailMode === 'rescue') {
    const li = [...$('arList').children].find(x => x.dataset.id === id);
    if (li) { if (await closeDetail(false)) { li.querySelector('.rescue').focus(); rescueRow(li); } return; }
    // 列表里已没有这一行：直接按货号捞回
    try {
      const res = await send({action: 'rescue', item_id: id, request_id: rid()});
      if (!res.ok) return primaryMsg(res.status < 500 && res.data.error ? '未能捞回：' + res.data.error : '未能捞回，请重试');
      const card = applyRescued(res.data, null);
      if (await closeDetail()) say('已捞回 ' + card.name);
    } catch (e) { setNet(true); primaryMsg('未能捞回，请重试'); }
    return;
  }
  const fe = cards.get(id);
  if (fe && !handled.has(id)) {
    // 瀑布流里的卡片：关闭详情后按卡上「留下」处理（淡出、可撤销、失败行内重试）。
    if (await closeDetail(false)) { const target = nextFocus(fe.li, 'keep'); act('keep', id); (target || $('undo')).focus(); }
    return;
  }
  // 不是当前卡片（如已在别处处理过）：直接按货号留下，结果写在详情里
  try {
    const res = await send({action: 'keep', item_id: id, request_id: rid()});
    if (!res.ok) return primaryMsg(res.status < 500 && res.data.error ? '未能留下：' + res.data.error : '未能留下，请重试');
    const had = cards.has(id) && !handled.has(id);
    if (cards.has(id)) dropCard(id);
    if (had) newCount = Math.max(0, (newCount || 0) - 1);
    setNet(false); sync(res.data); counts(); updateEmpty();
    if (await closeDetail()) say('已留下，剩余 ' + newCount + ' 件');
  } catch (e) { setNet(true); primaryMsg('未能留下，请重试'); }
}

/* ---------- 放大层 ---------- */
let zs = 1, zx = 0, zy = 0, zOpener = null;
const pts = new Map(); let pinch = null, pan = null;
function zApply() { $('zImg').style.transform = 'translate(' + zx + 'px,' + zy + 'px) scale(' + zs + ')'; }
function zClamp() {
  const r = $('zStage').getBoundingClientRect();
  const mx = (zs - 1) * r.width / 2, my = (zs - 1) * r.height / 2;
  zx = Math.max(-mx, Math.min(mx, zx)); zy = Math.max(-my, Math.min(my, zy));
}
function zSet(s) { zs = Math.max(1, Math.min(6, s)); zClamp(); zApply(); }
function zReset() { zs = 1; zx = zy = 0; zApply(); }
function openZoom(opener) {
  if (!detail || !detail.images.length) return;
  zOpener = opener;
  $('zoom').hidden = false; $('detail').inert = true;
  setGi(gi); $('zImg').src = detail.images[gi].src; $('zImg').alt = $('gImg').alt; $('zPg').textContent = $('gPg').textContent;
  zReset(); $('zClose').focus();
}
function closeZoom() {
  $('zoom').hidden = true; $('detail').inert = false;
  pts.clear(); pinch = pan = null;
  if (zOpener) zOpener.focus();
}
(function zoomGestures() {
  const st = $('zStage');
  const dist = () => { const [a, b] = [...pts.values()]; return Math.hypot(a.x - b.x, a.y - b.y) || 1; };
  st.addEventListener('pointerdown', e => {
    st.setPointerCapture(e.pointerId); pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pts.size === 2) { pinch = {d: dist(), s: zs}; pan = null; }
    else if (pts.size === 1) pan = {x: e.clientX, y: e.clientY, zx, zy};
  });
  st.addEventListener('pointermove', e => {
    if (!pts.has(e.pointerId)) return;
    pts.set(e.pointerId, {x: e.clientX, y: e.clientY});
    if (pts.size === 2 && pinch) zSet(pinch.s * dist() / pinch.d);
    else if (pts.size === 1 && pan && zs > 1) { zx = pan.zx + e.clientX - pan.x; zy = pan.zy + e.clientY - pan.y; zClamp(); zApply(); }
  });
  const up = e => { pts.delete(e.pointerId); if (pts.size < 2) pinch = null; if (!pts.size) pan = null; else { const p = [...pts.values()][0]; pan = {x: p.x, y: p.y, zx, zy}; } };
  st.addEventListener('pointerup', up); st.addEventListener('pointercancel', up);
})();

/* ---------- 键盘与焦点 ---------- */
document.addEventListener('keydown', e => {
  const layer = !$('zoom').hidden ? $('zoom') : !$('detail').hidden ? $('detail') : null;
  if (layer) {
    if (e.key === 'Escape') { e.preventDefault(); if (layer === $('zoom')) closeZoom(); else closeDetail(); return; }
    if (e.key === 'Tab') {
      const f = focusables(layer); if (!f.length) return;
      const first = f[0], last = f[f.length - 1];
      if (e.shiftKey && (document.activeElement === first || !layer.contains(document.activeElement))) { e.preventDefault(); last.focus(); }
      else if (!e.shiftKey && (document.activeElement === last || !layer.contains(document.activeElement))) { e.preventDefault(); first.focus(); }
    }
    return;
  }
});

/* ---------- 绑定 ---------- */
$('undo').onclick = undo;
$('retry').onclick = retry;
$('netRetry').onclick = netRetry;
$('reload').onclick = reloadFeed;
$('openAr').onclick = openAr;
$('openSeen').onclick = openSeen;
$('emptySeenOpen').onclick = openSeen;
$('seenBack').onclick = closeSeen;
$('seenMore').onclick = () => loadSeen(false);
$('seenReload').onclick = () => loadSeen(false);
$('back').onclick = closeAr;
$('openAuc').onclick = openAuc;
$('aucBack').onclick = closeAuc;
$('aucMore').onclick = () => loadAuc(false);
$('aucReload').onclick = () => loadAuc(false);
$('arMore').onclick = () => loadAr(false);
$('arReload').onclick = () => loadAr(false);
$('dClose').onclick = () => closeDetail();
$('dComment').addEventListener('blur', () => { if ($('dComment').value !== savedComment) saveComment(); });
$('dRecheck').onclick = recheck;
$('dSold').onclick = () => { $('dSoldConfirm').hidden = false; $('dSold').setAttribute('aria-expanded', 'true'); $('dSoldYes').focus(); };
$('dSoldNo').onclick = () => { $('dSoldConfirm').hidden = true; $('dSold').setAttribute('aria-expanded', 'false'); $('dSold').focus(); };
$('dSoldYes').onclick = markSold;
$('dFeeReset').onclick = () => saveFee(null);
$('dPrimary').onclick = primary;
$('gPrev').onclick = () => setGi(gi - 1);
$('gNext').onclick = () => setGi(gi + 1);
$('gMain').onclick = () => openZoom($('gMain'));
$('gZoom').onclick = () => openZoom($('gZoom'));
$('zClose').onclick = closeZoom;
$('zIn').onclick = () => zSet(zs * 1.5);
$('zOut').onclick = () => zSet(zs / 1.5);
$('zReset').onclick = zReset;
$('zPrev').onclick = () => setGi(gi - 1);
$('zNext').onclick = () => setGi(gi + 1);
window.addEventListener('offline', () => { setNet(true); shout('网络已断开，操作未保存'); });
window.addEventListener('online', () => setNet(false));
if (onNewTab()) beh.start();
load();

/* ---------- 06 客人（故事 14，CAP-14） ----------
   待关联 / 已关联 / 已停用三个列表，邀请码行，confirm-dialog。关联只在本机（即时生效）；停用、重置、换邀请码
   入发件箱后显示"待同步"，每 3 秒读一次状态，确认后换成结果并 polite 播报。临时密码只在本页内存里，
   确认后显示一次与"复制"，离开 06 或离开页面即不可见。 */
window.curioGuests = (function () {
  const POLL = 3000;
  let data = null, active = false, loading = false, timer = null, lastJson = '';
  const temps = new Map();     // command_id -> {password, shown}
  const msgs = new Map();      // account_id -> {text, retry}
  const busy = new Set();      // account_id：写入中
  const issued = new Set();    // 本页发出的指令 command_id（确认后播报一次）
  let picker = null;           // {account_id, mode: 'link'|'relink', q, results, error}
  let prevStates = null;       // command_id -> state；邀请码 'invite' -> 当前码
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (cls, text, k) => { const b = el('button', cls, text); b.type = 'button'; if (k) b.dataset.k = k; return b; };
  const two = n => String(n).padStart(2, '0');
  function when(iso) {
    const d = new Date(iso); if (isNaN(d)) return '';
    return d.getFullYear() + '-' + two(d.getMonth() + 1) + '-' + two(d.getDate()) + ' ' + two(d.getHours()) + ':' + two(d.getMinutes());
  }
  function keyOf() { const a = document.activeElement; return a && a.dataset ? a.dataset.k : null; }
  function refocus(k, fallback) {
    const t = k && document.querySelector('[data-k="' + CSS.escape(k) + '"]');
    if (t && t.offsetParent !== null) t.focus(); else if (fallback) fallback.focus();
  }

  /* ---------- 读取与轮询 ---------- */
  let again = null;            // 读取进行中又要读：结束后再读一次（refresh 优先）
  async function load(refresh) {
    if (loading) { again = again || refresh ? true : false; return; }
    loading = true;
    try {
      const d = await get('/api/guests' + (refresh ? '?refresh=1' : ''));
      $('gErr').hidden = true;
      if (refresh && d.pull) $('gPullNote').hidden = d.pull.accounts_status !== 'error';
      absorb(d);
    } catch (e) {
      if (active) { $('gErr').hidden = false; shout('未能读取客人'); }
    } finally { loading = false; }
    if (again !== null) { const r = again; again = null; return load(r); }
    schedule();
  }
  function pendingAny() {
    if (!data) return false;
    if (data.invite.pending && data.invite.pending.state === 'pending') return true;
    return ['pending', 'linked', 'disabled'].some(l => data[l].some(r =>
      (r.disable && r.disable.state === 'pending') || (r.reset && r.reset.state === 'pending')));
  }
  function schedule() {
    clearTimeout(timer); timer = null;
    if (pendingAny()) timer = setTimeout(() => load(false), POLL);
  }
  function absorb(d) {
    // 待同步 → 已生效：播报结果；重置的临时密码此刻才显示。
    const now = new Map();
    ['pending', 'linked', 'disabled'].forEach(l => d[l].forEach(r => {
      if (r.disable && r.disable.command_id) now.set(r.disable.command_id, {state: r.disable.state, kind: 'disable', r});
      if (r.reset) now.set(r.reset.command_id, {state: r.reset.state, kind: 'reset', r});
    }));
    now.forEach((v, id) => {
      const before = prevStates ? prevStates.get(id) : undefined;
      const watched = before === 'pending' || issued.has(id);
      if (!watched || before === v.state) return;
      if (v.state === 'done') {
        if (v.kind === 'reset' && temps.has(id)) temps.get(id).shown = true;
        say(v.kind === 'disable' ? '已停用' : '已重置');
        issued.delete(id);
      } else if (v.state === 'failed') {
        shout('未同步，请处理'); issued.delete(id);
      }
    });
    if (prevStates) {
      const was = prevStates.get('invite'), cur = d.invite.current && d.invite.current.code;
      if (was !== undefined && cur && was !== cur) say('邀请码已更换');
    }
    prevStates = new Map([...now].map(([id, v]) => [id, v.state]));
    prevStates.set('invite', d.invite.current ? d.invite.current.code : null);
    data = d;
    $('cGuests').textContent = d.count;
    const json = JSON.stringify(d);
    if (json !== lastJson) { lastJson = json; render(); }
  }

  /* ---------- 绘制 ---------- */
  function render() {
    if (!data) return;
    const k = keyOf(), typing = document.activeElement && document.activeElement.id === 'gPickQ';
    renderInvite();
    $('gNoGc').hidden = data.gc.available;
    $('gPulled').textContent = (data.pulled_at ? '名单更新于 ' + when(data.pulled_at) : '名单尚未从服务器更新') +
      (data.pull_error ? ' · 上次更新失败：' + data.pull_error : '');
    fill($('gPending'), data.pending, 'pending');
    $('gPendN').textContent = data.pending.length;
    $('gPendEmpty').hidden = data.pending.length > 0;
    $('gLinkedN').textContent = data.linked.length;
    filterLinked();
    fill($('gDisabled'), data.disabled, 'disabled');
    $('gDisN').textContent = data.disabled.length;
    if (typing) { const q = $('gPickQ'); if (q) { q.focus(); q.setSelectionRange(q.value.length, q.value.length); } }
    else refocus(k);
  }
  function filterLinked() {
    const q = $('gFilter').value.trim().toLowerCase();
    const rows = q ? data.linked.filter(r => r.nickname.toLowerCase().includes(q) || r.username.toLowerCase().includes(q)) : data.linked;
    fill($('gLinked'), rows, 'linked');
    $('gFilterNone').textContent = q && !rows.length && data.linked.length ? '没有找到 ' + $('gFilter').value.trim() : '';
  }
  function fill(list, rows, kind) { list.replaceChildren(...rows.map(r => row(r, kind))); }
  function tag(text) { return el('span', 'tag', text); }
  function row(r, kind) {
    const li = el('li', 'grow'); li.dataset.id = r.account_id;
    const n = el('p', 'gn'); n.append(r.nickname, ' · ', el('span', 'mono', r.username)); li.append(n);
    const m = el('p', 'gm', '注册 ' + when(r.created_at));
    if (r.customer_id) m.append(' · ', el('span', 'mono', r.customer_id), r.customer_name ? ' ' + r.customer_name : '');
    li.append(m);
    const st = el('p', 'gst');
    const dis = r.disable, rs = r.reset;
    if (kind === 'disabled') st.append(tag('已停用'));
    else if (dis && dis.state === 'pending') st.append(tag('停用 · 待同步'));
    if (kind !== 'disabled' && rs && rs.state === 'pending') st.append(tag('重置密码 · 待同步'));
    if (kind !== 'disabled' && rs && rs.state === 'done' && temps.has(rs.command_id)) st.append(tag('已重置'));
    if (st.childNodes.length) li.append(st);
    if (kind !== 'disabled' && rs && rs.state === 'done' && !temps.has(rs.command_id)) {
      li.append(el('p', 'gm', '已重置 · 临时密码已不可见，如需请再重置一次'));
    }
    const fails = kind === 'disabled' ? [] : [[dis, '停用'], [rs, '重置密码']].filter(([c]) => c && c.state === 'failed');
    if (fails.length) {
      const f = el('p', 'im'); f.setAttribute('role', 'alert');
      f.append(fails.map(([c, w]) => w + '未同步' + (c.error ? '（' + c.error + '）' : '')).join('；') +
               '。同步（含发布）已暂停，需用 scripts/outbox.py 处理。');
      li.append(f);
    }
    if (rs && rs.state === 'done' && temps.has(rs.command_id) && temps.get(rs.command_id).shown && kind !== 'disabled') li.append(tempBox(r, temps.get(rs.command_id)));
    const msg = msgs.get(r.account_id);
    if (msg) {
      const p = el('p', 'im'); p.setAttribute('role', 'alert'); p.append(msg.text);
      if (msg.retry) { const b = btn('lnk lnk-pad', '重试', r.account_id + ':retry'); b.onclick = msg.retry; p.append(' · ', b); }
      li.append(p);
    }
    if (kind === 'disabled') return li;
    const acts = el('div', 'acts2');
    const isBusy = busy.has(r.account_id);
    if (kind === 'pending') {
      const a = btn('btn', '关联到已有客户', r.account_id + ':link'); a.onclick = () => openPicker(r, 'link');
      const c = btn('btn', isBusy ? '正在新建…' : '新建档案', r.account_id + ':create'); c.onclick = () => create(r);
      if (!data.gc.available) { a.setAttribute('aria-disabled', 'true'); c.setAttribute('aria-disabled', 'true'); a.onclick = c.onclick = () => shout('找不到 GoodCustomer 资料库'); }
      if (isBusy) c.setAttribute('aria-disabled', 'true');
      acts.append(a, c);
    } else {
      const a = btn('btn', '更正关联', r.account_id + ':relink'); a.onclick = () => openPicker(r, 'relink');
      if (!data.gc.available) { a.setAttribute('aria-disabled', 'true'); a.onclick = () => shout('找不到 GoodCustomer 资料库'); }
      const u = btn('btn', '解除关联', r.account_id + ':unlink'); u.onclick = () => confirmUnlink(r, u);
      acts.append(a, u);
    }
    li.append(acts);
    if (picker && picker.account_id === r.account_id) li.append(pickerBox(r));
    const x = el('p', 'gx');
    if (fails.length) return li;   // 上一条指令未同步：先处理，不再排新的
    if (kind === 'linked') {
      const resetBusy = rs && rs.state === 'pending';
      const rb = btn('lnk lnk-pad', '重置密码', r.account_id + ':reset');
      if (resetBusy) rb.setAttribute('aria-disabled', 'true');
      rb.onclick = () => resetBusy ? shout('上一次重置还在待同步') : confirmReset(r, rb);
      x.append(rb);
    }
    if (!(dis && dis.state === 'pending')) {
      const db = btn('lnk lnk-pad', '停用账号', r.account_id + ':disable'); db.onclick = () => confirmDisable(r, db);
      x.append(db);
    }
    if (x.childNodes.length) li.append(x);
    return li;
  }
  function tempBox(r, t) {
    const box = el('div', 'tmp');
    box.append(el('p', null, '临时密码（只显示这一次，请在微信里发给' + r.nickname + '）'));
    const line = el('p'); const code = el('span', 'mono', t.password);
    const cp = btn('lnk lnk-pad', '复制', r.account_id + ':copy');
    const note = el('p', 'im'); note.hidden = true;
    cp.onclick = async () => {
      try { await navigator.clipboard.writeText(t.password); note.textContent = '已复制临时密码'; note.hidden = false; say('已复制临时密码'); }
      catch (e) {
        const inp = el('input', 'ti mono'); inp.readOnly = true; inp.value = t.password; inp.setAttribute('aria-label', '临时密码');
        note.replaceChildren('未能自动复制，请长按选择临时密码', inp); note.hidden = false;
        inp.focus(); inp.select(); shout('未能自动复制，请手动复制');
      }
    };
    line.append(code, ' ', cp); box.append(line, note);
    return box;
  }

  /* ---------- 关联：搜索档案 ---------- */
  function openPicker(r, mode) {
    picker = {account_id: r.account_id, mode, q: '', results: [], error: ''};
    msgs.delete(r.account_id); render();
    const q = $('gPickQ'); if (q) q.focus();
  }
  function closePicker(r) { picker = null; render(); refocus(r.account_id + ':' + (r.customer_id ? 'relink' : 'link')); }
  let searchTimer = null, searchSeq = 0;
  function pickerBox(r) {
    const box = el('div', 'pick');
    const lab = el('label', 'flabel', picker.mode === 'relink' ? '改到哪位客户（按微信名、显示名、别名或编号搜）' : '关联到哪位客户（按微信名、显示名、别名或编号搜）');
    lab.htmlFor = 'gPickQ';
    const q = el('input', 'ti'); q.id = 'gPickQ'; q.type = 'search'; q.autocomplete = 'off'; q.spellcheck = false; q.value = picker.q;
    q.oninput = () => { picker.q = q.value; clearTimeout(searchTimer); searchTimer = setTimeout(runSearch, 200); };
    q.onkeydown = e => { if (e.key === 'Escape') { e.preventDefault(); closePicker(r); } };
    const st = el('p', 'gnone'); st.setAttribute('role', 'status');
    const ul = el('ul');
    if (picker.error) st.textContent = picker.error;
    else if (picker.q.trim() && !picker.results.length) st.textContent = '没有找到 ' + picker.q.trim();
    picker.results.forEach(c => {
      const li = el('li'); const b = btn(null, null, r.account_id + ':pick:' + c.customer_id);
      b.append(el('span', 'mono', c.customer_id), ' ' + c.display_name + (c.aliases.length ? '（' + c.aliases.join('、') + '）' : ''));
      b.onclick = () => choose(r, c, b);
      li.append(b); ul.append(li);
    });
    const cancel = btn('lnk lnk-pad', '取消', r.account_id + ':pickcancel'); cancel.onclick = () => closePicker(r);
    box.append(lab, q, st, ul, cancel);
    return box;
  }
  async function runSearch() {
    if (!picker) return;
    const mine = ++searchSeq, q = picker.q.trim();
    if (!q) { picker.results = []; picker.error = ''; render(); return; }
    try {
      const r = await fetch('/api/guests/search?q=' + encodeURIComponent(q));
      const d = await r.json();
      if (mine !== searchSeq || !picker) return;
      picker.results = r.ok ? d.results : []; picker.error = r.ok ? '' : (d.error || '搜索失败');
    } catch (e) { if (picker) { picker.results = []; picker.error = '网络已断开，搜索失败'; } }
    render();
  }
  function choose(r, c, opener) {
    if (busy.has(r.account_id)) return;
    if (picker.mode === 'relink') {
      confirmDlg('更正关联', r.nickname + '（' + r.username + '）将从 ' + r.customer_id + ' 改到 ' + c.customer_id + ' ' + c.display_name + '。', '确认更正', opener,
        () => write(r, {action: 'relink', account_id: r.account_id, customer_id: c.customer_id}));
    } else write(r, {action: 'link', account_id: r.account_id, customer_id: c.customer_id});
  }
  function create(r) {
    if (busy.has(r.account_id)) return;
    write(r, {action: 'create', account_id: r.account_id});
  }
  function confirmUnlink(r, opener) {
    confirmDlg('解除关联', r.nickname + '（' + r.username + '）与 ' + r.customer_id + ' 解除关联后回到待关联。', '确认解除', opener,
      () => write(r, {action: 'unlink', account_id: r.account_id}));
  }

  /* ---------- 写入（幂等 request_id；失败行内"未保存 · 重试"，重试用同一个 request_id） ---------- */
  async function write(r, body, rq) {
    if (busy.has(r.account_id)) return;
    const request_id = rq || rid();
    busy.add(r.account_id); msgs.delete(r.account_id); render();
    let res;
    try { res = await post('/api/guests/link', Object.assign({request_id}, body)); }
    catch (e) { res = null; }
    busy.delete(r.account_id);
    if (!res || !res.ok) {
      const text = res && res.status === 400 && res.data.error ? res.data.error : '未保存';
      msgs.set(r.account_id, {text, retry: () => write(r, body, request_id)});
      shout(text === '未保存' ? '未保存，请重试' : text);
      render(); refocus(r.account_id + ':retry');
      return;
    }
    picker = null;
    const d = res.data;
    const next = nextFocus(r);
    say(body.action === 'unlink' ? '已解除关联' : d.action === 'create' ? '已新建档案 ' + d.customer_id + ' 并关联' :
        (body.action === 'relink' ? '已更正关联到' : '已关联到') + (d.display_name || d.customer_id));
    await load(false);
    refocus(next, body.action === 'unlink' ? $('gPendH') : body.action === 'relink' ? $('gLinkedH') : $('gPendH'));
  }
  function nextFocus(r) {
    // 行移走后焦点落到同列表下一行（否则上一行）的第一个按钮。
    const li = document.querySelector('li.grow[data-id="' + CSS.escape(r.account_id) + '"]');
    const sib = li && (li.nextElementSibling || li.previousElementSibling);
    const b = sib && sib.querySelector('button[data-k]');
    return b ? b.dataset.k : r.account_id + ':relink';
  }

  /* ---------- 停用 / 重置密码（AD-24 指令） ---------- */
  function confirmDisable(r, opener) {
    confirmDlg('停用账号', '停用后 ' + r.nickname + '（' + r.username + '）现有登录立即失效，再登录只看到停用提示，不再记录浏览。停用不可恢复。', '确认停用', opener,
      () => commandWrite(r, 'disable_account'));
  }
  function confirmReset(r, opener) {
    confirmDlg('重置密码', '重置后该客人所有设备都会退出登录。临时密码在服务器确认后显示一次。', '确认重置', opener,
      () => commandWrite(r, 'reset_password'));
  }
  async function commandWrite(r, action, rq) {
    const request_id = rq || rid();
    msgs.delete(r.account_id);
    let res;
    try { res = await post('/api/guests/command', {action, account_id: r.account_id, request_id}); }
    catch (e) { res = null; }
    if (!res || !res.ok) {
      const text = res && res.status === 400 && res.data.error ? res.data.error : '未保存';
      msgs.set(r.account_id, {text, retry: () => commandWrite(r, action, request_id)});
      shout(text === '未保存' ? '未保存，请重试' : text);
      render(); refocus(r.account_id + ':retry');
      return;
    }
    const d = res.data;
    if (d.temp_password) temps.set(d.command_id, {password: d.temp_password, shown: false});
    if (!d.replayed) issued.add(d.command_id);
    if (d.password_lost) msgs.set(r.account_id, {text: '未能取得临时密码，请再重置一次'});
    await load(false);
    refocus(r.account_id + ':' + (action === 'reset_password' ? 'reset' : 'relink'), $('gPendH'));
  }

  /* ---------- 邀请码 ---------- */
  function renderInvite() {
    const inv = data.invite;
    $('invCode').textContent = inv.current ? inv.current.code : '未设置';
    const p = inv.pending;
    $('invNew').hidden = !p;
    if (p) { $('invNewCode').textContent = p.code; $('invTag').textContent = p.state === 'failed' ? '未同步' : '待同步'; }
    $('invFail').hidden = !(p && p.state === 'failed');
    $('invFail').textContent = p && p.state === 'failed' ? '新邀请码未同步 · 请处理（scripts/outbox.py list）' : '';
  }
  function openInvite() {
    $('invForm').hidden = false; $('invChange').setAttribute('aria-expanded', 'true');
    $('invMsg').hidden = true; $('invInput').value = ''; $('invInput').focus();
  }
  function closeInvite() {
    $('invForm').hidden = true; $('invChange').setAttribute('aria-expanded', 'false'); $('invChange').focus();
  }
  let invRid = null, invLast = '';
  function saveInvite() {
    const code = $('invInput').value.trim();
    if (!/^[A-Za-z0-9_-]{4,32}$/.test(code)) {
      $('invMsg').textContent = '邀请码须为 4–32 位字母、数字、下划线或连字符'; $('invMsg').hidden = false;
      $('invInput').setAttribute('aria-invalid', 'true'); $('invInput').focus(); return;
    }
    $('invInput').removeAttribute('aria-invalid');
    confirmDlg('更换邀请码', '新邀请码 ' + code + '：旧邀请码将立即失效，已注册的客人不受影响。服务器确认前旧码仍有效。', '确认更换', $('invSave'), async () => {
      if (code !== invLast) { invRid = rid(); invLast = code; }
      let res;
      try { res = await post('/api/guests/invite', {code, request_id: invRid}); } catch (e) { res = null; }
      if (!res || !res.ok) {
        $('invMsg').textContent = res && res.status === 400 && res.data.error ? res.data.error : '未保存 · 请重试';
        $('invMsg').hidden = false; shout('未保存，请重试'); $('invSave').focus(); return;
      }
      invRid = null; invLast = '';
      closeInvite(); await load(false);
    });
  }

  /* ---------- confirm-dialog（role=alertdialog；焦点先在"取消"；困在框内；Esc / 取消关闭并回到触发处） ---------- */
  let dlgOpener = null, dlgYes = null;
  function confirmDlg(title, text, yes, opener, onYes) {
    dlgOpener = opener; dlgYes = onYes;
    $('cdT').textContent = title; $('cdD').textContent = text; $('cdYes').textContent = yes;
    $('cdlg').hidden = false; $('cdNo').focus();
  }
  function closeDlg(run) {
    $('cdlg').hidden = true;
    const fn = dlgYes, opener = dlgOpener; dlgYes = dlgOpener = null;
    if (opener && opener.isConnected) opener.focus(); else if (opener && opener.dataset && opener.dataset.k) refocus(opener.dataset.k);
    if (run && fn) fn();
  }
  document.addEventListener('keydown', e => {
    if ($('cdlg').hidden) return;
    if (e.key === 'Escape') { e.preventDefault(); e.stopPropagation(); closeDlg(false); return; }
    if (e.key === 'Tab') {
      const f = [$('cdNo'), $('cdYes')];
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[1]) { e.preventDefault(); f[0].focus(); }
      else if (!f.includes(document.activeElement)) { e.preventDefault(); f[0].focus(); }
    }
  }, true);
  $('cdNo').onclick = () => closeDlg(false);
  $('cdYes').onclick = () => closeDlg(true);
  $('invChange').onclick = () => $('invForm').hidden ? openInvite() : closeInvite();
  $('invCancel').onclick = closeInvite;
  $('invSave').onclick = saveInvite;
  $('invInput').addEventListener('keydown', e => { if (e.key === 'Enter') { e.preventDefault(); saveInvite(); } });
  $('gFilter').addEventListener('input', () => { if (data) filterLinked(); });
  $('gReload').onclick = () => load(true);
  $('gPullRetry').onclick = () => load(true);

  // 计数：打开后台时读一次（不拉服务器）。
  load(false);
  return {
    confirmDialog: confirmDlg, // 05 停用规律也用这个确认框
    open() { active = true; load(true); },
    leave() {
      active = false;
      // 已显示过的临时密码离开 06 即不可再见。
      [...temps].forEach(([id, t]) => { if (t.shown) temps.delete(id); });
      if (!$('cdlg').hidden) closeDlg(false);
      if (data) { lastJson = ''; render(); }
    }
  };
})();

/* ---------- 04 匹配（故事 16，CAP-5） ----------
   按客人一行（称呼 + 配上件数；按待处理件数排），点开展开逐件列表：缩略图、货号、标准名称、价格、
   "对上：品牌 · 图案 · 器型"与需求一句（故事 29：只用近一年的需求，按一致性排）；点"看原话"展开这条需求的客人原话
   （证据摘录原文，带日期，客人说的在前，最多 5 条；没有时显示"没有找到原话"）；每件"确认"或"不合适"（带 request_id；网络或服务端失败行内"未保存 · 重试"，重试沿用同一个 request_id；
   400 类拒绝只显示原因并重新读取）。不合适之后该行变成"已标不合适 · 撤销"，直到下一次读取。确认后出现
   "复制单件页链接"：链接 = 站点地址 + /item/CO-NNNN（不按人区分）；该件不在已送达的客人页上（未发布或已隐藏）时
   aria-disabled，原因"请先发布这件货"。剪贴板失败时显示已全选的只读输入框。 */
window.curioMatches = (function () {
  let data = null, active = false, inflight = null, again = false, lastJson = '';
  const expanded = new Set();  // 展开的客人 customer_id
  const quoted = new Set();    // 展开了原话的 客人|货
  const msgs = new Map();      // 客人|货 -> {text, retry, input}
  const busy = new Set();      // 客人|货：写入中
  const rids = new Map();      // 客人|货|决定 -> request_id（失败重试沿用）
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (cls, text, k) => { const b = el('button', cls, text); b.type = 'button'; if (k) b.dataset.k = k; return b; };
  const keyOf = () => { const a = document.activeElement; return a && a.dataset ? a.dataset.k : null; };
  const pairKey = (c, it) => c.customer_id + '|' + it.item_id;
  function find(k) { return k ? document.querySelector('[data-k="' + CSS.escape(k) + '"]') : null; }
  function refocus(k, fallback) {
    const t = find(k);
    if (t && t.offsetParent !== null) t.focus(); else if (fallback) fallback.focus();
  }

  // 读取进行中又要读：共用同一个 promise，结束后再读一次；调用方 await 到最新的那次读完。
  function load() {
    if (inflight) { again = true; return inflight; }
    inflight = (async () => {
      try {
        const d = await get('/api/match');
        $('mErr').hidden = true;
        absorb(d);
      } catch (e) {
        $('cMatch').textContent = '—';
        if (active) { $('mErrText').textContent = e && e.message && !(e instanceof TypeError) && !(e instanceof SyntaxError) ? '未能读取匹配（' + e.message + '）' : '未能读取匹配'; $('mErr').hidden = false; shout('未能读取匹配'); }
      }
    })().then(() => {
      inflight = null;
      if (again) { again = false; return load(); }
    });
    return inflight;
  }
  function recount() {
    data.count = data.customers.filter(c => c.items.some(it => it.decision === null && !it._rejected)).length;
    $('cMatch').textContent = data.available ? data.count : '—';
  }
  function absorb(d) {
    data = d;
    $('cMatch').textContent = d.available ? d.count : '—';
    const json = JSON.stringify(d);
    if (json !== lastJson) { lastJson = json; render(); }
  }

  /* ---------- 绘制 ---------- */
  function render() {
    if (!data) return;
    const k = keyOf();
    $('mNoGc').hidden = data.available;
    $('mNoSite').hidden = !data.available || data.site;
    if (data.site_problem) $('mNoSite').textContent = data.site_problem.replace('暂不能复制链接', '暂不能复制单件页链接。');
    const notes = [];
    if (data.warnings) notes.push('GoodCustomer 资料里有 ' + data.warnings + ' 行无法读取，已跳过');
    if (data.unreadable) notes.push(data.unreadable + ' 个资料文件读不了，已跳过');
    $('mNote').textContent = notes.join('；');
    $('mList').replaceChildren(...data.customers.map(row));
    $('mEmpty').hidden = !data.available || data.customers.length > 0;
    refocus(k);
  }
  function row(c) {
    const li = el('li', 'mrow'); li.dataset.id = c.customer_id;
    const open = expanded.has(c.customer_id);
    const h = btn('mhead', null, c.customer_id + ':head');
    h.setAttribute('aria-expanded', String(open));
    const n = el('span', 'mn', c.name);
    const cnt = el('span', 'mc'); cnt.append('配上 ', el('span', 'mono', c.total), ' 件');
    if (!c.pending) cnt.append(' · 已处理');
    else if (c.pending !== c.total) cnt.append(' · 待处理 ', el('span', 'mono', c.pending));
    h.append(n, cnt);
    h.onclick = () => { if (expanded.has(c.customer_id)) expanded.delete(c.customer_id); else expanded.add(c.customer_id); render(); };
    li.append(h);
    if (open) {
      const ul = el('ul', 'arlist'); ul.id = 'mItems-' + c.customer_id; ul.setAttribute('aria-label', c.name + ' 配上的货');
      h.setAttribute('aria-controls', ul.id);
      c.items.forEach(it => ul.append(it._rejected ? rejectedRow(c, it) : itemRow(c, it)));
      li.append(ul);
    }
    return li;
  }
  function rejectedRow(c, it) {
    const k = pairKey(c, it);
    const li = el('li', 'mi'); li.dataset.pair = k;
    const body = el('div'); body.style.gridColumn = '1 / -1';
    const p = el('p', 'why'); p.append(it.name + ' · 已标不合适 · ');
    const u = btn('lnk lnk-pad', busy.has(k) ? '正在撤销…' : '撤销', k + ':undo'); u.onclick = () => decide(c, it, 'undo');
    p.append(u); body.append(p);
    const m = msgs.get(k);
    if (m) body.append(msgBox(k, m));
    li.append(body);
    return li;
  }
  function msgBox(k, m) {
    const box = el('p', 'im'); box.append(m.text);
    if (m.retry) { const r = btn('lnk lnk-pad', '重试', k + ':retry'); r.onclick = m.retry; box.append(' · ', r); }
    if (m.input) {
      const inp = el('input', 'ti mono'); inp.readOnly = true; inp.value = m.input; inp.dataset.k = k + ':linkbox';
      inp.setAttribute('aria-label', '单件页链接'); box.append(inp);
    }
    return box;
  }
  function itemRow(c, it) {
    const k = pairKey(c, it), isBusy = busy.has(k);
    const li = el('li', 'mi'); li.dataset.pair = k;
    const th = btn('th', null, k + ':th'); th.setAttribute('aria-label', it.name + '，打开货详情');
    if (it.image) { const im = el('img'); im.src = it.image; im.alt = ''; th.append(im); }
    th.onclick = () => openDetail(it.item_id, 'pool', th);
    const body = el('div');
    const t = btn('t', null, k + ':name'); t.textContent = it.name; setLang(t, it.name_lang);
    (it.tags || []).forEach(x => t.append(tagEl(x)));
    t.onclick = () => openDetail(it.item_id, 'pool', t);
    const p = el('p', 'p'); p.append(el('span', 'mono', it.code || ''), ' · ', el('span', 'mono', it.price));
    if (!it.on_sale) p.append(' · 原平台已下架');
    const why = el('p', 'why');
    if (it.decision === 'confirmed') why.append(tagEl('已确认'));
    why.append('对上：' + (it.dims || []).join(' · '));
    const need = el('p', 'why mneed', '需求：' + (it.need_text || ''));
    const qOpen = quoted.has(k);
    const qb = btn('lnk lnk-pad', qOpen ? '收起原话' : '看原话', k + ':quotes');
    const qid = 'mQ-' + k.replace(/[^A-Za-z0-9_-]/g, '_');
    qb.setAttribute('aria-expanded', String(qOpen)); if (qOpen) qb.setAttribute('aria-controls', qid);  // 收起时原话不在页面上
    qb.onclick = () => { if (quoted.has(k)) quoted.delete(k); else quoted.add(k); render(); };
    need.append(' · ', qb);
    body.append(t, p, why, need);
    if (qOpen) {
      const qs = it.quotes || [];
      if (!qs.length) { const none = el('p', 'why mquotes', '没有找到原话'); none.id = qid; body.append(none); }
      else {
        const ol = el('ul', 'mquotes'); ol.id = qid; ol.setAttribute('aria-label', '客人原话');
        qs.forEach(q => {
          const li2 = el('li');
          if (q.date) li2.append(el('span', 'mono', q.date), ' ');
          if (!q.customer) li2.append((q.who || '对方') + '：');
          li2.append(q.text);
          ol.append(li2);
        });
        body.append(ol);
      }
    }
    const acts = el('div', 'acts2');
    const no = btn('btn', '不合适', k + ':reject'); no.onclick = () => decide(c, it, 'rejected');
    let w = null;
    if (it.decision === 'confirmed') {
      const cp = btn('btn', '复制单件页链接', k + ':copy');
      if (it.link) cp.onclick = () => copy(c, it);
      else {
        w = el('p', 'why', it.link_reason); w.id = 'mWhy-' + k.replace(/[^A-Za-z0-9_-]/g, '_');
        cp.setAttribute('aria-disabled', 'true'); cp.setAttribute('aria-describedby', w.id);
        cp.onclick = () => shout(it.link_reason);
      }
      acts.append(cp, no);
    } else {
      const ok = btn('btn', isBusy ? '正在保存…' : '确认', k + ':confirm'); ok.onclick = () => decide(c, it, 'confirmed');
      acts.append(ok, no);
    }
    body.append(acts);
    if (w) body.append(w);
    if (isBusy) acts.querySelectorAll('button').forEach(b => b.setAttribute('aria-disabled', 'true'));
    const m = msgs.get(k);
    if (m) body.append(msgBox(k, m));
    li.append(th, body);
    return li;
  }

  /* ---------- 确认 / 不合适 / 撤销 ---------- */
  // 新的一次操作：之前的成功 / 复制提示收起（失败待重试的保留）。
  function settle() { [...msgs].forEach(([k, m]) => { if (!m.retry) msgs.delete(k); }); }
  async function decide(c, it, decision) {
    const k = pairKey(c, it);
    if (busy.has(k)) return;
    const rk = k + '|' + decision;
    const request_id = rids.get(rk) || rid(); rids.set(rk, request_id);
    busy.add(k); msgs.delete(k); settle(); render();
    let res;
    try { res = await post('/api/match/decide', {customer_id: c.customer_id, item_id: it.item_id, decision, request_id}); }
    catch (e) { res = null; }
    busy.delete(k);
    if (res && res.status === 400) {
      // 拒绝（已不匹配、已不在商品池…）：重试不会成功——显示原因并重新读取。
      rids.delete(rk);
      const text = '未保存（' + (res.data.error || '请刷新本页') + '）';
      msgs.set(k, {text}); shout(text);
      await load(); render(); refocus(k + ':confirm', $('h04'));
      return;
    }
    if (!res || !res.ok) {
      msgs.set(k, {text: '未保存', retry: () => decide(c, it, decision)});
      shout('未保存，请重试'); render(); refocus(k + ':retry');
      return;
    }
    rids.delete(rk);
    if (decision === 'rejected') {
      // 先留在原位显示"已标不合适 · 撤销"，下一次读取后才离开列表。
      it._rejected = true; recount(); say('已标为不合适'); lastJson = ''; render(); refocus(k + ':undo', $('h04'));
      return;
    }
    say(decision === 'confirmed' ? '已确认' : '已撤销不合适');
    await load();
    refocus(k + (decision === 'confirmed' ? ':copy' : ':confirm'), $('h04'));
  }
  async function copy(c, it) {
    const k = pairKey(c, it);
    settle();
    try {
      await navigator.clipboard.writeText(it.link);
      msgs.set(k, {text: '已复制单件页链接 · 去微信发给' + c.name});
      say('已复制单件页链接'); render(); refocus(k + ':copy');
    } catch (e) {
      msgs.set(k, {text: '未能自动复制，请长按选择链接', input: it.link});
      shout('未能自动复制链接，请手动复制'); render();
      const inp = find(k + ':linkbox'); if (inp) { inp.focus(); inp.select(); }
    }
  }

  $('mReload').onclick = () => load();
  window.addEventListener('curio:detail-closed', () => { if (active) load(); });
  load();                      // 计数：打开后台时读一次
  return {
    open() { active = true; load(); },
    // 切到其它分区：收起提示，并刷新 04 计数（01/02 的改动会影响匹配）。
    leave() { active = false; if (msgs.size) { msgs.clear(); if (data) { lastJson = ''; render(); } } load(); }
  };
})();

/* ---------- 05 待审核（故事 17，CAP-8、FR-19） ----------
   每条 rule-card：规律一句、依据件数、最近一次时间；"确认"/"否决"均为最终操作、无撤销（带 request_id；
   网络或服务端（5xx）失败行内"未保存 · 重试"，重试沿用同一个 request_id；4xx 拒绝不可重试，只显示原因并重新读取）。
   成功后条目移出、焦点移到下一条，polite 播报"已确认规律"/"已否决规律"。下方"已生效的规律"可"停用"（需确认）。
   打开 05 时先让 Mac 归纳一次；计数（新规律）只改分区上的数字，不弹窗、不播报。 */
window.curioRules = (function () {
  let data = null, active = false, inflight = null, again = false, wantRefresh = false;
  const msgs = new Map();      // 'p'+id / 'a'+id -> {text, retry}
  const busy = new Set();      // 写入中的规律 id
  const rids = new Map();      // 规律 id|决定 -> request_id（失败重试沿用）
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const btn = (cls, text, k) => { const b = el('button', cls, text); b.type = 'button'; if (k) b.dataset.k = k; return b; };
  const keyOf = () => { const a = document.activeElement; return a && a.dataset ? a.dataset.k : null; };
  function find(k) { return k ? document.querySelector('#rulesView [data-k="' + CSS.escape(k) + '"]') : null; }
  function refocus(k, fallback) {
    const t = find(k);
    if (t && t.offsetParent !== null) t.focus(); else if (fallback) fallback.focus();
  }
  function load(refresh) {
    if (refresh) wantRefresh = true;
    if (inflight) { again = true; return inflight; }
    inflight = (async () => {
      const r = wantRefresh; wantRefresh = false;
      try {
        const d = await get('/api/rules' + (r ? '?refresh=1' : ''));
        $('rErr').hidden = true;
        absorb(d);
      } catch (e) {
        if (!data) $('cRules').textContent = '—';
        if (active) { $('rErr').hidden = false; shout('未能读取待审核'); }
      }
    })().then(() => {
      inflight = null;
      if (again) { again = false; return load(); }
    });
    return inflight;
  }
  function absorb(d) {
    data = d;
    $('cRules').textContent = d.count;    // 只改数字，不播报
    $('rNote').textContent = d.refresh_error ? '这次未能重新归纳（' + d.refresh_error + '），显示的是上次的待审核' : '';
    if (d.config) {
      $('rMin').textContent = d.config.min_count;
      $('rRatio').textContent = Math.round(d.config.min_ratio * 100) + '%';
    }
    render();
  }
  function render() {
    if (!data) return;
    const k = keyOf();
    $('rList').replaceChildren(...data.items.map(card));
    $('rEmpty').hidden = data.items.length > 0;
    const live = data.active || [];
    $('rActH').hidden = $('rActive').hidden = !live.length;
    $('rActN').textContent = live.length;
    $('rActive').replaceChildren(...live.map(activeRow));
    refocus(k);
  }
  function msgBox(mk) {
    const m = msgs.get(mk);
    if (!m) return null;
    const box = el('p', 'im'); box.append(m.text);
    if (m.retry) { const rb = btn('lnk lnk-pad', '重试', mk + ':retry'); rb.onclick = m.retry; box.append(' · ', rb); }
    return box;
  }
  function card(r) {
    const li = el('li', 'rcard'); li.dataset.id = r.id;
    const s = el('p', 'rs', r.sentence); s.id = 'rS-' + r.id;
    const why = el('p', 'why'); why.append('依据 ', el('span', 'mono', r.count), ' 件');
    const t = r.last_at ? fmtTime(r.last_at) : '';
    if (t) why.append(' · 最近一次 ', el('span', 'mono', t));
    const acts = el('div', 'acts2');
    const isBusy = busy.has(r.id);
    const ok = btn('btn primary', isBusy ? '正在保存…' : '确认', 'p' + r.id + ':confirm'); ok.onclick = () => decide(r, 'confirm');
    const no = btn('btn', '否决', 'p' + r.id + ':reject'); no.onclick = () => decide(r, 'reject');
    [ok, no].forEach(b => { b.setAttribute('aria-describedby', s.id); if (isBusy) b.setAttribute('aria-disabled', 'true'); });
    acts.append(ok, no);
    li.append(s, why, acts);
    const m = msgBox('p' + r.id); if (m) li.append(m);
    return li;
  }
  function activeRow(r) {
    const li = el('li', 'rcard'); li.dataset.id = r.id;
    const s = el('p', 'rs', r.text); s.id = 'rA-' + r.id;
    const why = el('p', 'why');
    const t = r.decided_at ? fmtTime(r.decided_at) : '';
    if (t) why.append('确认于 ', el('span', 'mono', t));
    const stop = btn('btn', busy.has(r.id) ? '正在保存…' : '停用', 'a' + r.id + ':retire');
    stop.setAttribute('aria-describedby', s.id);
    stop.onclick = () => window.curioGuests.confirmDialog('停用这条规律？', r.text + '。停用后不再执行，也不会再提出；已被它移出的货仍可在"已自动移出"里捞回。', '停用', stop, () => decide(r, 'retire'));
    li.append(s, why, stop);
    const m = msgBox('a' + r.id); if (m) li.append(m);
    return li;
  }
  async function decide(r, decision) {
    if (busy.has(r.id)) return;
    const mk = (decision === 'retire' ? 'a' : 'p') + r.id;
    const rk = r.id + '|' + decision;
    const request_id = rids.get(rk) || rid(); rids.set(rk, request_id);
    busy.add(r.id); msgs.delete(mk); render();
    let res;
    try { res = await post('/api/rules/decide', {rule_id: r.id, decision, request_id}); }
    catch (e) { res = null; }
    busy.delete(r.id);
    if (res && res.status >= 400 && res.status < 500) {
      // 拒绝（已处理、已撤下、方向冲突、令牌失效…）：重试不会成功——显示原因并重新读取。
      rids.delete(rk);
      const text = '未保存（' + (res.data.error || '请刷新本页') + '）';
      shout(text); await load();
      msgs.set(mk, {text}); render();
      refocus(mk + ':' + decision, $('h05'));
      return;
    }
    if (!res || !res.ok) {
      msgs.set(mk, {text: '未保存', retry: () => decide(r, decision)});
      shout('未保存，请重试'); render(); refocus(mk + ':retry');
      return;
    }
    rids.delete(rk);
    if (decision === 'retire') {
      say('已停用规律'); await load(); refocus('a' + ((data.active || [])[0] || {}).id + ':retire', $('h05'));
      return;
    }
    const i = data.items.findIndex(x => x.id === r.id);
    if (i >= 0) data.items.splice(i, 1);
    data.count = typeof res.data.count === 'number' ? res.data.count : data.items.length;
    $('cRules').textContent = data.count;
    render();
    say(decision === 'confirm' ? '已确认规律' : '已否决规律');
    const next = data.items[Math.max(0, i)] || data.items[data.items.length - 1];
    if (next) refocus('p' + next.id + ':confirm'); else $('rEmptyH').focus();
    if (decision === 'confirm') load();     // 已生效的规律列表
  }
  $('rReload').onclick = () => load(active);
  load();                      // 计数：打开后台时读一次（不归纳）
  return {
    open() { active = true; load(true); },
    leave() { active = false; if (msgs.size) { [...msgs].forEach(([k, m]) => { if (!m.retry) msgs.delete(k); }); render(); } load(); }
  };
})();

/* ---------- 07 符合度（故事 23）：只读。今日 / 昨日数字、近 30 天折线（内联 SVG）、差异清单。
   只在打开 07 时读完整数据；分区计数打开后台时与 01 写入后读一次今天的摘要。 ---------- */
window.curioAgree = (function () {
  let data = null, pick = 'day', inflight = null;
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const pct = x => Math.round(x * 100) + '%';
  const shortDay = d => d.slice(5).replace('-', '/');
  function load() {
    if (inflight) return inflight;
    inflight = (async () => {
      try { data = await get('/api/agreement'); $('agErr').hidden = true; }
      catch (e) { if (!$('agreeView').hidden) { $('agErr').hidden = false; shout('未能读取符合度'); } }
      render();
    })().finally(() => { inflight = null; });
    return inflight;
  }
  function showCount(s) { $('cAgree').textContent = s && s.status === 'ok' ? pct(s.agreement) : '—'; }
  async function refreshCount() {
    try { const h = await get('/api/agreement/history?days=1'); showCount(h.days[h.days.length - 1]); }
    catch (e) { /* 下次再读 */ }
  }
  function noneText(p) {
    return p.status === 'no_prediction' ? '当天没有封存预测' : '无数据（当天没有留下）';
  }
  function dayBox(title, r) {
    const box = el('div', 'agday');
    box.append(el('h2', null, title), el('p', 'd', r.day));
    const p = r.pool, k = r.rank;
    const dl = el('dl');
    const row = (t, v) => dl.append(el('dt', null, t), el('dd', null, v));
    box.append(el('p', 'd', '进池符合度'));
    if (p.status === 'ok') {
      box.append(el('p', 'big', pct(p.agreement)));
      row('随机基线', pct(p.baseline));
      row('提升', p.lift == null ? '—' : p.lift.toFixed(1) + ' 倍');
      row('留下', p.n + ' 件');
      row('看到', p.seen + ' 件');
      row('系统同选', p.hits + ' 件');
    } else {
      box.append(el('p', 'none', noneText(p)));
    }
    row('R12 重合', k.status === 'ok' ? k.overlap + ' / ' + k.size : '无数据');
    box.append(dl);
    if (p.n) box.append(el('p', 'none', '你留下的 ' + p.n + ' 件里有 ' + p.kept_in_top + ' 件在系统前 ' + (p.system_top || 100)));
    if (p.late_seal) box.append(el('p', 'none', '封存晚于当天最早的 ' + p.late_decisions + ' 次挑选，这些不计入'));
    return box;
  }
  function chart(days) {
    const ok = days.filter(r => r.status === 'ok');
    if (!ok.length) return el('p', 'gnone', '暂无数据');
    const W = 320, H = 140, L = 32, R = 8, T = 8, B = 22;
    const ns = 'http://www.w3.org/2000/svg';
    const s = (tag, attrs) => { const e = document.createElementNS(ns, tag); Object.entries(attrs).forEach(([a, v]) => e.setAttribute(a, v)); return e; };
    const svg = s('svg', {viewBox: '0 0 ' + W + ' ' + H, role: 'img'});
    const x = i => L + (W - L - R) * (days.length > 1 ? i / (days.length - 1) : 0.5);
    const y = v => T + (H - T - B) * (1 - Math.min(1, Math.max(0, v)));
    [0, 0.5, 1].forEach(v => {
      svg.append(s('line', {class: 'ax', x1: L, x2: W - R, y1: y(v), y2: y(v)}));
      const t = s('text', {x: L - 4, y: y(v) + 4, 'text-anchor': 'end'}); t.textContent = Math.round(v * 100) + '%'; svg.append(t);
    });
    [[0, 'start'], [days.length - 1, 'end']].forEach(([i, a]) => {
      const t = s('text', {x: x(i), y: H - 6, 'text-anchor': a}); t.textContent = shortDay(days[i].day); svg.append(t);
    });
    // 无数据的日子断开折线，不画成 0。
    const path = key => {
      let d = '', pen = false;
      days.forEach((r, i) => {
        if (r.status !== 'ok') { pen = false; return; }
        d += (pen ? 'L' : 'M') + x(i).toFixed(1) + ' ' + y(r[key]).toFixed(1) + ' '; pen = true;
      });
      return d.trim();
    };
    svg.append(s('path', {class: 'bl', d: path('baseline')}), s('path', {class: 'ln', d: path('agreement')}));
    days.forEach((r, i) => {
      if (r.status !== 'ok') return;
      svg.append(s('rect', {class: 'bp', x: x(i) - 2, y: y(r.baseline) - 2, width: 4, height: 4}),
                 s('circle', {class: 'pt', cx: x(i), cy: y(r.agreement), r: 2.5}));
    });
    const last = ok[ok.length - 1];
    svg.setAttribute('aria-label', '近 ' + days.length + ' 天进池符合度：有数据 ' + ok.length + ' 天，最近一天 ' + last.day +
      ' 为 ' + pct(last.agreement) + '，随机基线 ' + pct(last.baseline));
    return svg;
  }
  function diffRow(c) {
    const li = el('li', 'prow');
    const th = el('div', 'th static');
    if (c.image) { const im = el('img'); im.src = c.image; im.alt = ''; im.loading = 'lazy'; th.append(im); }
    const body = el('div');
    const name = el('p', 't'); const nm = el('span', null, c.name || c.item_id); if (c.name_lang) nm.lang = c.name_lang; name.append(nm);
    (c.tags || []).forEach(x => name.append(tagEl(x)));
    const p = el('p', 'p'); p.append(el('span', 'mono', c.price || '价格待询'), ' · 来源 ' + (c.platform || ''));
    const st = el('p', 'st'); st.append('系统第 ', el('span', 'mono', String(c.system_pos)), ' 名 · 分数 ',
      el('span', 'mono', c.system_score == null ? '无' : String(c.system_score)));
    body.append(name, p, st);
    li.append(th, body);
    return li;
  }
  function renderDiff() {
    const r = data ? (pick === 'day' ? data.day : data.prev) : null;
    const p = r ? r.pool : null;
    const ok = p && p.status === 'ok';
    const you = ok ? p.you_only || [] : [], sys = ok ? p.system_only || [] : [];
    $('agDiffNote').textContent = !r ? '' : !ok ? (pick === 'day' ? '今日' : '昨日') + '：' + noneText(p) + '，没有差异清单'
      : !you.length && !sys.length ? '两边完全一致' : '';
    $('agYouN').textContent = ok ? you.length : '';
    $('agSysN').textContent = ok ? sys.length : '';
    $('agYou').replaceChildren(...you.map(diffRow));
    $('agSys').replaceChildren(...sys.map(diffRow));
    [['agPickToday', 'day'], ['agPickPrev', 'prev']].forEach(([id, k]) => $(id).setAttribute('aria-pressed', String(pick === k)));
  }
  function render() {
    if (!data) return;
    const h = data.history || [];
    showCount(h[h.length - 1]);
    $('agDays').replaceChildren(dayBox('今日', data.day), dayBox('昨日', data.prev));
    $('agChart').replaceChildren(chart(h));
    renderDiff();
  }
  $('agReload').onclick = load;
  $('agPickToday').onclick = () => { pick = 'day'; renderDiff(); };
  $('agPickPrev').onclick = () => { pick = 'prev'; renderDiff(); };
  refreshCount();              // 分区计数：打开后台时读一次今天的摘要
  return {open() { load(); }, refreshCount};
})();
