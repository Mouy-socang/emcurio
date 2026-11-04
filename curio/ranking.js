/* 后台 02 商品池与 03 R12（故事 11，CAP-7）：分区切换（#new / #pool / #rank …… #agree）、商品池列表（加入榜、移出 / 放回商品池、
   标记售出（需确认）/ 撤销售出（故事 13）、货详情）、排榜（把手长按拖动、上移 / 下移 / 移出榜、播报）、排序即时保存与重试、预览入口与发布状态。
   原生 JS，无构建、无外部库；与 triage.js 共用 $、rid、get、post、say、shout、setNet、openDetail、viewTitle。
   发布不在这里：只从预览横条（/preview/，preview-bar.js）发起。 */
(function () {
  'use strict';
  const SIZE = 12;
  const REASON_FULL = 'R12 已满 ' + SIZE + ' 件，请先移出一件';
  const LONG_PRESS = 350;

  let view = 'new';
  let pool = null;              // /api/pool 的结果
  const cards = new Map();      // 货号 -> 卡片字段（名称、图、价格、标签、rank_reason …）
  const info = new Map();       // 货号 -> {off, gone, reason, days, stale}（服务端判定的榜行状态；days/stale 见故事 28）
  let order = [];               // 本地的榜（货号按名次）
  let saved = [];               // 服务端已保存的榜
  let status = null;            // 发布状态 {published_at, dirty}
  let rankLoaded = false, unsaved = false, saving = false, again = false, saveError = '';
  let selected = null, dragging = false, previewing = false, cancelDrag = null;
  let poolSort = 'score';       // 02 排序：score（按分数，服务端顺序 = sort_key：综合分 − 曝光扣分）/ time（按进池时间，新到旧）

  const same = (a, b) => a.length === b.length && a.every((x, i) => x === b[i]);
  const two = n => String(n).padStart(2, '0');
  function hhmm(iso) {
    const d = new Date(iso); if (isNaN(d)) return '';
    return two(d.getHours()) + ':' + two(d.getMinutes());
  }
  function el(tag, cls, text) {
    const e = document.createElement(tag);
    if (cls) e.className = cls;
    if (text != null) e.textContent = text;
    return e;
  }
  function nameEl(tag, c) {
    const h = el(tag);
    const t = el('span', null, c.name || c.code); if (c.name_lang) t.lang = c.name_lang;
    h.append(t);
    (c.tags || []).forEach(x => h.append(tagEl(x)));
    return h;
  }
  // 故事 27：分数与理由一行（02 每张卡、03 每行）；没有分数时不显示。
  // 故事 28：02 卡片在分数旁显示"曝光 −N"（挂过 R12 的扣分，只影响排序，分数本身不变）。
  function scoreEl(c, exposure) {
    if (c.score == null) return null;
    const p = el('p', 'sc'); p.append('分数 ', el('span', 'mono', String(c.score)));
    if (exposure && c.exposure_penalty > 0) p.append(' · ', el('span', 'xp', '曝光 −' + c.exposure_penalty));
    if (c.score_reason) p.append(' · ' + c.score_reason);
    return p;
  }
  const sortKey = x => x.sort_key == null ? -Infinity : x.sort_key;
  // 重绘后把焦点放回同一个控件（data-k = 货号:用途）。
  function keyOf() { const a = document.activeElement; return a && a.dataset ? a.dataset.k : null; }
  function refocus(k, fallback, keep) {
    if (!k) return;
    const t = document.querySelector('[data-k="' + CSS.escape(k) + '"]');
    const o = keep ? {preventScroll: true} : undefined;     // keep：02 原位更新，不因移焦点滚动页面
    if (t && t.offsetParent !== null) t.focus(o); else if (fallback) fallback.focus(o);
  }
  function disable(btn, reason, whyEl) {
    if (reason) {
      btn.setAttribute('aria-disabled', 'true');
      if (whyEl) { whyEl.textContent = reason; whyEl.hidden = false; btn.setAttribute('aria-describedby', whyEl.id); }
    } else {
      btn.removeAttribute('aria-disabled'); btn.removeAttribute('aria-describedby');
      if (whyEl) whyEl.hidden = true;
    }
  }

  /* ---------- 分区切换 ---------- */
  function fromHash() {
    return location.hash === '#pool' ? 'pool' : location.hash === '#rank' ? 'rank' : location.hash === '#match' ? 'match' :
      location.hash === '#rules' ? 'rules' : location.hash === '#guests' ? 'guests' : location.hash === '#agree' ? 'agree' : 'new';
  }
  function show(v, user) {
    view = v;
    // 回到 01 时显示刷货卡（已自动移出子视图收起）。
    $('arView').hidden = true;
    if ($('seenView')) $('seenView').hidden = true;      // 01 的「看过未选」子视图同样收起
    if ($('aucView')) $('aucView').hidden = true;        // 故事 20：「拍卖新品」子视图同样收起
    $('triageView').hidden = v !== 'new';
    $('poolView').hidden = v !== 'pool';
    $('rankView').hidden = v !== 'rank';
    $('matchView').hidden = v !== 'match';
    $('rulesView').hidden = v !== 'rules';
    $('guestsView').hidden = v !== 'guests';
    $('agreeView').hidden = v !== 'agree';
    [['new', 'tab01'], ['pool', 'tab02'], ['rank', 'tab03'], ['match', 'tab04'], ['rules', 'tab05'], ['guests', 'tab06'], ['agree', 'tab07']].forEach(([k, id]) => {
      if (k === v) $(id).setAttribute('aria-current', 'page'); else $(id).removeAttribute('aria-current');
    });
    document.querySelector('main.scr').classList.toggle('wide', v === 'pool' || v === 'new');   // 01 瀑布流与 02 商品池用宽版
    document.title = viewTitle();
    if (v === 'pool') loadPool();
    if (v === 'rank') { loadRank(); if (!pool) loadPool(); }
    if (window.curioMatches) { if (v === 'match') window.curioMatches.open(); else window.curioMatches.leave(); }
    if (window.curioRules) { if (v === 'rules') window.curioRules.open(); else window.curioRules.leave(); }
    if (window.curioGuests) { if (v === 'guests') window.curioGuests.open(); else window.curioGuests.leave(); }
    if (window.curioAgree && v === 'agree') window.curioAgree.open();
    if (user) {
      const h = v === 'pool' ? $('h02') : v === 'rank' ? $('h03') : v === 'match' ? $('h04') : v === 'rules' ? $('h05') : v === 'guests' ? $('h06') : v === 'agree' ? $('h07') : $('h01');
      h.setAttribute('tabindex', '-1'); h.focus();
    }
  }

  /* ---------- 读取 ---------- */
  let poolSeq = 0;               // 商品池读取序号：较早发出的读取晚到时丢弃，不覆盖之后的操作
  async function loadPool(anchor, top) {
    const seq = ++poolSeq;
    try {
      const d = await get('/api/pool');
      if (seq !== poolSeq) return false;
      pool = d;
      d.items.concat(d.out).forEach(c => { if (c.code) cards.set(c.code, c); });
      if (!rankLoaded && !unsaved && !saving) { order = d.rank.slice(); saved = d.rank.slice(); }
      $('poolErr').hidden = true;
    } catch (e) {
      if (seq !== poolSeq) return false;
      if (view === 'pool') { $('poolErr').hidden = false; shout('未能读取商品池'); }
    }
    renderPool(anchor, top); counts();
    return true;
  }
  function absorb(d) {
    d.rows.forEach(r => { cards.set(r.code, Object.assign({}, cards.get(r.code) || {}, r)); info.set(r.code, {off: r.off, gone: r.gone, reason: r.reason, days: r.days_on_rank || 0, stale: !!r.stale_hint}); });
    status = d.status;
  }
  async function loadRank(force) {
    if (dragging) return;
    try {
      const d = await get('/api/rank');
      absorb(d);
      if (force || (!unsaved && !saving)) { order = d.rows.map(r => r.code); saved = order.slice(); }
      rankLoaded = true;
    } catch (e) {
      if (view === 'rank') { showRankErr('未能读取 R12', loadRank); }
      return;
    }
    renderRank(); counts();
  }
  function counts() {
    $('cPool').textContent = pool ? pool.counts.kept : '—';
    $('cRank').textContent = (rankLoaded || pool ? order.length : '—') + ' / ' + SIZE;
  }

  /* ---------- 02 商品池 ---------- */
  function rankReason(c) {
    if (order.includes(c.code)) return null;
    return c.rank_reason || (order.length >= SIZE ? REASON_FULL : null);
  }
  function poolRow(c, out) {
    const li = el('li', 'prow' + (c.on_sale ? '' : ' gone')); li.dataset.id = c.item_id;
    const th = el('button', 'th'); th.type = 'button'; th.setAttribute('aria-label', (c.name || '') + '，打开货详情');
    if (c.image) { const im = el('img'); im.src = c.image; im.alt = ''; im.loading = 'lazy'; th.append(im); }
    th.addEventListener('contextmenu', e => e.preventDefault());
    const body = el('div');
    const code = el('p', 'code', c.code || '—');
    const t = el('button', 't'); t.type = 'button'; t.append(...nameEl('span', c).childNodes);
    t.dataset.k = c.item_id + ':open';
    const p = el('p', 'p'); p.append(el('span', 'mono', c.price), ' · 来源 ' + c.platform);
    const st = el('p', 'st', out && c.sold ? '已售出' : c.on_sale ? '在售' : '已下架');
    const pos = order.indexOf(c.code);
    if (!out && pos >= 0) st.append(' · R12 第 ', el('span', 'mono', String(pos + 1)), ' 名');
    if (c.auction) st.append(' · ', el('span', 'auc', '拍卖中' + (endsText(c.ends) ? ' · 结束于 ' + endsText(c.ends) : '')));
    const why = el('p', 'reason'); why.id = 'why-' + c.item_id.replace(/[^\w-]/g, '_'); why.hidden = true;
    const msg = el('p', 'im'); msg.hidden = true; msg.setAttribute('role', 'alert');
    const acts = el('div', 'acts2');
    let confirmBox = null;
    if (out && c.sold) {
      const undo = el('button', 'btn', '撤销售出'); undo.type = 'button'; undo.dataset.k = c.item_id + ':back';
      undo.onclick = () => poolAct('undo_sold', c, msg, undo);
      acts.append(undo); acts.style.gridTemplateColumns = '1fr';
    } else if (out) {
      const back = el('button', 'btn', '放回商品池'); back.type = 'button'; back.dataset.k = c.item_id + ':back';
      back.onclick = () => poolAct('back_to_pool', c, msg, back);
      acts.append(back); acts.style.gridTemplateColumns = '1fr';
    } else {
      const inRank = pos >= 0;
      const rb = el('button', 'btn', inRank ? '移出 R12' : '加入 R12'); rb.type = 'button'; rb.dataset.k = c.item_id + ':rank';
      disable(rb, inRank ? null : rankReason(c), why);
      rb.onclick = () => {
        if (rb.getAttribute('aria-disabled') === 'true') { say(why.textContent); return; }
        if (order.includes(c.code)) dropFromRank(c.code); else addToRank(c.code);
      };
      const ob = el('button', 'btn', '移出商品池'); ob.type = 'button'; ob.dataset.k = c.item_id + ':out';
      ob.onclick = () => poolAct('out_of_pool', c, msg, ob);
      // 标记售出需确认：先展开确认行，再点"确认标记售出"才提交。
      const sb = el('button', 'btn', '标记售出'); sb.type = 'button'; sb.dataset.k = c.item_id + ':sold';
      confirmBox = el('div', 'confirm'); confirmBox.hidden = true;
      const ctext = el('p', null, '标记售出后，这件货立即从客人页消失，单件页显示"已售出"。可撤销。');
      ctext.id = 'sold-' + c.item_id.replace(/[^\w-]/g, '_');
      const yes = el('button', 'btn primary', '确认标记售出'); yes.type = 'button'; yes.dataset.k = c.item_id + ':soldyes';
      yes.setAttribute('aria-describedby', ctext.id);
      const no = el('button', 'btn', '取消'); no.type = 'button';
      const crow = el('div', 'acts2'); crow.append(yes, no);
      confirmBox.append(ctext, crow);
      sb.setAttribute('aria-expanded', 'false');
      sb.onclick = () => { confirmBox.hidden = false; sb.setAttribute('aria-expanded', 'true'); yes.focus(); };
      no.onclick = () => { confirmBox.hidden = true; sb.setAttribute('aria-expanded', 'false'); sb.focus(); };
      yes.onclick = () => poolAct('mark_sold', c, msg, yes);
      acts.append(rb, ob, sb);
    }
    // 故事 20：只拍卖的货可"转为普通货"（拍下后解除拍卖限制）或"改回拍卖"。
    let conv = null;
    if (c.auction_only) {
      const cb = el('button', 'btn', c.auction ? '转为普通货' : '改回拍卖'); cb.type = 'button'; cb.dataset.k = c.item_id + ':conv';
      cb.onclick = () => convert(c, !!c.auction, msg, cb);
      conv = el('div', 'acts2'); conv.style.gridTemplateColumns = '1fr'; conv.append(cb);
    }
    body.append(code, t, p, st);
    const sc = scoreEl(c, true); if (sc) body.append(sc);
    // 故事 21：看图识别的功能 · 器型 · 图案。
    if (c.vision_line) body.append(el('p', 'vis', c.vision_line));
    body.append(acts);
    if (conv) body.append(conv);
    if (confirmBox) body.append(confirmBox);
    body.append(why, msg);
    li.append(th, body);
    th.onclick = () => openDetail(c.item_id, out ? 'new' : 'pool', th);
    t.onclick = () => openDetail(c.item_id, out ? 'new' : 'pool', t);
    return li;
  }
  // 02 保持原位（故事 26）：重画前记下一行（anchor 货号，缺省为视口里第一行）离视口顶的距离（或给定的 top），
  // 重画后把它滚回那里；处理掉一件时以下一件为锚、top 取被处理那件原来的位置，下一件就落在原处、可接着处理。
  function anchorRow(id) {
    const rows = document.querySelectorAll('#poolList > li, #outList > li');
    if (id) { for (const li of rows) if (li.dataset.id === id) return li; }
    for (const li of rows) { const r = li.getBoundingClientRect(); if (r.bottom > 0 && r.height) return li; }
    return null;
  }
  // 02 排序切换：按分数为服务端顺序（sort_key = 综合分 − 曝光扣分，高到低）；按进池时间为最近进池的在前。
  function sortedPool() {
    if (poolSort !== 'time') return pool.items;
    return pool.items.slice().sort((a, b) => (b.pool_at || '').localeCompare(a.pool_at || '') || a.item_id.localeCompare(b.item_id));
  }
  function setPoolSort(v) {
    if (poolSort === v) return;
    poolSort = v; renderPool(false);
    const top = $('poolList').getBoundingClientRect().top;
    if (top < 0) window.scrollBy(0, top - 12);          // 换了排序从第一件看起
    say(v === 'time' ? '已按进池时间排序' : '已按分数排序');
  }
  function renderPool(anchor, top) {
    if (!pool) return;
    const k = keyOf();
    const a = view === 'pool' && anchor !== false ? anchorRow(anchor) : null;     // anchor=false：不保持原位（换排序）
    const aId = a && a.dataset.id, aTop = a && (top != null && a.dataset.id === anchor ? top : a.getBoundingClientRect().top);
    $('poolN').textContent = pool.counts.kept;
    $('poolRankN').textContent = order.length;
    const list = $('poolList'); list.textContent = '';
    sortedPool().forEach(c => list.append(poolRow(c, false)));
    list.setAttribute('aria-label', poolSort === 'time' ? '已留下的货，最近进池的在前' : '已留下的货，按分数高到低');
    $('sortScore').setAttribute('aria-pressed', poolSort === 'score' ? 'true' : 'false');
    $('sortTime').setAttribute('aria-pressed', poolSort === 'time' ? 'true' : 'false');
    list.classList.add('grid');
    $('poolEmpty').hidden = pool.items.length > 0;
    const outList = $('outList'); outList.textContent = '';
    pool.out.forEach(c => outList.append(poolRow(c, true)));
    outList.hidden = $('outH').hidden = !pool.out.length;
    $('outN').textContent = pool.out.length;
    if (aId) {
      const b = anchorRow(aId);
      if (b && b.dataset.id === aId) window.scrollBy(0, b.getBoundingClientRect().top - aTop);
    }
    refocus(k, $('h02'), true);
  }
  // 操作成功后先在本地挪动这一件（移出 / 售出到下方列表，放回 / 撤销售出到商品池），读取失败时页面也不会把它放回原处。
  function moveLocal(action, c, d) {
    if (!pool) return;
    const fromKept = action === 'out_of_pool' || action === 'mark_sold';
    const src = fromKept ? pool.items : pool.out, dst = fromKept ? pool.out : pool.items;
    const i = src.findIndex(x => x.item_id === c.item_id);
    const row = Object.assign({}, i >= 0 ? src[i] : c, {sold: !!d.sold});
    if (d.left_rank) {
      row.rank_pos = null;
      order = order.filter(x => x !== c.code); saved = saved.filter(x => x !== c.code);
      $('poolRankN').textContent = order.length;
    }
    if (i >= 0) src.splice(i, 1);
    if (!dst.some(x => x.item_id === c.item_id)) {
      // 与服务端同序：排序键（综合分 − 曝光扣分）高到低、无分在后；同键时刚处理的在前。
      const at = dst.findIndex(x => sortKey(x) <= sortKey(row));
      dst.splice(at < 0 ? dst.length : at, 0, row);
    }
    pool.counts.kept = pool.items.length; pool.counts.out = pool.out.length;
  }
  const calm = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  function fadeOut(li) {
    if (!li || calm()) return Promise.resolve();
    li.classList.add('leaving'); li.inert = true;     // 淡出期间不可点、不可聚焦
    return new Promise(r => setTimeout(r, 220));
  }
  async function poolAct(action, c, msg, btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const r = btn.dataset.rid || rid(); btn.dataset.rid = r;
    btn.setAttribute('aria-busy', 'true'); msg.hidden = true;
    let res = null;
    try { res = await post('/api/pool/action', {action, item_id: c.item_id, request_id: r}); }
    catch (e) { setNet(true); }
    btn.removeAttribute('aria-busy');
    if (!res || !res.ok) {
      if (res && res.status < 500) delete btn.dataset.rid;
      msg.textContent = (res && res.status < 500 && res.data.error ? '未保存：' + res.data.error : '未保存') + ' · ';
      const again2 = el('button', 'lnk lnk-pad', '重试'); again2.type = 'button'; again2.onclick = () => poolAct(action, c, msg, btn);
      msg.append(again2); msg.hidden = false; shout('未保存，请重试');
      return;
    }
    setNet(false);
    const d = res.data;
    // 不用响应里的榜覆盖本地（可能是重放的旧结果）：只拿掉移出商品池的那件（moveLocal），其余以重新读取的榜为准。
    const rankNote = d.left_rank ? '，并已移出 R12' : '';
    const next = {out_of_pool: '已移出商品池' + rankNote, mark_sold: '已标记售出' + rankNote,
                  undo_sold: '已撤销售出', back_to_pool: '已放回商品池'}[action];
    // 原位：这一件淡出，页面以下一行为锚不跳动；焦点移到下一行的同一个按钮（不滚动），没有则到列表标题。
    const li = btn.closest('li'), sib = li && (li.nextElementSibling || li.previousElementSibling);
    const part = {out_of_pool: ':out', mark_sold: ':sold'}[action] || ':back';
    const k = sib ? sib.dataset.id + part : null;
    const sid = sib ? sib.dataset.id : null, top = li && sib === li.nextElementSibling ? li.getBoundingClientRect().top : null;
    poolSeq++;                                // 作废此前还在路上的商品池读取
    await fadeOut(li);
    moveLocal(action, c, d);
    renderPool(sid, top); counts();
    if (k) refocus(k, $('h02'), true); else $('h02').focus({preventScroll: true});
    say(next);
    // 后台重读：只在是最新一次读取时重画；重画沿用当时的焦点（用户已移开焦点就不抢回来）。
    loadPool(sid);
    loadRank();
  }

  // 结束时间（柏林时间，"11月5日 21:05"）；读不出时为空。
  function endsText(iso) {
    const d = iso ? new Date(iso) : null; if (!d || isNaN(d)) return '';
    try {
      const f = new Intl.DateTimeFormat('zh-CN', {timeZone: 'Europe/Berlin', month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit', hour12: false}).formatToParts(d);
      const g = t => (f.find(x => x.type === t) || {}).value || '';
      return g('month') + '月' + g('day') + '日 ' + g('hour') + ':' + g('minute');
    } catch (e) { return ''; }
  }
  // 转为普通货 / 改回拍卖（故事 20）：同一按钮重试沿用 request_id；成功后重读商品池与 R12。
  async function convert(c, on, msg, btn) {
    if (btn.getAttribute('aria-busy') === 'true') return;
    const r = btn.dataset.rid || rid(); btn.dataset.rid = r;
    btn.setAttribute('aria-busy', 'true'); msg.hidden = true;
    let res = null;
    try { res = await post('/api/auction/convert', {item_id: c.item_id, on, request_id: r}); }
    catch (e) { setNet(true); }
    btn.removeAttribute('aria-busy');
    if (!res || !res.ok) {
      if (res && res.status < 500) delete btn.dataset.rid;
      msg.textContent = (res && res.status < 500 && res.data.error ? '未保存：' + res.data.error : '未保存') + ' · ';
      const again2 = el('button', 'lnk lnk-pad', '重试'); again2.type = 'button'; again2.onclick = () => convert(c, on, msg, btn);
      msg.append(again2); msg.hidden = false; shout('未保存，请重试');
      return;
    }
    delete btn.dataset.rid; setNet(false);
    // 改回拍卖的货仍在 R12 上：下次发布会被拒，先提示（重画后写在这一行的消息里）。
    const warn = !on && res.data.on_rank ? '这件在 R12 上，发布前请移出或再转为普通货' : '';
    if (warn) shout(warn); else say(on ? '已转为普通货，可以加入 R12' : '已改回拍卖，拍卖中不能加入 R12');
    await loadPool(c.item_id);
    loadRank();
    if (warn) {
      const li = [...document.querySelectorAll('#poolList > li, #outList > li')].find(x => x.dataset.id === c.item_id);
      const m = li ? li.querySelector('.im') : msg;
      m.textContent = warn; m.hidden = false;
    }
  }

  /* ---------- 03 R12 ---------- */
  function addToRank(code) {
    if (order.includes(code)) return;
    if (order.length >= SIZE) { say(REASON_FULL); return; }
    order.push(code); selected = null;
    say('已加入 R12，第 ' + order.length + ' 名');
    changed();
    return '已加入 R12，第 ' + order.length + ' 名';
  }
  function dropFromRank(code) {
    const i = order.indexOf(code); if (i < 0) return;
    order.splice(i, 1);
    if (selected === code) selected = null;
    say('已移出 R12');
    changed();
    return '已移出 R12';
  }
  function move(code, to, focusPart) {
    const from = order.indexOf(code);
    if (from < 0) return;
    to = Math.max(0, Math.min(order.length - 1, to));
    if (to === from) { say(to === 0 ? '已在第 1 名' : '已在最后一名'); return; }
    order.splice(from, 1); order.splice(to, 0, code);
    changed(code + ':' + focusPart);
    say('已放到第 ' + (to + 1) + ' 名');
  }
  // 本地排序变了：立即重绘并保存。
  function changed(focusKey) {
    renderRank(focusKey); renderPool(); counts();
    save();
  }
  function showRankErr(text, retryFn) {
    const m = $('rankErr'); m.textContent = text;
    if (retryFn) { m.append(' · '); const b = el('button', 'lnk lnk-pad', '重试'); b.type = 'button'; b.onclick = retryFn; m.append(b); }
    m.hidden = false;
  }
  async function save() {
    unsaved = true; renderMeta();
    if (saving) { again = true; return; }
    saving = true;
    let reload = false;
    for (;;) {
      again = false;
      const codes = order.slice();
      let res = null;
      try { res = await post('/api/rank', {codes, request_id: rid()}); }
      catch (e) { setNet(true); }
      if (res && res.ok) {
        setNet(false); saveError = '';
        saved = res.data.saved; absorb(res.data);
        if (!again && same(order, codes)) unsaved = false;
      } else if (res && res.status < 500) {
        // 被拒绝（如货已下架、不在商品池）：重新读取服务端的榜（期间的改动也不再套用），写明哪一项没保存。
        saveError = '排序未保存：' + (res.data.error || '请刷新本页');
        unsaved = false; again = false; reload = true;
        shout('排序未保存');
      } else {
        saveError = 'retry';
        shout('排序未保存，请重试');
      }
      if (!again) break;
    }
    saving = false;
    if (reload) await loadRank(true);
    renderRank(); renderPool(); counts();
  }
  function rowEl(code, i) {
    const c = cards.get(code) || {code, name: code, tags: [], price: ''};
    const st = info.get(code) || {off: !!c.rank_reason, gone: c.on_sale === false, reason: c.rank_reason || null};
    const li = el('li', 'rr' + (st.off ? ' off' : '')); li.dataset.code = code;
    const line = el('div', 'line');
    const ix = el('span', 'ix', two(i + 1)); ix.setAttribute('aria-hidden', 'true');
    const th = el('span', 'th');
    if (st.off) th.append(el('span', 'plate', '已下架'));
    if (c.image) { const im = el('img'); im.src = c.image; im.alt = ''; th.append(im); }
    th.addEventListener('contextmenu', e => e.preventDefault());
    const sel = el('button', 'sel'); sel.type = 'button'; sel.dataset.k = code + ':sel';
    sel.setAttribute('aria-expanded', selected === code ? 'true' : 'false');
    const h3 = nameEl('h3', c);
    const vis = el('span', 'vh', '第 ' + (i + 1) + ' 名，' + (st.off ? '已下架，' : ''));
    h3.prepend(vis);
    const rp = el('p', 'rp', code + (c.price ? ' · ' + c.price : ''));
    sel.append(h3, rp);
    const sc = scoreEl(c); if (sc) sel.append(sc);
    if (st.off && st.reason && !st.gone) sel.append(el('p', 'why', st.reason));
    // 故事 28：已在已发布 R12 上累计挂了几天；挂满提示天数且未售出时提示可考虑换下（不自动移出）。
    if (st.days > 0) {
      const xp = el('p', 'xd', '已在 R12 ' + st.days + ' 天');
      if (st.stale) xp.append(' · ', el('span', 'stale', '还没卖出，可考虑换下'));
      sel.append(xp);
    }
    const hd = el('button', 'hd', '≡'); hd.type = 'button'; hd.dataset.k = code + ':hd';
    hd.setAttribute('aria-label', '拖动 ' + (c.name || code) + '，第 ' + (i + 1) + ' 名' + (st.off ? '，已下架' : ''));
    line.append(ix, th, sel, hd);
    li.append(line);
    if (selected === code) {
      const mv = el('div', 'mv');
      const up = el('button', 'btn', '上移'); up.type = 'button'; up.dataset.k = code + ':up';
      const down = el('button', 'btn', '下移'); down.type = 'button'; down.dataset.k = code + ':down';
      const rm = el('button', 'btn', '移出 R12'); rm.type = 'button'; rm.dataset.k = code + ':rm';
      const det = el('button', 'lnk lnk-pad', '查看详情'); det.type = 'button'; det.dataset.k = code + ':det';
      up.onclick = () => move(code, order.indexOf(code) - 1, 'up');
      down.onclick = () => move(code, order.indexOf(code) + 1, 'down');
      rm.onclick = () => { const n = li.nextElementSibling || li.previousElementSibling; const k = n ? n.dataset.code + ':sel' : null; dropFromRank(code); refocus(k, $('h03')); };
      det.onclick = () => { if (c.item_id) openDetail(c.item_id, 'rank', det); };
      mv.append(up, down, rm, det);
      li.append(mv);
    }
    line.addEventListener('click', e => {
      if (e.target.closest('.hd') || line.dataset.dragged) return;
      selected = selected === code ? null : code;
      renderRank(code + ':sel');
    });
    bindDrag(hd, li, code, line);
    hd.addEventListener('keydown', e => {
      if (e.key === 'ArrowUp' || e.key === 'ArrowDown') {
        e.preventDefault();
        move(code, order.indexOf(code) + (e.key === 'ArrowUp' ? -1 : 1), 'hd');
      }
    });
    hd.addEventListener('contextmenu', e => e.preventDefault());
    return li;
  }
  function renderRank(focusKey) {
    if (dragging) return;
    const k = focusKey || keyOf();
    const list = $('rankList'); list.textContent = '';
    order.forEach((code, i) => list.append(rowEl(code, i)));
    $('rankEmpty').hidden = order.length > 0 || !rankLoaded;
    renderMeta();
    if (k && k.indexOf(':') > 0 && document.querySelector('[data-k="' + CSS.escape(k) + '"]')) refocus(k);
  }
  function offRows() { return order.map((c, i) => [c, i]).filter(([c]) => (info.get(c) || {}).off); }
  function renderMeta() {
    // 发布状态
    let line = '';
    if (status && status.published_at) {
      line = '已发布 · ' + hhmm(status.published_at);
      if (status.orders > 0) line += ' · 已为 ' + status.orders + ' 位客人排好顺序';
      if (status.dirty || unsaved || !same(order, saved)) line += ' · 有未发布的修改';
    } else if (rankLoaded) line = '尚未发布';
    $('pubLine').textContent = line;
    // 保存失败
    if (saveError === 'retry' && unsaved) showRankErr('排序未保存', save);
    else if (saveError && saveError !== 'retry') showRankErr(saveError, () => { saveError = ''; save(); });
    else $('rankErr').hidden = true;
    // 补位提示
    const off = offRows();
    const offMsg = $('rankOff');
    if (off.length) { offMsg.textContent = '第 ' + off.map(([, i]) => i + 1).join('、') + ' 名已下架或不能发布，请移出（或从 02 商品池换一件）'; offMsg.hidden = false; }
    else offMsg.hidden = true;
    // 预览
    let reason = null;
    if (order.length > SIZE) reason = 'R12 最多 ' + SIZE + ' 件';
    else if (unsaved || saving) reason = '排序尚未保存';
    else if (off.length) reason = 'R12 里有已下架或不能发布的货，请先移出或换一件';
    disable($('btnPreview'), reason, $('whyPreview'));
    if (!previewing) $('btnPreview').textContent = '预览';
  }

  /* ---------- 拖动：鼠标按住整行即可拖（移动超过 4px 拿起）；触摸只在把手上长按（不挡住滚动） ---------- */
  function bindDrag(hd, li, code, line) {
    let timer = null, start = null, pid = null, active = false, from = 0, lastY = 0, raf = 0, mouse = false;
    const mid = n => { const r = n.getBoundingClientRect(); return r.top + r.height / 2; };
    function renumber() {
      [...$('rankList').children].forEach((n, i) => { const ix = n.querySelector('.ix'); if (ix) ix.textContent = two(i + 1); });
    }
    // 拖着的行始终留在 DOM 里（指针捕获不丢），把越过的邻行挪到它另一侧。
    function reorder(y) {
      let p = li.previousElementSibling, n = li.nextElementSibling, moved = false;
      while (p && y < mid(p)) { li.after(p); p = li.previousElementSibling; moved = true; }
      while (n && y > mid(n)) { li.before(n); n = li.nextElementSibling; moved = true; }
      if (moved) renumber();
    }
    // 按住在屏幕上下边缘附近时持续滚动。
    function tick() {
      if (!active) return;
      const edge = lastY < 60 ? -8 : lastY > window.innerHeight - 60 ? 8 : 0;
      if (edge) { window.scrollBy(0, edge); reorder(lastY); }
      raf = requestAnimationFrame(tick);
    }
    function stop() {
      if (timer) { clearTimeout(timer); timer = null; }
      pid = null; cancelAnimationFrame(raf);
      if (!active) return false;
      active = false; dragging = false; cancelDrag = null; li.classList.remove('drag');
      return true;
    }
    function cancel() {
      if (!stop()) return;
      renderRank(code + ':hd');               // 按 order 重画：回到拿起前的位置
      say('已取消拖动，仍在第 ' + (from + 1) + ' 名');
    }
    function pickUp() {
      if (!li.isConnected || pid === null) return;      // 期间列表重画过：放弃
      active = true; dragging = true; cancelDrag = cancel; from = order.indexOf(code);
      li.classList.add('drag');
      say('已拿起，第 ' + (from + 1) + ' 名');
      raf = requestAnimationFrame(tick);
    }
    line.addEventListener('pointerdown', e => {
      if (e.button > 0 || dragging) return;
      mouse = e.pointerType === 'mouse';
      const onHandle = !!e.target.closest('.hd');
      if (!mouse && !onHandle) return;                  // 触摸在行上：留给页面滚动
      start = {x: e.clientX, y: e.clientY}; pid = e.pointerId; lastY = e.clientY;
      try { line.setPointerCapture(pid); } catch (err) { /* 合成事件 */ }
      if (!mouse) timer = setTimeout(() => { timer = null; pickUp(); }, LONG_PRESS);
    });
    line.addEventListener('pointermove', e => {
      if (e.pointerId !== pid) return;
      if (timer) {
        if (Math.hypot(e.clientX - start.x, e.clientY - start.y) > 10) { clearTimeout(timer); timer = null; }
        return;
      }
      if (!active && mouse && start && Math.hypot(e.clientX - start.x, e.clientY - start.y) > 4) pickUp();
      if (!active) return;
      e.preventDefault();
      lastY = e.clientY;
      reorder(lastY);
    });
    line.addEventListener('pointerup', e => {
      if (e.pointerId !== pid || !stop()) return;
      line.dataset.dragged = '1';                       // 吞掉拖动结束后的那次点击（不切换选中）
      setTimeout(() => { delete line.dataset.dragged; }, 0);
      const now = [...$('rankList').children].map(n => n.dataset.code);
      if (!same(now, order)) {
        order = now; changed(code + ':hd');
        say('已放到第 ' + (now.indexOf(code) + 1) + ' 名');
      } else renderRank(code + ':hd');
    });
    line.addEventListener('pointercancel', e => { if (e.pointerId === pid) { if (active) cancel(); else stop(); } });
    line.addEventListener('lostpointercapture', e => { if (pid !== null && e.pointerId === pid) { if (active) cancel(); else stop(); } });
  }

  /* ---------- 预览 ---------- */
  async function preview() {
    const b = $('btnPreview');
    if (b.getAttribute('aria-disabled') === 'true') { say($('whyPreview').textContent); return; }
    if (previewing) return;
    previewing = true; b.textContent = '正在准备预览…'; b.setAttribute('aria-busy', 'true');
    $('previewMsg').hidden = true;
    let res = null;
    try { res = await post('/api/rank/preview', {rank: order}); } catch (e) { setNet(true); }
    previewing = false; b.removeAttribute('aria-busy'); b.textContent = '预览';
    if (res && res.ok) {
      const miss = res.data.no_images || [], failed = res.data.image_failures || 0;
      if (!miss.length && !failed) { location.href = res.data.url; return; }
      // 有货没有可用图片：先告诉墨墨，再由他决定是否继续看预览。
      const m = $('previewMsg');
      m.textContent = (miss.length ? '这些货没有可用图片：' + miss.join('、') + '。' : '') +
        (failed ? failed + ' 张图片未能转码，已跳过。' : '') + ' ';
      const go = el('a', 'lnk lnk-pad', '继续预览'); go.href = res.data.url;
      m.append(go); m.hidden = false; go.focus();
      return;
    }
    const m = $('previewMsg');
    m.textContent = '未能预览' + (res && res.data.error ? '：' + res.data.error : '') + ' · ';
    const again2 = el('button', 'lnk lnk-pad', '重试'); again2.type = 'button'; again2.onclick = preview;
    m.append(again2); m.hidden = false;
    if (res && res.status === 409) loadRank();
  }

  /* ---------- 货详情里的"加入榜 / 移出榜" ---------- */
  window.curioRank = {
    // 货详情里标记售出之后：与 poolAct 一样重新读取商品池与榜。
    reload() { loadPool(); loadRank(); },
    // d：/api/triage/item 的结果；rank_reason 由服务端判定（null 为可以上榜），榜满按当前件数判断。
    primary(d) {
      const code = d.code;
      if (code && order.includes(code)) return {label: '移出 R12', reason: null};
      return {label: '加入 R12', reason: d.rank_reason || (order.length >= SIZE ? REASON_FULL : null)};
    },
    async fromDetail(d) {
      if (!d.code) return '未能加入 R12：这件货还没有货号';
      if (!order.includes(d.code) && !cards.has(d.code)) {
        cards.set(d.code, {item_id: d.item_id, code: d.code, name: d.name, name_lang: d.name_lang, image: d.image,
                           price: d.price, tags: d.tags || [], on_sale: d.status === 'active', rank_reason: d.rank_reason});
      }
      return (order.includes(d.code) ? dropFromRank(d.code) : addToRank(d.code)) || '未能加入 R12';
    }
  };

  /* ---------- 绑定 ---------- */
  $('btnPreview').onclick = preview;
  $('poolReload').onclick = loadPool;
  $('sortScore').onclick = () => setPoolSort('score');
  $('sortTime').onclick = () => setPoolSort('time');
  window.addEventListener('hashchange', () => show(fromHash(), true));
  document.addEventListener('keydown', e => { if (e.key === 'Escape' && cancelDrag) { e.preventDefault(); cancelDrag(); } });
  window.addEventListener('curio:detail-closed', () => {
    if (view === 'pool') loadPool();
    if (view === 'rank' || rankLoaded) loadRank();
  });
  const published = new URLSearchParams(location.search).get('published');
  if (published !== null) {
    history.replaceState(null, '', '/' + location.hash);
    setTimeout(() => say('已发布'), 400);
  }
  show(fromHash(), false);        // 打开的分区按需读取；其余分区的计数也读一次
  if (view !== 'pool') loadPool();
  if (view !== 'rank') loadRank();
})();
