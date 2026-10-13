/* RDC-EB page helpers. */
(function () {
  var meta = document.querySelector('meta[name="csrf-token"]');
  var token = meta ? meta.content : '';
  var _fetch = window.fetch;
  /* every state-changing same-origin request carries the session's CSRF token (also the shared RDC-OPS helpers) */
  window.fetch = function (url, opts) {
    opts = opts || {};
    var m = (opts.method || 'GET').toUpperCase();
    if (m !== 'GET' && m !== 'HEAD' && token && (typeof url !== 'string' || url.charAt(0) === '/')) {
      opts.headers = Object.assign({}, opts.headers || {}, { 'X-CSRF-Token': token });
    }
    return _fetch.call(window, url, opts);
  };

  window.ebPost = function (url, data, done) {
    var body = (data instanceof FormData || data instanceof URLSearchParams) ? data : new URLSearchParams(data || {});
    return fetch(url, { method: 'POST', body: body, headers: data instanceof FormData ? {} : { 'Content-Type': 'application/x-www-form-urlencoded' } })
      .then(function (r) { return r.json().catch(function () { return { error: 'The server refused the request (' + r.status + ').' }; }); })
      .then(function (j) {
        if (j.error) showToast(j.error, 'error');
        else { if (j.message) showToast(j.message, 'success'); if (done) done(j); }
        return j;
      })
      .catch(function (e) { showToast('Network error: ' + e.message, 'error'); });
  };

  /* <button data-post="/url" data-confirm="Sure?" data-fields="a=1&b=2" data-reload> */
  document.addEventListener('click', function (e) {
    var b = e.target.closest('[data-post]');
    if (!b) return;
    e.preventDefault();
    if (b.dataset.confirm && !window.confirm(b.dataset.confirm)) return;
    var data = new URLSearchParams();                      /* repeated keys (field=a&field=b) are kept */
    (b.dataset.fields || '').split('&').filter(Boolean).forEach(function (kv) { var p = kv.split('='); data.append(decodeURIComponent(p[0]), decodeURIComponent(p[1] || '')); });
    if (b.dataset.prompt) { var v = window.prompt(b.dataset.prompt); if (v === null) return; data.append(b.dataset.promptField || 'note', v); }
    b.disabled = true;
    ebPost(b.dataset.post, data, function () { if (b.hasAttribute('data-reload')) setTimeout(function () { location.reload(); }, 500); }).then(function (j) { b.disabled = false; });
  });

  /* column chooser: remembers which columns of a table are shown */
  window.ebColumns = function (tableId, menuId, key) {
    var t = document.getElementById(tableId), m = document.getElementById(menuId);
    if (!t || !m) return;
    var saved = {};
    try { saved = JSON.parse(localStorage.getItem(key) || '{}'); } catch (e) { }
    var heads = t.querySelectorAll('thead th');
    var box = m.querySelector('div');
    heads.forEach(function (th, i) {
      if (th.dataset.fixed) return;
      var lab = document.createElement('label');
      var cb = document.createElement('input'); cb.type = 'checkbox'; cb.checked = saved[i] !== false;
      lab.appendChild(cb); lab.appendChild(document.createTextNode(th.textContent.trim()));
      box.appendChild(lab);
      var apply = function () {
        t.querySelectorAll('tr').forEach(function (tr) { var c = tr.children[i]; if (c) c.style.display = cb.checked ? '' : 'none'; });
        saved[i] = cb.checked; try { localStorage.setItem(key, JSON.stringify(saved)); } catch (e) { }
      };
      cb.addEventListener('change', apply); apply();
    });
    m.querySelector('button').addEventListener('click', function (ev) { ev.stopPropagation(); m.classList.toggle('open'); });
    document.addEventListener('click', function (ev) { if (!m.contains(ev.target)) m.classList.remove('open'); });
  };
})();
