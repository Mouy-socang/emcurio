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
