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
  if (!$('guestsView').hidden) return '客人 · 后台';
  if (!$('poolView').hidden) return '商品池 · 后台';
  if (!$('rankView').hidden) return '榜 · 后台';
  return $('arView').hidden ? '新货 · 后台' : '已自动移出 · 后台';
}
function viewFocus() {
  if (!$('matchView').hidden) return $('h04');
  if (!$('guestsView').hidden) return $('h06');
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
      if (queue.some(x => x.item_id === id)) { queue = queue.filter(x => x.item_id !== id); newCount = Math.max(0, (newCount || 0) - 1); render(); }
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
  msg.textContent = '已标记售出' + (res.data.left_rank ? '，并已移出榜' : '') + '（可在 02 商品池"已移出"里撤销）';
  msg.hidden = false; say('已标记售出');
  if (detailMode === 'pool' || detailMode === 'rank') { $('dPrimary').textContent = '加入榜'; setPrimaryReason('不在商品池，不能上榜'); }
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
   按客人一行（称呼 + 配上件数；有"明说想要"的在前，再按待处理件数），点开展开逐件列表：缩略图、货号、标准名称、价格、
   分级与理由；每件"确认"或"不合适"（带 request_id；网络或服务端失败行内"未保存 · 重试"，重试沿用同一个 request_id；
   400 类拒绝只显示原因并重新读取）。不合适之后该行变成"已标不合适 · 撤销"，直到下一次读取。确认后出现
   "复制单件页链接"：链接 = 站点地址 + /item/CO-NNNN（不按人区分）；该件不在已送达的客人页上（未发布或已隐藏）时
   aria-disabled，原因"请先发布这件货"。剪贴板失败时显示已全选的只读输入框。 */
window.curioMatches = (function () {
  let data = null, active = false, inflight = null, again = false, lastJson = '';
  const expanded = new Set();  // 展开的客人 customer_id
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
    const n = el('span', 'mn', c.name); if (c.has_need) n.append(tagEl('明说想要'));
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
    why.append(it.reasons.join('；'));
    body.append(t, p, why);
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
