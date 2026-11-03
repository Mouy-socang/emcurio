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
