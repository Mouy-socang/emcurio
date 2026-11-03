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
    var url = '/api/pool?' + (q ? q + '&' : '') + 'offset=' + encodeURIComponent(ol.getAttribute('data-next'));
    fetch(url, { credentials: 'same-origin', headers: { Accept: 'application/json' } }).then(function (r) {
      if (!r.ok) throw new Error('http');
      return r.json();
    }).then(function (d) {
      if (mine !== gen) return;      // 筛选已变：丢掉这批
      ol.insertAdjacentHTML('beforeend', d.html);
      if (window.curioMoney) window.curioMoney();
      ol.setAttribute('data-next', d.next_offset === null ? '' : String(d.next_offset));
      loading = false;
      more.innerHTML = '';
      relayout();
      announce('已加载 ' + d.count + ' 件，共显示 ' + d.shown + ' 件');
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
        if (!r.ok) throw new Error('http');
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
