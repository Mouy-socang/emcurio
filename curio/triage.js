/* 后台 01 新货（故事 7，CAP-6）：拣选卡、手势与键盘、乐观写入与重试、撤销、已自动移出、货详情与放大层。
   原生 JS，无构建、无外部库。所有写请求带 X-Curio-Token；留下 / 去掉 / 捞回 / 撤销带 request_id（服务端幂等）。 */
'use strict';
const $ = id => document.getElementById(id);
const reduced = () => window.matchMedia('(prefers-reduced-motion: reduce)').matches;
const rid = () => (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : 'r' + Date.now().toString(36) + Math.random().toString(36).slice(2);
const LABEL = {keep: '留下', remove: '去掉'};

let queue = [];            // 本地新货队列，queue[0] 为当前卡片
const handled = new Set(); // 本地已留下 / 去掉（含未确认）的货，补货时排除
const failed = new Map();  // request_id -> {action, item}：写入失败、待重试
let newCount = null, arCount = null, inflight = 0, chain = Promise.resolve();
let flying = false, undoRetryRid = null, loadingMore = false, writes = 0, undoBusy = false;

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
  inflight++; writes++;
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
  if (hide && document.activeElement === $('retry')) $('btnKeep').focus();
  $('fail').hidden = hide;
}

/* ---------- 刷货卡 ---------- */
function setLang(el, lang) { if (lang) el.setAttribute('lang', lang); else el.removeAttribute('lang'); }
function tagEl(text) {
  const s = document.createElement('span'); s.className = 'tag';
  if (text.includes('Kleinanzeigen')) { s.append('仅后台 · '); const k = document.createElement('span'); k.lang = 'de'; k.textContent = 'Kleinanzeigen'; s.append(k); }
  else s.textContent = text;
  return s;
}
function scoreText(c) { return c.score == null ? '暂无分数' : '分数 ' + c.score + (c.score_reason ? ' · ' + c.score_reason : ''); }
function counts() {
  $('cNew').textContent = newCount == null ? '—' : newCount;
  $('arN').textContent = arCount == null ? '—' : arCount;
  $('arN2').textContent = arCount == null ? '' : arCount;
}
function render() {
  counts();
  const c = queue[0], empty = !c;
  $('card').hidden = empty; $('under').hidden = empty || queue.length < 2; $('empty').hidden = !empty || newCount == null;
  for (const b of [$('btnKeep'), $('btnRemove')]) { if (empty) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled'); }
  if (empty) return;
  const name = $('name'); name.textContent = '';
  const t = document.createElement('span'); t.textContent = c.name; setLang(t, c.name_lang); name.append(t);
  (c.tags || []).forEach(x => name.append(tagEl(x)));
  $('price').textContent = c.price;
  $('src').textContent = '来源 ' + c.platform;
  $('score').textContent = scoreText(c);
  $('ph').setAttribute('aria-label', c.name + '，打开货详情');
  const img = $('img');
  if (c.image) { img.src = c.image; img.hidden = false; } else { img.removeAttribute('src'); img.hidden = true; }
  $('card').style.transform = '';
}

async function load() {
  try {
    const d = await get('/api/triage?limit=50');
    queue = d.queue.filter(c => !handled.has(c.item_id));
    newCount = d.counts.new; arCount = d.counts.auto_removed;
    $('loadErr').hidden = true;
    render();
    return true;
  } catch (e) {
    $('loadErr').hidden = false; shout('未能读取新货');
    return false;
  }
}
async function topUp() {
  if (loadingMore || queue.length >= 5) return;
  loadingMore = true;
  const w = writes, idle = !inflight;
  try {
    const d = await get('/api/triage?limit=50');
    const have = new Set(queue.map(c => c.item_id));
    const wasEmpty = !queue.length;
    d.queue.forEach(c => { if (!handled.has(c.item_id) && !have.has(c.item_id)) queue.push(c); });
    // 读取期间有写入发出时，这份计数可能已过时，不采用。
    if (idle && !inflight && !failed.size && w === writes) { newCount = d.counts.new; arCount = d.counts.auto_removed; }
    if (wasEmpty) render(); else counts();
  } catch (e) {
    if (!queue.length && newCount > 0) { $('loadErr').hidden = false; render(); shout('未能读取新货'); }
  } finally { loadingMore = false; }
}
function sync(d) {
  if (!inflight && !failed.size && d.remaining != null) { newCount = d.remaining; arCount = d.auto_removed; counts(); }
}

function act(action, expectId) {
  if (!queue.length || flying) return;
  if (expectId && queue[0].item_id !== expectId) return;
  hideMsg();
  const item = queue.shift();
  handled.add(item.item_id);
  for (const [k, f] of failed) if (f.item.item_id === item.item_id) failed.delete(k);  // 重新处理：旧的未保存动作作废
  updateFail();
  newCount = Math.max(0, (newCount || 0) - 1);
  render();
  say('已' + LABEL[action] + '，剩余 ' + newCount + ' 件');
  dispatch(action, item, rid());
  if (queue.length < 5) topUp();
}
function markFailed(action, item, r, network) {
  failed.set(r, {action, item});
  if (!queue.some(c => c.item_id === item.item_id)) { queue.unshift(item); newCount = (newCount || 0) + 1; }
  handled.delete(item.item_id);
  render(); updateFail();
  if (network) setNet(true);
  shout(failed.size + ' 件未保存');
}
// 服务端拒绝（4xx）：该件没有被处理，放回队首、计数加回。
function restore(item) {
  handled.delete(item.item_id);
  if (!queue.some(c => c.item_id === item.item_id)) { queue.unshift(item); newCount = (newCount || 0) + 1; }
  render();
}
async function dispatch(action, item, r) {
  try {
    const res = await send({action, item_id: item.item_id, request_id: r});
    if (res.ok) { failed.delete(r); updateFail(); setNet(false); sync(res.data); return true; }
    if (res.status >= 500) { markFailed(action, item, r, false); return false; }
    failed.delete(r); updateFail();
    restore(item);
    showMsg('未保存：' + (res.data.error || '请刷新本页后重试')); shout('未保存');
    return false;
  } catch (e) { markFailed(action, item, r, true); return false; }
}
async function retry() {
  const entries = [...failed.entries()];
  let net = false;
  for (const [r, f] of entries) {
    if (!failed.has(r)) continue;  // 期间已重新处理这件：旧动作作废，不覆盖新动作
    try {
      const res = await send({action: f.action, item_id: f.item.item_id, request_id: r});
      if (res.ok || res.status < 500) {
        failed.delete(r);
        const i = queue.findIndex(c => c.item_id === f.item.item_id);
        if (i >= 0 && res.ok) { queue.splice(i, 1); handled.add(f.item.item_id); newCount = Math.max(0, newCount - 1); }
        if (!res.ok) { restore(f.item); showMsg('未保存：' + (res.data.error || '请刷新本页后重试')); }
        else sync(res.data);
      }
    } catch (e) { net = true; }
  }
  render(); updateFail();
  if (!failed.size && !net) { setNet(false); say('已保存'); }
  else { if (net) setNet(true); shout(failed.size + ' 件未保存'); }
}
async function netRetry() {
  if (failed.size) return retry();
  if (await load()) setNet(false);
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
  const id = c ? c.item_id : d.item_id, had = queue.some(x => x.item_id === id);
  queue = queue.filter(x => x.item_id !== id);
  if (d.state === 'new' && c) {
    queue.unshift(c); handled.delete(id);
    if (!had) newCount = (newCount || 0) + 1;
  } else {
    // 撤销后不在新货（如撤销捞回 → 自动移出、撤销去掉 → 留下）：从本地队列拿掉
    if (had) newCount = Math.max(0, (newCount || 0) - 1);
    if (d.state === 'auto_removed') arCount = (arCount || 0) + 1;
  }
  sync(d);
  render();
  const text = '已撤销，回到 ' + (c ? c.name : '上一步');
  showMsg(text); say(text);
}

/* ---------- 手势：右滑留下、左滑去掉 ---------- */
(function swipe() {
  const card = $('card');
  let start = null, dragging = false, suppress = false, samples = [];
  card.addEventListener('pointerdown', e => {
    if (!e.isPrimary || e.button > 0 || flying || !queue.length) return;
    start = {x: e.clientX, y: e.clientY, id: e.pointerId}; dragging = false;
    samples = [{x: e.clientX, t: e.timeStamp}];
  });
  card.addEventListener('pointermove', e => {
    if (!start || e.pointerId !== start.id) return;
    const dx = e.clientX - start.x, dy = e.clientY - start.y;
    if (!dragging) {
      if (Math.abs(dx) > 8 && Math.abs(dx) > Math.abs(dy)) { dragging = true; try { card.setPointerCapture(e.pointerId); } catch (err) { /* 合成事件无活动指针 */ } }
      else if (Math.abs(dy) > 8) { start = null; return; }
      else return;
    }
    samples.push({x: e.clientX, t: e.timeStamp}); if (samples.length > 20) samples.shift();
    if (!reduced()) card.style.transform = 'translateX(' + dx + 'px) rotate(' + (dx * 0.03) + 'deg)';
  });
  function end(e, cancel) {
    if (!start || e.pointerId !== start.id) return;
    const dx = e.clientX - start.x; start = null;
    if (!dragging) return;
    dragging = false; suppress = true; setTimeout(() => { suppress = false; }, 0);
    const w = card.offsetWidth || 1;
    // 甩动速度：取最近 100ms 内的位移（至少按 16ms 计），避免单个采样的抖动。
    const last = samples[samples.length - 1], old = samples.find(p => p.t >= last.t - 100) || last;
    const v = (last.x - old.x) / Math.max(16, last.t - old.t);  // px/ms
    const fling = Math.abs(v) > 0.6 && Math.abs(dx) > 40 && Math.sign(v) === Math.sign(dx);
    if (!cancel && (Math.abs(dx) > w * 0.35 || fling)) {
      const action = dx > 0 ? 'keep' : 'remove', target = queue[0] && queue[0].item_id;
      if (reduced()) { card.style.transform = ''; act(action, target); return; }
      flying = true; card.classList.add('fly');
      card.style.transform = 'translateX(' + (dx > 0 ? 1.3 : -1.3) * w + 'px) rotate(' + (dx > 0 ? 8 : -8) + 'deg)';
      setTimeout(() => { card.classList.remove('fly'); card.style.transform = ''; flying = false; act(action, target); }, 180);
    } else if (reduced()) card.style.transform = '';
    else {
      card.classList.add('back'); card.style.transform = '';
      setTimeout(() => card.classList.remove('back'), 220);
    }
  }
  card.addEventListener('pointerup', e => end(e, false));
  card.addEventListener('pointercancel', e => end(e, true));
  $('ph').addEventListener('click', e => { if (suppress) { e.preventDefault(); return; } if (queue[0]) openDetail(queue[0].item_id, 'new', $('ph')); });
})();

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
  $('openAr').focus();
}
// 捞回成功：在售的货放回本地队首，计数随之更新。
function applyRescued(d, fallback) {
  const card = d.card || fallback;
  queue = queue.filter(x => x.item_id !== card.item_id);
  handled.delete(card.item_id);
  if (card.status === 'active') { queue.unshift(card); newCount = (newCount || 0) + 1; }
  arCount = Math.max(0, (arCount || 0) - 1);
  setNet(false); sync(d); render();
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

/* ---------- 货详情 ---------- */
let detail = null, detailMode = 'new', detailOpener = null, gi = 0, savedComment = '', savedAlts = [];
function focusables(root) {
  return [...root.querySelectorAll('button,a[href],input,textarea,select,[tabindex]:not([tabindex="-1"])')].filter(el => !el.hidden && el.offsetParent !== null);
}
async function openDetail(id, mode, opener) {
  let d;
  try { d = await get('/api/triage/item?id=' + encodeURIComponent(id)); }
  catch (e) {
    if ($('arView').hidden) showMsg('未能打开货详情，请重试');
    else { $('arMsg').textContent = '未能打开货详情，请重试'; $('arMsg').hidden = false; }
    shout('未能打开货详情'); return;
  }
  $('arMsg').hidden = true;
  detail = d; detailMode = mode; detailOpener = opener;
  $('dCode').textContent = d.code || '';
  const nm = $('dName'); nm.textContent = d.name; setLang(nm, d.name_lang);
  const tags = $('dTags'); tags.textContent = '';
  const pr = document.createElement('span'); pr.className = 'mono'; pr.textContent = d.price; tags.append(pr);
  (d.tags || []).forEach(x => tags.append(tagEl(x)));
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
  if (d.score != null) { const m = document.createElement('span'); m.className = 'mono'; m.textContent = d.score; sc.append(m); if (d.score_reason) sc.append(' · ' + d.score_reason); }
  else sc.textContent = '暂无分数';
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
  if (!$('poolView').hidden) return '商品池 · 后台';
  if (!$('rankView').hidden) return '榜 · 后台';
  return $('arView').hidden ? '新货 · 后台' : '已自动移出 · 后台';
}
function viewFocus() {
  if (!$('poolView').hidden) return $('h02');
  if (!$('rankView').hidden) return $('h03');
  return $('arView').hidden ? $('btnKeep') : $('arTitle');
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
      if (queue.some(x => x.item_id === id)) { queue = queue.filter(x => x.item_id !== id); newCount = Math.max(0, (newCount || 0) - 1); render(); }
    }
  } catch (e) { /* 保持“暂时无法确认” */ }
  b.textContent = '复查'; b.removeAttribute('aria-disabled');
  if (detail && detail.item_id === id) { msg.textContent = text; msg.hidden = false; }
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
  if (queue[0] && queue[0].item_id === id) {
    if (await closeDetail(false)) { act('keep', id); $('btnKeep').focus(); }
    return;
  }
  // 不是当前卡片（如已在别处处理过）：直接按货号留下，结果写在详情里
  try {
    const res = await send({action: 'keep', item_id: id, request_id: rid()});
    if (!res.ok) return primaryMsg(res.status < 500 && res.data.error ? '未能留下：' + res.data.error : '未能留下，请重试');
    const had = queue.some(x => x.item_id === id);
    queue = queue.filter(x => x.item_id !== id); handled.add(id);
    if (had) newCount = Math.max(0, (newCount || 0) - 1);
    setNet(false); sync(res.data); render();
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
  if ($('triageView').hidden || e.altKey || e.ctrlKey || e.metaKey) return;
  const t = e.target;
  if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
  if (e.key === 'ArrowLeft') { e.preventDefault(); act('remove'); }
  else if (e.key === 'ArrowRight') { e.preventDefault(); act('keep'); }
});

/* ---------- 绑定 ---------- */
$('btnKeep').onclick = () => act('keep');
$('btnRemove').onclick = () => act('remove');
$('undo').onclick = undo;
$('retry').onclick = retry;
$('netRetry').onclick = netRetry;
$('reload').onclick = load;
$('openAr').onclick = openAr;
$('back').onclick = closeAr;
$('arMore').onclick = () => loadAr(false);
$('arReload').onclick = () => loadAr(false);
$('dClose').onclick = () => closeDetail();
$('dComment').addEventListener('blur', () => { if ($('dComment').value !== savedComment) saveComment(); });
$('dRecheck').onclick = recheck;
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
load();
