/* Small dependency-free SVG charts (no external library, no network). */
(function () {
  var NS = 'http://www.w3.org/2000/svg';
  var COLORS = ['#7A73FF', '#00D4FF', '#F59E0B', '#34D399', '#F87171'];
  function el(name, attrs, text) {
    var e = document.createElementNS(NS, name);
    for (var k in (attrs || {})) e.setAttribute(k, attrs[k]);
    if (text != null) e.textContent = text;
    return e;
  }
  function fmt(v, d) { return v == null ? '' : Number(v).toLocaleString('en-IN', { maximumFractionDigits: d == null ? 0 : d }); }
  function nice(max) {
    if (max <= 0) return 1;
    var p = Math.pow(10, Math.floor(Math.log10(max))), n = max / p;
    return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 5 ? 5 : 10) * p;
  }
  function frame(host, labels) {
    host.innerHTML = '';
    var w = host.clientWidth || 520, h = host.clientHeight || 230;
    var svg = el('svg', { viewBox: '0 0 ' + w + ' ' + h, class: 'eb-chart', preserveAspectRatio: 'none' });
    host.appendChild(svg);
    return { svg: svg, w: w, h: h, l: 46, r: 12, t: 12, b: 28 };
  }
  function empty(host, msg) { host.innerHTML = '<div class="eb-muted" style="padding:60px 0;text-align:center;font-size:13px">' + (msg || 'No data for this selection') + '</div>'; }

  /* series: [{name, values:[...], color?, dashed?}], labels: x labels; opts: {min,max,decimals,lines:[{value,label}]} */
  window.ebLine = function (host, labels, series, opts) {
    opts = opts || {};
    var any = series.some(function (s) { return s.values.some(function (v) { return v != null; }); });
    if (!labels.length || !any) return empty(host);
    var f = frame(host), pw = f.w - f.l - f.r, ph = f.h - f.t - f.b;
    var vals = [].concat.apply([], series.map(function (s) { return s.values.filter(function (v) { return v != null; }); }));
    (opts.lines || []).forEach(function (l) { vals.push(l.value); });
    var lo = opts.min != null ? opts.min : Math.min.apply(null, vals), hi = opts.max != null ? opts.max : Math.max.apply(null, vals);
    if (hi === lo) { hi += 1; lo -= 1; }
    var pad = (hi - lo) * 0.08; if (opts.min == null) lo -= pad; if (opts.max == null) hi += pad;
    var x = function (i) { return f.l + (labels.length === 1 ? pw / 2 : i * pw / (labels.length - 1)); };
    var y = function (v) { return f.t + ph - (v - lo) / (hi - lo) * ph; };
    for (var g = 0; g <= 4; g++) {
      var gv = lo + (hi - lo) * g / 4, gy = y(gv);
      f.svg.appendChild(el('line', { x1: f.l, x2: f.w - f.r, y1: gy, y2: gy, class: 'grid' }));
      f.svg.appendChild(el('text', { x: f.l - 6, y: gy + 3, 'text-anchor': 'end' }, fmt(gv, opts.decimals == null ? 2 : opts.decimals)));
    }
    labels.forEach(function (lb, i) {
      if (labels.length > 12 && i % Math.ceil(labels.length / 12)) return;
      f.svg.appendChild(el('text', { x: x(i), y: f.h - 8, 'text-anchor': 'middle' }, lb));
    });
    (opts.lines || []).forEach(function (l) {
      f.svg.appendChild(el('line', { x1: f.l, x2: f.w - f.r, y1: y(l.value), y2: y(l.value), stroke: l.color || '#F59E0B', 'stroke-dasharray': '5 4', 'stroke-width': 1 }));
      f.svg.appendChild(el('text', { x: f.w - f.r - 2, y: y(l.value) - 3, 'text-anchor': 'end', style: 'fill:' + (l.color || '#F59E0B') }, l.label));
    });
    series.forEach(function (s, si) {
      var col = s.color || COLORS[si % COLORS.length], d = '', started = false;
      s.values.forEach(function (v, i) {
        if (v == null) { started = false; return; }
        d += (started ? 'L' : 'M') + x(i) + ' ' + y(v); started = true;
      });
      f.svg.appendChild(el('path', { d: d, fill: 'none', stroke: col, 'stroke-width': 2.2, 'stroke-dasharray': s.dashed ? '5 4' : '' }));
      s.values.forEach(function (v, i) {
        if (v == null) return;
        var c = el('circle', { cx: x(i), cy: y(v), r: 3.2, fill: col });
        c.appendChild(el('title', {}, (s.name ? s.name + ' · ' : '') + labels[i] + ': ' + fmt(v, opts.decimals == null ? 2 : opts.decimals)));
        f.svg.appendChild(c);
      });
    });
    legend(host, series);
  };

  window.ebBars = function (host, labels, series, opts) {
    opts = opts || {};
    var any = series.some(function (s) { return s.values.some(function (v) { return v != null; }); });
    if (!labels.length || !any) return empty(host);
    var f = frame(host), pw = f.w - f.l - f.r, ph = f.h - f.t - f.b;
    var max = nice(Math.max.apply(null, [].concat.apply([], series.map(function (s) { return s.values.map(function (v) { return v || 0; }); }))));
    var y = function (v) { return f.t + ph - v / max * ph; };
    for (var g = 0; g <= 4; g++) {
      var gv = max * g / 4;
      f.svg.appendChild(el('line', { x1: f.l, x2: f.w - f.r, y1: y(gv), y2: y(gv), class: 'grid' }));
      f.svg.appendChild(el('text', { x: f.l - 6, y: y(gv) + 3, 'text-anchor': 'end' }, fmt(gv, opts.decimals)));
    }
    var band = pw / labels.length, bw = Math.max(3, band * 0.7 / series.length);
    labels.forEach(function (lb, i) {
      var x0 = f.l + i * band + band * 0.15;
      if (labels.length <= 14 || i % Math.ceil(labels.length / 14) === 0) f.svg.appendChild(el('text', { x: f.l + i * band + band / 2, y: f.h - 8, 'text-anchor': 'middle' }, lb.length > 10 ? lb.slice(0, 9) + '…' : lb));
      series.forEach(function (s, si) {
        var v = s.values[i]; if (v == null) return;
        var r = el('rect', { x: x0 + si * bw, y: y(v), width: bw - 1, height: Math.max(0, f.t + ph - y(v)), fill: s.color || COLORS[si % COLORS.length], rx: 2 });
        r.appendChild(el('title', {}, lb + (s.name ? ' · ' + s.name : '') + ': ' + fmt(v, opts.decimals)));
        f.svg.appendChild(r);
      });
    });
    legend(host, series);
  };

  function legend(host, series) {
    if (series.length < 2) return;
    var d = document.createElement('div'); d.className = 'eb-legend';
    series.forEach(function (s, i) { d.innerHTML += '<span><i style="background:' + (s.color || COLORS[i % COLORS.length]) + '"></i>' + (s.name || '') + '</span>'; });
    host.appendChild(d);
  }
})();

