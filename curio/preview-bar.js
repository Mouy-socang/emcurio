/* 预览横条（故事 11，AD-28）："发布"只从这里发起：带预览所见的榜与 request_id 调后台发布接口；
   成功回到 03 R12（显示"已发布 · HH:MM"），失败就地"未发布 · 重试"，排序不动。不记录任何行为。 */
(function () {
  'use strict';
  var bar = document.getElementById('pvbar');
  if (!bar) return;
  var btn = document.getElementById('pvPublish'), msg = document.getElementById('pvMsg');
  var token = bar.getAttribute('data-token'), rank = JSON.parse(bar.getAttribute('data-rank') || '[]');
  var rid = null, busy = false;
  function newRid() {
    return (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : 'p' + Date.now().toString(36) + Math.random().toString(36).slice(2);
  }
  function size() { document.documentElement.style.setProperty('--pvh', bar.offsetHeight + 'px'); }
  size();
  window.addEventListener('resize', size);
  function fail(text) {
    msg.textContent = '未发布' + (text ? '（' + text + '）' : '') + ' · ';
    var again = document.createElement('button');
    again.type = 'button'; again.textContent = '重试';
    again.addEventListener('click', publish);
    msg.appendChild(again);
    msg.hidden = false;
  }
  function done() { busy = false; btn.textContent = '发布'; btn.removeAttribute('aria-busy'); }
  function publish() {
    if (busy) return;
    busy = true;
    rid = rid || newRid();
    msg.hidden = true;
    btn.textContent = '正在发布…'; btn.setAttribute('aria-busy', 'true');
    fetch('/api/rank/publish', {
      method: 'POST', credentials: 'same-origin',
      headers: {'Content-Type': 'application/json', 'X-Curio-Token': token},
      body: JSON.stringify({rank: rank, request_id: rid})
    }).then(function (r) {
      return r.json().catch(function () { return {}; }).then(function (d) { return {ok: r.ok, status: r.status, d: d}; });
    }).then(function (res) {
      if (res.ok) { location.href = '/?published=' + encodeURIComponent(res.d.published_at || '') + '#rank'; return; }
      if (res.status < 500) rid = null;     // 被拒绝（如另一个发布正在进行、排序已变）：下次是新的请求
      done();
      fail(res.d.error || '');
    }).catch(function () {
      done();                                // 网络失败：保留 request_id，重试不会重复发布
      fail('网络已断开');
    });
  }
  btn.addEventListener('click', publish);
})();
