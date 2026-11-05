/* Curiobject 客人页增强（表单不依赖本脚本也能提交）。 */
(function () {
  'use strict';
  var live = document.getElementById('live');
  function say(text) { if (live) { live.textContent = ''; setTimeout(function () { live.textContent = text; }, 30); } }

  // 同意复选框：未勾选时"注册"为 aria-disabled，点按只朗读原因。
  var consent = document.getElementById('r-consent');
  var gate = document.querySelector('[data-consent-gate]');
  var reason = document.querySelector('[data-consent-reason]');
  function syncGate() {
    if (!consent || !gate) return;
    if (consent.checked) { gate.removeAttribute('aria-disabled'); if (reason) reason.hidden = true; }
    else { gate.setAttribute('aria-disabled', 'true'); if (reason) reason.hidden = false; }
  }
  if (consent) { consent.addEventListener('change', syncGate); syncGate(); }

  // aria-disabled 的提交按钮不提交，只朗读原因。
  document.addEventListener('submit', function (e) {
    var btn = e.target.querySelector('button[type=submit][aria-disabled=true]');
    if (btn) {
      e.preventDefault();
      var why = document.getElementById(btn.getAttribute('aria-describedby'));
      say(why ? why.textContent : '');
      return;
    }
    var b = e.target.querySelector('button[type=submit]');
    if (b && !b.dataset.busy) { b.dataset.busy = '1'; }
    else if (b) { e.preventDefault(); }   // 防重复提交
  });
  // 从往返缓存（后退）回来时解除"提交中"，否则按钮再也点不动。
  window.addEventListener('pageshow', function () {
    document.querySelectorAll('button[data-busy]').forEach(function (b) { delete b.dataset.busy; });
  });

  // 注册/登录切换：不跳页、不丢已填内容；焦点到新表单 h1，标题随之改变。
  document.querySelectorAll('[data-switch]').forEach(function (a) {
    a.addEventListener('click', function (e) {
      var to = document.getElementById(a.getAttribute('data-switch'));
      var from = a.closest('section');
      if (!to || !from) return;
      e.preventDefault();
      from.hidden = true;
      to.hidden = false;
      document.title = to.getAttribute('data-title');
      var h1 = to.querySelector('h1');
      if (h1) h1.focus();
    });
  });

  // 注册表单：去看说明页再回来时保留已填内容与勾选（密码与邀请码除外，不存进浏览器）。
  var form = document.querySelector('form[data-remember]');
  if (form) {
    var key = 'curio-' + form.getAttribute('data-remember');
    var store = null;
    try { store = window.sessionStorage; } catch (err) { store = null; }
    if (store) {
      var saved = null;
      try { saved = JSON.parse(store.getItem(key) || 'null'); } catch (err) { saved = null; }
      if (saved) {
        Array.prototype.forEach.call(form.elements, function (el) {
          if (!el.name || el.type === 'password' || el.type === 'hidden' || el.name === 'invite' || !(el.name in saved)) return;
          if (el.type === 'checkbox') { if (!el.checked) el.checked = !!saved[el.name]; }
          else if (!el.value) el.value = saved[el.name];
        });
        syncGate();
      }
      var save = function () {
        var data = {};
        Array.prototype.forEach.call(form.elements, function (el) {
          if (!el.name || el.type === 'password' || el.type === 'hidden' || el.name === 'invite') return;
          data[el.name] = el.type === 'checkbox' ? el.checked : el.value;
        });
        try { store.setItem(key, JSON.stringify(data)); } catch (err) { /* 存不下就算了 */ }
      };
      form.addEventListener('input', save);
      form.addEventListener('change', save);
    }
  }

  // 已登录（注册完成）后清掉暂存的注册草稿。
  if (document.querySelector('[data-signed-in]')) {
    try { window.sessionStorage.removeItem('curio-register'); } catch (err) { /* 忽略 */ }
  }

  // 说明页"返回"：回到来处（注册页已填内容由上面恢复）。
  var back = document.querySelector('[data-back]');
  if (back && document.referrer && new URL(document.referrer).origin === location.origin) {
    back.addEventListener('click', function (e) { e.preventDefault(); history.back(); });
  }
})();

/* 首页（故事 10）：双列瀑布流按阅读顺序放进较短一列（不用 CSS columns）、筛选底部面板、就地筛选、滚到底自动续载。
   不跑本脚本时：两列网格、面板用 :target 打开、筛选与"加载更多"都是普通链接。 */
(function () {
  'use strict';
  var GAP = 12;
  var polite = document.getElementById('polite');
  function announce(text) { if (polite) { polite.textContent = ''; setTimeout(function () { polite.textContent = text; }, 30); } }

  // ----- 瀑布流：DOM 顺序 = 阅读顺序 = Tab 顺序，视觉上依次放进较短的一列 -----
  function layout() {
    var ol = document.querySelector('ol.wf');
    if (!ol) return;
    ol.classList.add('js');
    var cw = (ol.clientWidth - GAP) / 2, h = [0, 0], cards = ol.children, i;
    for (i = 0; i < cards.length; i++) cards[i].style.width = cw + 'px';
    for (i = 0; i < cards.length; i++) {
      var col = h[0] <= h[1] ? 0 : 1;
      cards[i].style.transform = 'translate(' + (col * (cw + GAP)) + 'px,' + h[col] + 'px)';
      h[col] += cards[i].offsetHeight + GAP;
    }
    ol.style.height = Math.max(h[0], h[1]) + 'px';
  }
  var queued = false;
  function relayout() {
    if (queued) return;
    queued = true;
    requestAnimationFrame(function () { queued = false; layout(); });
  }
  window.addEventListener('resize', relayout);
  if (document.fonts && document.fonts.ready) document.fonts.ready.then(relayout);
  var ro = window.ResizeObserver ? new ResizeObserver(relayout) : null;

  // 图片：加载后（老浏览器不按 width/height 预留比例时）重排；失败时占位里显示"图片未能加载"。
  document.addEventListener('load', function (e) {
    if (e.target.tagName === 'IMG') relayout();
  }, true);
  document.addEventListener('error', function (e) {
    var t = e.target;
    if (t.tagName === 'IMG' && t.parentNode && t.parentNode.classList.contains('ph')) {
      t.parentNode.classList.add('broken');
      relayout();
    }
  }, true);

  // ----- 底部面板（role=dialog）：焦点移入、困在面板内、Esc / 关闭，焦点回到触发按钮 -----
  var opened = null, trigger = null;
  function openSheet(id, from) {
    var s = document.getElementById(id);
    if (!s) return;
    closeSheet(false);
    s.classList.add('open');
    opened = s;
    trigger = from;
    var first = s.querySelector('[aria-checked=true]') || s.querySelector('.opt');
    if (first) first.focus();
  }
  function closeSheet(restore) {
    if (!opened) return;
    opened.classList.remove('open');
    var t = trigger;
    opened = null;
    trigger = null;
    if (location.hash.indexOf('#sheet-') === 0) history.replaceState(null, '', location.pathname + location.search);
    if (restore !== false && t) t.focus();
  }
  // 不跑脚本时用 :target 打开的面板：脚本接管为 .open。
  if (location.hash.indexOf('#sheet-') === 0) {
    var pre = document.getElementById(location.hash.slice(1));
    if (pre) openSheet(pre.id, document.getElementById(pre.getAttribute('data-trigger')));
  }

  document.addEventListener('keydown', function (e) {
    var a = e.target;
    if ((e.key === ' ' || e.key === 'Spacebar') && a.tagName === 'A' && /^(button|radio)$/.test(a.getAttribute('role') || '')) {
      e.preventDefault();
      a.click();
      return;
    }
    if (!opened) return;
    if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); closeSheet(); return; }
    if (e.key === 'Tab') {
      var f = opened.querySelectorAll('a[href]');
      if (!f.length) return;
      if (e.shiftKey && document.activeElement === f[0]) { e.preventDefault(); f[f.length - 1].focus(); }
      else if (!e.shiftKey && document.activeElement === f[f.length - 1]) { e.preventDefault(); f[0].focus(); }
    }
  });

  // 筛选地址里的所选值（品牌 / 器型 / 价格区间 / 图案），作为 filter 事件的 params（故事 15）。
  function filterParams(href) {
    var out = {}, q = (href.split('#')[0].split('?')[1] || '').split('&');
    q.forEach(function (kv) {
      var i = kv.indexOf('=');
      if (i < 1) return;
      var k = kv.slice(0, i), v;
      try { v = decodeURIComponent(kv.slice(i + 1).replace(/\+/g, ' ')); } catch (err) { return; }
      if (/^(brand|form|band|pattern)$/.test(k) && v) out[k] = v;
    });
    return out;
  }

  // ----- 就地筛选：取同一地址的整页 HTML，替换 [data-swap] 区块（汇总行是 role=status，原地更新即播报） -----
  function applyFilter(a) {
    if (!window.fetch || !window.DOMParser) { location.href = a.href; return; }
    var href = a.getAttribute('href');
    var triggerId = opened ? opened.getAttribute('data-trigger') : null;
    var tabs = Array.prototype.slice.call(document.querySelectorAll('.tc'));
    var tabIndex = tabs.indexOf(a);
    closeSheet(false);
    gen++;            // 作废进行中的续载：旧条件的卡片不能接到新列表后面
    loading = false;
    fetch(href, { credentials: 'same-origin' }).then(function (r) {
      if (!r.ok) throw new Error('http');
      return r.text();
    }).then(function (text) {
      var doc = new DOMParser().parseFromString(text, 'text/html');
      if (!doc.getElementById('pool-list')) throw new Error('page');   // 登录过期等：整页跳转
      document.querySelectorAll('[data-swap]').forEach(function (el) {
        var n = doc.getElementById(el.id);
        if (n) el.innerHTML = n.innerHTML;
      });
      history.replaceState(null, '', href.split('#')[0] || '/');
      if (window.curioTrack) window.curioTrack('filter', null, filterParams(href));
      if (window.curioMoney) window.curioMoney();
      var here = document.querySelector('form[data-currency] [name=next]');
      if (here) here.value = href.split('#')[0] || '/';
      setupList();
      var pool = document.getElementById('pool');
      if (pool) pool.scrollIntoView();
      var target = triggerId ? document.getElementById(triggerId)
        : tabIndex >= 0 ? document.querySelectorAll('.tc')[tabIndex] : document.getElementById('h-pool');
      if (target) {
        if (target.id === 'h-pool') target.setAttribute('tabindex', '-1');
        target.focus({ preventScroll: true });
      }
    }).catch(function () { location.href = href; });
  }

  document.addEventListener('click', function (e) {
    var a = e.target.closest ? e.target.closest('a,button') : null;
    if (!a) return;
    if (a.hasAttribute('data-sheet')) { e.preventDefault(); openSheet(a.getAttribute('data-sheet'), a); }
    else if (a.hasAttribute('data-close')) { e.preventDefault(); closeSheet(); }
    else if (a.hasAttribute('data-filter')) { e.preventDefault(); applyFilter(a); }
    else if (a.hasAttribute('data-retry')) { e.preventDefault(); loadMore(); }
  });

  // ----- 滚到底自动续载（每批 20 件，顺序不变；失败可重试） -----
  var io = null, loading = false, gen = 0;
  function setupList() {
    var ol = document.querySelector('ol.wf');
    if (ro) { ro.disconnect(); if (ol) ro.observe(ol); }
    relayout();
    if (io) { io.disconnect(); io = null; }
    var more = document.querySelector('[data-more]');
    if (!ol || !more || !window.IntersectionObserver || !window.fetch || ol.getAttribute('data-next') === '') return;
    more.innerHTML = '';
    io = new IntersectionObserver(function (entries) {
      if (entries[0].isIntersecting) loadMore();
    }, { rootMargin: '600px 0px' });
    io.observe(more);
  }
  function loadMore() {
    var ol = document.querySelector('ol.wf'), more = document.querySelector('[data-more]');
    if (loading || !ol || !more || ol.getAttribute('data-next') === '') return;
    loading = true;
    var mine = gen;
    more.innerHTML = '<p class="muted">正在加载…</p>';
    var q = ol.getAttribute('data-query');
    var url = '/api/pool?' + (q ? q + '&' : '') + 'offset=' + encodeURIComponent(ol.getAttribute('data-next')) +
      '&pn=' + encodeURIComponent(ol.getAttribute('data-pn') || '');
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('http');
      return r.json();
    }).then(function (d) {
      if (mine !== gen) return;      // 筛选已变：丢掉这批
      // 续载期间发布了新快照：服务器从头给了第一批，整列替换（避免重复或漏掉）。
      if (d.reset) { ol.innerHTML = d.html; ol.setAttribute('data-pn', String(d.publish_no)); }
      else ol.insertAdjacentHTML('beforeend', d.html);
      if (window.curioMoney) window.curioMoney();
      ol.setAttribute('data-next', d.next_offset === null ? '' : String(d.next_offset));
      loading = false;
      more.innerHTML = '';
      relayout();
      announce(d.reset ? '藏品已更新，已从头重新显示' : '已加载 ' + d.count + ' 件，共显示 ' + d.shown + ' 件');
      if (io) {
        io.unobserve(more);
        if (d.next_offset === null) { io.disconnect(); io = null; } else io.observe(more);
      }
    }).catch(function () {
      if (mine !== gen) return;
      loading = false;
      more.innerHTML = '<p class="im">未能加载更多藏品 · <button type="button" class="linkbtn" data-retry>重试</button></p>';
    });
  }
  setupList();
})();

/* 故事 12：币种切换（首页与单件页页头）、单件页画廊与放大层、复制货号。
   不跑本脚本时：币种是表单提交、画廊横向滑动加缩略图锚点、货号是可选中的文字。 */
(function () {
  'use strict';
  var polite = document.getElementById('polite'), live = document.getElementById('live');
  function announce(el, text) { if (el) { el.textContent = ''; setTimeout(function () { el.textContent = text; }, 30); } }

  // ----- 币种：页内就地换算（data-eur 为欧元原值），选择存进账号 -----
  function group(n) { return String(n).replace(/\B(?=(\d{3})+(?!\d))/g, ','); }
  function eurText(v) {           // 与服务器 price_text 一致：整数欧元 €1,250；有小数时两位 €99.50
    if (Number.isInteger(v)) return '€' + group(v);
    var p = v.toFixed(2).split('.');
    return '€' + group(p[0]) + '.' + p[1];
  }
  function render(code, rate) {
    document.querySelectorAll('.m[data-eur]').forEach(function (el) {
      var v = parseFloat(el.getAttribute('data-eur'));
      if (!isFinite(v)) return;
      if (code === 'CNY') {
        var n = Math.round(v * rate);
        el.innerHTML = '';
        var a = document.createElement('span'); a.setAttribute('aria-hidden', 'true'); a.textContent = '≈ ¥' + group(n);
        var b = document.createElement('span'); b.className = 'vh'; b.textContent = '约 ' + n + ' 元人民币';
        el.appendChild(a); el.appendChild(b);
      } else {
        el.textContent = (el.hasAttribute('data-approx') ? '约 ' : '') + eurText(v);
      }
    });
    document.querySelectorAll('[data-label]').forEach(function (a) {
      var m = a.querySelector('.m[data-eur]');
      if (!m) return;
      var v = parseFloat(m.getAttribute('data-eur'));
      a.setAttribute('aria-label', a.getAttribute('data-label') + '，' +
        (code === 'CNY' ? '约 ' + Math.round(v * rate) + ' 元人民币' : eurText(v)));
    });
    document.querySelectorAll('[data-cny-note]').forEach(function (n) { n.hidden = code !== 'CNY'; });
  }
  function setChecked(form, code) {
    var opts = form.querySelectorAll('[role=radio]');
    opts.forEach(function (b) {
      var on = b.value === code;
      b.setAttribute('aria-checked', on ? 'true' : 'false');
      b.tabIndex = on ? 0 : -1;
    });
  }
  // 当前选中的币种：续载或就地筛选插进来的新卡片（服务器可能还按旧币种渲染）由 window.curioMoney 重新换算。
  var active = null;
  window.curioMoney = function () { if (active) render(active.code, active.rate); };
  document.querySelectorAll('[data-currency]').forEach(function (form) {
    var rate = parseFloat(form.getAttribute('data-fx'));
    var checked = form.querySelector('[aria-checked=true]');
    setChecked(form, checked ? checked.value : 'EUR');
    active = { code: checked ? checked.value : 'EUR', rate: rate };
    var busy = false;
    function choose(code, btn) {
      var was = form.querySelector('[aria-checked=true]');
      if (!isFinite(rate) || busy || (was && was.value === code)) return;
      setChecked(form, code);
      active = { code: code, rate: rate };
      render(code, rate);
      if (btn) btn.focus();
      announce(polite, code === 'CNY' ? '价格已切换为人民币，约数，以欧元为准' : '价格已切换为欧元');
      if (form.hasAttribute('data-local')) return;          // 预览：只在页面内切换
      busy = true;
      var body = 'currency=' + encodeURIComponent(code) + '&next=' + encodeURIComponent(form.elements.next.value);
      fetch(form.action, { method: 'POST', credentials: 'same-origin',
        headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' }, body: body
      }).then(function (r) {
        busy = false;
        if (!r.ok) throw new Error('http');       // currency 事件由服务器在 /currency 里记（无脚本的表单提交同样记）
      }).catch(function () {
        busy = false;
        var back = was ? was.value : 'EUR';
        setChecked(form, back);
        active = { code: back, rate: rate };
        render(back, rate);
        announce(live, '未能保存币种，请重试');
      });
    }
    form.querySelectorAll('[role=radio]').forEach(function (b) {
      b.addEventListener('click', function (e) { e.preventDefault(); choose(b.value, b); });
    });
    if (form.tagName === 'FORM') form.addEventListener('submit', function (e) {
      e.preventDefault();
      e.stopPropagation();          // 不让通用的"防重复提交"拦住下一次切换
      var b = e.submitter || document.activeElement;
      if (b && b.value) choose(b.value, b);
    });
    form.addEventListener('keydown', function (e) {
      if (!/^(ArrowLeft|ArrowRight|ArrowUp|ArrowDown)$/.test(e.key)) return;
      var opts = Array.prototype.slice.call(form.querySelectorAll('[role=radio]'));
      var i = opts.indexOf(document.activeElement);
      if (i < 0) return;
      e.preventDefault();
      var next = opts[(i + (e.key === 'ArrowLeft' || e.key === 'ArrowUp' ? opts.length - 1 : 1)) % opts.length];
      choose(next.value, next);
    });
  });

  // ----- 单件页画廊：左右滑（原生横向滚动）、上一张 / 下一张、缩略图、页码 -----
  var gal = document.querySelector('[data-gallery]');
  var track = gal ? gal.querySelector('[data-track]') : null;
  var slides = track ? Array.prototype.slice.call(track.children) : [];
  var thumbs = Array.prototype.slice.call(document.querySelectorAll('[data-thumb]'));
  var page = gal ? gal.querySelector('[data-page]') : null;
  var gi = 0;
  function show(n, smooth) {
    if (!slides.length) return;
    gi = (n + slides.length) % slides.length;
    track.scrollTo({ left: gi * track.clientWidth, behavior: smooth ? 'smooth' : 'auto' });
    mark();
  }
  function mark() {
    if (page) page.textContent = (gi + 1) + ' / ' + slides.length;
    thumbs.forEach(function (t, k) { if (k === gi) t.setAttribute('aria-current', 'true'); else t.removeAttribute('aria-current'); });
  }
  function zbtnOf() { return gal.querySelector('[data-open-zoom]') || track; }
  if (track && slides.length) {
    var tick = null;
    track.addEventListener('scroll', function () {
      if (tick) return;
      tick = requestAnimationFrame(function () {
        tick = null;
        var n = Math.round(track.scrollLeft / Math.max(1, track.clientWidth));
        if (n !== gi && n >= 0 && n < slides.length) { gi = n; mark(); }
      });
    });
    gal.querySelectorAll('[hidden][data-prev],[hidden][data-next],[hidden][data-open-zoom]').forEach(function (b) { b.hidden = false; });
    var prev = gal.querySelector('[data-prev]'), next = gal.querySelector('[data-next]');
    if (prev) prev.addEventListener('click', function () { show(gi - 1, true); });
    if (next) next.addEventListener('click', function () { show(gi + 1, true); });
    thumbs.forEach(function (t, k) {
      t.setAttribute('role', 'button');
      t.addEventListener('click', function (e) { e.preventDefault(); show(k, true); });
    });
    slides.forEach(function (s, k) {
      var img = s.querySelector('img');
      // 点图片打开放大层：关闭后焦点回到"放大"按钮（图片本身不可聚焦）。
      if (img) { img.style.cursor = 'zoom-in'; img.addEventListener('click', function () { openZoom(zbtnOf()); }); }
    });
    var zbtn = zbtnOf();
    if (zbtn) zbtn.addEventListener('click', function () { openZoom(zbtn); });
    window.addEventListener('resize', function () { show(gi, false); });
  }

  // ----- 放大层：焦点到"关闭"、困在层内、Esc / 关闭；双指缩放与拖动之外有 + / − / 复位与上一张 / 下一张 -----
  var zl = document.querySelector('[data-zoom-layer]');
  var zimg = zl ? zl.querySelector('[data-zimg]') : null, zstage = zl ? zl.querySelector('[data-zstage]') : null;
  var zs = 1, zx = 0, zy = 0, opener = null;
  function zApply() { zimg.style.transform = 'translate(' + zx + 'px,' + zy + 'px) scale(' + zs + ')'; }
  function zClamp() {
    var r = zstage.getBoundingClientRect(), mx = (zs - 1) * r.width / 2, my = (zs - 1) * r.height / 2;
    zx = Math.max(-mx, Math.min(mx, zx)); zy = Math.max(-my, Math.min(my, zy));
  }
  function zSet(s) { zs = Math.max(1, Math.min(6, s)); zClamp(); zApply(); }
  function zReset() { zs = 1; zx = zy = 0; zApply(); }
  function zShow() {
    var src = slides[gi] ? slides[gi].querySelector('img') : null;
    if (!src) return;
    zimg.src = src.currentSrc || src.src; zimg.alt = src.alt;
    zl.querySelector('[data-zpage]').textContent = (gi + 1) + ' / ' + slides.length;
    zReset();
  }
  function openZoom(from) {
    if (!zl) return;
    opener = from;
    zl.hidden = false;
    document.body.classList.add('zl-open');
    zShow();
    zl.querySelector('[data-zclose]').focus();
  }
  function closeZoom() {
    if (!zl || zl.hidden) return;
    zl.hidden = true;
    document.body.classList.remove('zl-open');
    pts = {}; pinch = pan = null;
    show(gi, false);
    if (opener) opener.focus();
  }
  var pts = {}, pinch = null, pan = null;
  if (zl) {
    zl.querySelector('[data-zclose]').addEventListener('click', closeZoom);
    zl.querySelector('[data-zin]').addEventListener('click', function () { zSet(zs * 1.5); });
    zl.querySelector('[data-zout]').addEventListener('click', function () { zSet(zs / 1.5); });
    zl.querySelector('[data-zreset]').addEventListener('click', zReset);
    var zp = zl.querySelector('[data-zprev]'), zn = zl.querySelector('[data-znext]');
    if (zp) zp.addEventListener('click', function () { gi = (gi - 1 + slides.length) % slides.length; mark(); zShow(); });
    if (zn) zn.addEventListener('click', function () { gi = (gi + 1) % slides.length; mark(); zShow(); });
    var list = function () { return Object.keys(pts).map(function (k) { return pts[k]; }); };
    var dist = function () { var p = list(); return Math.hypot(p[0].x - p[1].x, p[0].y - p[1].y) || 1; };
    zstage.addEventListener('pointerdown', function (e) {
      zstage.setPointerCapture(e.pointerId); pts[e.pointerId] = { x: e.clientX, y: e.clientY };
      var n = list().length;
      if (n === 2) { pinch = { d: dist(), s: zs }; pan = null; }
      else if (n === 1) pan = { x: e.clientX, y: e.clientY, zx: zx, zy: zy };
    });
    zstage.addEventListener('pointermove', function (e) {
      if (!pts[e.pointerId]) return;
      pts[e.pointerId] = { x: e.clientX, y: e.clientY };
      var n = list().length;
      if (n === 2 && pinch) zSet(pinch.s * dist() / pinch.d);
      else if (n === 1 && pan && zs > 1) { zx = pan.zx + e.clientX - pan.x; zy = pan.zy + e.clientY - pan.y; zClamp(); zApply(); }
    });
    var up = function (e) {
      delete pts[e.pointerId];
      var p = list();
      if (p.length < 2) pinch = null;
      pan = p.length ? { x: p[0].x, y: p[0].y, zx: zx, zy: zy } : null;
    };
    zstage.addEventListener('pointerup', up);
    zstage.addEventListener('pointercancel', up);
    zstage.addEventListener('dblclick', function () { if (zs > 1) zReset(); else zSet(2.5); });
    document.addEventListener('keydown', function (e) {
      if (zl.hidden) return;
      if (e.key === 'Escape' || e.key === 'Esc') { e.preventDefault(); closeZoom(); return; }
      if (e.key === 'Tab') {
        var f = Array.prototype.slice.call(zl.querySelectorAll('button')).filter(function (b) { return !b.hidden; });
        if (!f.length) return;
        if (e.shiftKey && (document.activeElement === f[0] || !zl.contains(document.activeElement))) { e.preventDefault(); f[f.length - 1].focus(); }
        else if (!e.shiftKey && (document.activeElement === f[f.length - 1] || !zl.contains(document.activeElement))) { e.preventDefault(); f[0].focus(); }
      }
    });
  }

  // ----- 复制货号：成功提示常驻；剪贴板不可用时显示已全选的只读输入框 -----
  var bar = document.querySelector('[data-copybar]');
  if (bar) {
    var code = bar.getAttribute('data-code');
    var btn = bar.querySelector('[data-copy]'), msg = bar.querySelector('[data-copy-msg]');
    var fallback = bar.querySelector('[data-copy-fallback]'), input = bar.querySelector('#copy-code');
    btn.hidden = false;
    var legacy = function () {
      var ta = document.createElement('textarea');
      ta.value = code; ta.setAttribute('readonly', ''); ta.className = 'vh';
      document.body.appendChild(ta); ta.select();
      var ok = false;
      try { ok = document.execCommand('copy'); } catch (err) { ok = false; }
      document.body.removeChild(ta);
      return ok;
    };
    var done = function () {
      fallback.hidden = true;
      msg.textContent = '已复制 ' + code + '，回到微信粘贴到转账备注';
      msg.hidden = false;
      announce(polite, '已复制 ' + code);
    };
    var failed = function () {
      msg.textContent = '未能自动复制，请长按选择货号 ' + code;
      msg.hidden = false;
      fallback.hidden = false;
      input.focus();
      input.select();
      try { input.setSelectionRange(0, code.length); } catch (err) { /* 部分浏览器不支持 */ }
      announce(live, '未能自动复制，货号 ' + code + '，请手动复制');
    };
    btn.addEventListener('click', function () {
      if (navigator.clipboard && window.isSecureContext) {
        navigator.clipboard.writeText(code).then(done, function () { if (legacy()) done(); else failed(); });
      } else if (legacy()) done();
      else failed();
    });
    input.addEventListener('focus', function () { input.select(); });
  }
})();

/* 状态未更新提示（AD-10，故事 13）：页面加载时在 polite 区读一次。 */
(function () {
  'use strict';
  var note = document.querySelector('[data-stale-note]');
  var polite = document.getElementById('polite');
  if (!note || !polite) return;
  setTimeout(function () { polite.textContent = note.textContent; }, 400);
})();

/* 故事 15（CAP-15，AD-25）：行为事件。只在服务器上已登录客人的首页与单件页运行（页面带 data-track-page）；
   Mac 预览（AD-28）、停用页、待同意页没有该标记，不生成也不上报。币种切换由服务器在 POST /currency 里记录。
   事件先进本机队列（localStorage，带 event_id 与账号标记 data-who），每批最多 50 条 POST /events；失败留在队列里下次重试
   （同一 event_id，服务器去重）。队列里别的账号（同一设备换了登录）留下的事件直接丢掉，不记到当前账号名下。
   单件页停留在页面隐藏或离开时记一段（1–1800 秒）并用 sendBeacon 发出；beacon 只表示浏览器接下了，事件仍留在队列里，
   下次 fetch 收到服务器答复才删（重复的由服务器按 event_id 去掉）。 */
(function () {
  'use strict';
  var page = document.querySelector('[data-track-page]');
  if (!page || /^\/preview(\/|$)/.test(location.pathname)) return;
  var who = page.getAttribute('data-who') || '';
  if (!who) return;
  var KEY = 'curio-events', FROM = 'curio-from', BATCH = 50, MAX_QUEUE = 200, MAX_AGE = 7 * 86400 * 1000, MAX_EVENT = 2048;
  var store = null, broken = false, mem = [];      // mem：本页的队列副本；存储不可用（或写失败）后只用它
  try { store = window.localStorage; } catch (err) { store = null; }

  function stored() {
    if (!store || broken) return null;
    try {
      var q = JSON.parse(store.getItem(KEY) || '[]');
      return Array.isArray(q) ? q : [];
    } catch (err) { return []; }
  }
  function keep(x) {             // 只留当前账号、未过期、结构完整的条目
    var t = x && x.e ? Date.parse(x.e.client_ts) : NaN;
    return x && x.w === who && x.e && typeof x.e.event_id === 'string' && isFinite(t) && Date.now() - t < MAX_AGE;
  }
  // 写回前重新读一次存储，按 event_id 合并（别的标签页刚加的不会被覆盖），再去掉 gone 里的。
  function save(adds, gone) {
    var base = stored(), out = [], seen = {}, rm = {};
    (gone || []).forEach(function (id) { rm[id] = 1; });
    // 存储可用时以存储为准（别的标签页已发掉删除的不会被本页旧副本加回来）；不可用时用内存队列。
    (base === null ? mem : base).concat(adds || []).forEach(function (x) {
      if (!keep(x) || seen[x.e.event_id] || rm[x.e.event_id]) return;   // 别的账号留下的、过期的：丢掉
      seen[x.e.event_id] = 1;
      out.push(x);
    });
    if (out.length > MAX_QUEUE) out = out.slice(out.length - MAX_QUEUE);
    mem = out;
    if (store && !broken) {
      try { store.setItem(KEY, JSON.stringify(out)); }
      catch (err) { broken = true; }   // 存不下（配额 / 无痕模式）：从此只用内存队列
    }
    return out;
  }
  function queue() { return save([], []); }
  function uuid() {
    var c = window.crypto;
    if (c && c.randomUUID) { try { return c.randomUUID(); } catch (err) { /* 非安全上下文 */ } }
    var b = new Uint8Array(16), i;
    if (c && c.getRandomValues) c.getRandomValues(b); else for (i = 0; i < 16; i++) b[i] = Math.random() * 256 | 0;
    b[6] = (b[6] & 15) | 64; b[8] = (b[8] & 63) | 128;
    var h = '';
    for (i = 0; i < 16; i++) h += (b[i] + 256).toString(16).slice(1);
    return h.slice(0, 8) + '-' + h.slice(8, 12) + '-' + h.slice(12, 16) + '-' + h.slice(16, 20) + '-' + h.slice(20);
  }
  function ids(batch) { return batch.map(function (x) { return x.e.event_id; }); }
  function body(batch) { return JSON.stringify({ schema_version: 1, events: batch.map(function (x) { return x.e; }) }); }

  var timer = null, sending = false;
  function schedule(ms) { if (!timer) timer = setTimeout(function () { timer = null; flush(); }, ms || 1500); }
  function flush() {
    if (sending || !window.fetch) return;
    var q = queue();
    if (!q.length) return;
    var batch = q.slice(0, BATCH);
    sending = true;
    fetch('/events', { method: 'POST', credentials: 'same-origin', keepalive: true,
      headers: { 'Content-Type': 'application/json' }, body: body(batch)
    }).then(function (r) {
      sending = false;
      // 收下（含重复、超限丢弃）、拒收（400：如枚举外类型）、未登录 / 不记录（401 / 403）：都不再重试这批。
      if (r.ok || r.status === 400 || r.status === 401 || r.status === 403 || r.status === 413 || r.status === 415) {
        if (save([], ids(batch)).length) schedule(200);
      } else schedule(30000);
    }).catch(function () { sending = false; schedule(30000); });
  }
  function beacon() {
    var q = queue();
    if (!q.length) return;
    var ok = false;
    if (navigator.sendBeacon) {
      try { ok = navigator.sendBeacon('/events', new Blob([body(q.slice(0, BATCH))], { type: 'text/plain;charset=UTF-8' })); }
      catch (err) { ok = false; }
    }
    if (!ok) flush();             // 发出与否都留在队列里，等 fetch 的答复再删
  }
  function add(type, code, params) {
    var e = { event_id: uuid(), type: type, co_code: code || null, client_ts: new Date().toISOString(),
      params: params || {}, event_schema_version: 1 };
    if (JSON.stringify(e).length > MAX_EVENT) return;     // 单条过大：不进队列（否则整批 413 会连带丢掉别的）
    save([{ w: who, e: e }], []);
    schedule();
  }
  window.curioTrack = add;

  function itemCode(a) {
    try {
      var m = /^\/item\/(CO-[0-9]{4,8})$/.exec(new URL(a.getAttribute('href'), location.href).pathname);
      return m ? m[1] : null;
    } catch (err) { return null; }
  }

  var kind = page.getAttribute('data-track-page');
  if (kind === 'home') {
    var params = {};
    try { params = JSON.parse(page.getAttribute('data-params') || '{}') || {}; } catch (err) { params = {}; }
    add('home_open', null, params);
    // 点开单件：记下来自榜还是商品池，单件页据此填 item_open 的 from。
    document.addEventListener('click', function (e) {
      var a = e.target.closest ? e.target.closest('a.rk-a, a.card-a') : null;
      var code = a && !a.classList.contains('au-a') ? itemCode(a) : null;     // 拍卖中一栏（故事 30）不算榜，记为 link
      if (!code) return;
      try {
        window.sessionStorage.setItem(FROM, JSON.stringify({ code: code, from: a.classList.contains('rk-a') ? 'rank' : 'pool', at: Date.now() }));
      } catch (err) { /* 记不下就算 link */ }
    }, true);
  } else if (kind === 'item') {
    var code = page.getAttribute('data-code'), from = 'link';
    try {
      var f = JSON.parse(window.sessionStorage.getItem(FROM) || 'null');
      window.sessionStorage.removeItem(FROM);
      if (f && f.code === code && Date.now() - f.at < 60000 && (f.from === 'rank' || f.from === 'pool')) from = f.from;
    } catch (err) { from = 'link'; }
    add('item_open', code, { from: from });
    var shownAt = document.visibilityState === 'hidden' ? null : Date.now();
    var endDwell = function () {
      if (shownAt === null) return;
      var s = Math.round((Date.now() - shownAt) / 1000);
      shownAt = null;
      if (s >= 1) add('item_dwell', code, { seconds: Math.min(s, 1800) });
    };
    document.addEventListener('visibilitychange', function () {
      if (document.visibilityState === 'hidden') { endDwell(); beacon(); }
      else if (shownAt === null) shownAt = Date.now();
    });
    window.addEventListener('pagehide', function () { endDwell(); beacon(); });
    window.addEventListener('pageshow', function (e) {
      if (e.persisted && shownAt === null && document.visibilityState !== 'hidden') shownAt = Date.now();
    });
  }
  if (kind !== 'item') {
    window.addEventListener('pagehide', beacon);
    document.addEventListener('visibilitychange', function () { if (document.visibilityState === 'hidden') beacon(); });
  }
})();

/* 故事 31：喜欢 / 想要。只在服务器上已登录客人的页面运行（页面带 data-track-page 与 data-who）；Mac 预览里按钮是
   disabled、不在表单里，这里也不运行。每个按钮是普通表单（POST /mark，带目标状态 on=1/0）；这里改为就地切换：
   先改显示（同一件在页内的所有按钮一起改），再以 JSON 提交，失败回滚并提示。连点时以最后一次为准：同一件同一动作
   同时只发一个请求，回来后若与最后一次点的不同再发一次（服务器按目标状态写，幂等）。 */
(function () {
  'use strict';
  var page = document.querySelector('[data-track-page]');
  if (!page || !page.getAttribute('data-who') || /^\/preview(\/|$)/.test(location.pathname) || !window.fetch) return;
  var READS_MIN = 10;
  var want = {}, server = {}, busy = {};      // 键 code|kind：最后一次点的目标状态、服务器确认的状态、是否在途

  function boxes(code) {
    return Array.prototype.filter.call(document.querySelectorAll('[data-marks]'), function (b) {
      return b.getAttribute('data-code') === code;
    });
  }
  function countText(likes, reads) {
    var parts = [];
    if (likes > 0) parts.push('♡ ' + likes);
    if (reads >= READS_MIN) parts.push('阅读 ' + reads);
    return parts.join(' · ');
  }
  function paint(code, kind, on, likes) {
    boxes(code).forEach(function (box) {
      if (likes != null) box.setAttribute('data-likes', String(likes));
      var n = box.querySelector('[data-mk-count]');
      if (n) n.textContent = countText(parseInt(box.getAttribute('data-likes'), 10) || 0, parseInt(box.getAttribute('data-reads'), 10) || 0);
      var lb = box.querySelector('[data-mark="like"]'), nl = parseInt(box.getAttribute('data-likes'), 10) || 0;
      if (lb) lb.setAttribute('data-n', nl > 0 ? String(nl) : '');   // 今日榜卡把人数放在 ♡ 按钮里
      var b = box.querySelector('[data-mark="' + kind + '"]');
      if (!b) return;
      b.classList.toggle('on', on);
      b.setAttribute('aria-pressed', on ? 'true' : 'false');
      var l = b.querySelector('.mk-l'), i = b.querySelector('.mk-i');
      if (l) l.textContent = kind === 'like' ? (on ? '已喜欢' : '喜欢') : (on ? '已想要' : '想要这件');
      if (i) i.textContent = on ? '♥' : '♡';
      var f = b.form;
      if (f && f.elements.on) f.elements.on.value = on ? '0' : '1';   // 无脚本回退时提交的仍是"另一状态"
    });
  }
  function note(code, text) {
    boxes(code).forEach(function (box) {
      var p = box.querySelector('.mk-msg');
      if (!p) { p = document.createElement('p'); p.className = 'im mk-msg'; p.setAttribute('role', 'alert'); box.appendChild(p); }
      p.textContent = text;
      p.hidden = !text;
    });
  }
  function likesNow(code) {
    var b = boxes(code)[0];
    return b ? parseInt(b.getAttribute('data-likes'), 10) || 0 : 0;
  }
  function send(code, kind, next) {
    var key = code + '|' + kind;
    if (busy[key]) return;
    var target = want[key];
    busy[key] = true;
    var body = 'code=' + encodeURIComponent(code) + '&kind=' + kind + '&on=' + (target ? '1' : '0') + '&next=' + encodeURIComponent(next);
    fetch('/mark', { method: 'POST', credentials: 'same-origin',
      headers: { 'Content-Type': 'application/x-www-form-urlencoded', Accept: 'application/json' }, body: body
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return { r: r, d: d }; });
    }).then(function (x) {
      busy[key] = false;
      if (!x.r.ok || !x.d.ok) {
        rollback(code, kind);
        // 停用（403）等：页面提示；喜欢人数回到点之前。
        note(code, x.r.status === 403 ? (x.d.detail || '无权操作') : x.r.status === 401 ? '登录已过期，请重新登录。'
          : x.r.status === 404 ? '此件已不在展出。' : '未能保存，请稍后再试。');
        return;
      }
      server[key] = x.d.on;
      note(code, '');
      if (want[key] !== x.d.on) { send(code, kind, next); return; }     // 在途时又点了：按最后一次再发
      paint(code, kind, x.d.on, x.d.likes);
    }).catch(function () {
      busy[key] = false;
      rollback(code, kind);
      note(code, '未能保存，请稍后再试。');
    });
  }
  function rollback(code, kind) {           // 回到服务器确认的状态；喜欢人数按显示与确认之差改回
    var key = code + '|' + kind, back = server[key], shown = want[key];
    want[key] = back;
    paint(code, kind, back, kind === 'like' && shown !== back ? Math.max(0, likesNow(code) + (back ? 1 : -1)) : null);
  }
  document.addEventListener('submit', function (e) {
    var f = e.target;
    if (!f.classList || !f.classList.contains('mk-f')) return;
    e.preventDefault();
    e.stopPropagation();          // 不让通用的"防重复提交"拦住下一次点按
    var code = f.elements.code.value, kind = f.elements.kind.value, key = code + '|' + kind;
    var b = f.querySelector('[data-mark]');
    var cur = b ? b.getAttribute('aria-pressed') === 'true' : false;
    if (!(key in server)) server[key] = cur;
    var on = !cur;
    want[key] = on;
    var likes = kind === 'like' ? Math.max(0, likesNow(code) + (on ? 1 : -1)) : null;
    paint(code, kind, on, likes);
    send(code, kind, f.elements.next.value);
  }, true);
})();
