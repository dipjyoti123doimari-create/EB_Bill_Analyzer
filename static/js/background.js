/* background.js — Stripe gradient blob animation */
(function () {
  var PALETTES = {
    "Pre-dawn": { base: "#050A15", colors: ["#3B3FA0","#1A0F6B","#2D1B8E","#0D1F5C","#4B3480"], as: 0.80 },
    "Sunrise":  { base: "#1A0A2E", colors: ["#FF6B9D","#FF8C42","#9B4DFF","#4D79FF","#FF4DA6"], as: 0.42 },
    "Daytime":  { base: "#0A2540", colors: ["#635BFF","#00D4FF","#7A5AF8","#0EA5E9","#8B5CF6"], as: 0.45 },
    "Dusk":     { base: "#0F0820", colors: ["#8A4FFF","#FF61AB","#5B21B6","#7C3AED","#DB2777"], as: 0.55 },
    "Sunset":   { base: "#1A0510", colors: ["#FF2D6B","#9B2CFF","#FF7A3D","#FF4DA6","#C041FF"], as: 0.48 },
    "Night":    { base: "#05060F", colors: ["#3A7BFF","#7C5CFF","#00D4FF","#5856D6","#2563EB"], as: 0.72 }
  };

  var GEOM = [
    { bx:0.25, by:0.35, ox:0.28, oy:0.22, r:0.28, spd:0.35, ph:0.00, al:0.65 },
    { bx:0.72, by:0.28, ox:0.22, oy:0.28, r:0.25, spd:0.28, ph:2.09, al:0.60 },
    { bx:0.50, by:0.70, ox:0.19, oy:0.16, r:0.29, spd:0.31, ph:4.19, al:0.60 },
    { bx:0.18, by:0.65, ox:0.14, oy:0.22, r:0.24, spd:0.40, ph:1.05, al:0.55 },
    { bx:0.82, by:0.72, ox:0.11, oy:0.16, r:0.22, spd:0.24, ph:3.49, al:0.50 }
  ];

  var FRAME_MS = 1000 / 30;
  var state = { W:0, H:0, dpr:1, mx:0.5, my:0.5, tx:0.5, ty:0.5, running:false, raf:null, lt:0, theme:'Daytime', auto:true, animate:true };
  var canvas, ctx;

  function timeTheme() {
    var m = new Date().getHours() * 60 + new Date().getMinutes();
    if (m >= 240 && m < 360)  return "Pre-dawn";
    if (m >= 360 && m < 540)  return "Sunrise";
    if (m >= 540 && m < 1020) return "Daytime";
    if (m >= 1020 && m < 1110) return "Dusk";
    if (m >= 1110 && m <= 1200) return "Sunset";
    return "Night";
  }

  function hexA(hex, a) {
    var h = hex.replace('#', '');
    return 'rgba(' + parseInt(h.slice(0,2),16) + ',' + parseInt(h.slice(2,4),16) + ',' + parseInt(h.slice(4,6),16) + ',' + a + ')';
  }

  function paint(t) {
    var W = state.W, H = state.H, diag = Math.sqrt(W*W + H*H);
    var name = state.auto ? timeTheme() : state.theme;
    var pal = PALETTES[name] || PALETTES.Daytime;
    ctx.globalCompositeOperation = 'source-over';
    ctx.fillStyle = pal.base;
    ctx.fillRect(0, 0, W, H);
    ctx.globalCompositeOperation = 'screen';
    for (var i = 0; i < GEOM.length; i++) {
      var g = GEOM[i], color = pal.colors[i % pal.colors.length];
      var cx = (g.bx + Math.sin(t * g.spd + g.ph) * g.ox) * W;
      var cy = (g.by + Math.cos(t * g.spd * 0.8 + g.ph) * g.oy) * H;
      cx += (state.mx - 0.5) * W * 0.05 * (i % 2 === 0 ? 1 : -1) * (i + 1) / GEOM.length;
      cy += (state.my - 0.5) * H * 0.03 * (i % 3 === 0 ? -1 : 1) * (i + 1) / GEOM.length;
      var r = g.r * diag * 0.5, a0 = g.al * pal.as;
      var grd = ctx.createRadialGradient(cx, cy, 0, cx, cy, r);
      grd.addColorStop(0,   hexA(color, a0));
      grd.addColorStop(0.5, hexA(color, a0 * 0.4));
      grd.addColorStop(1,   hexA(color, 0));
      ctx.fillStyle = grd;
      ctx.fillRect(0, 0, W, H);
    }
    ctx.globalCompositeOperation = 'source-over';
  }

  function resize() {
    state.dpr = Math.min(window.devicePixelRatio || 1, 2);
    state.W = window.innerWidth; state.H = window.innerHeight;
    canvas.width = state.W * state.dpr; canvas.height = state.H * state.dpr;
    ctx.setTransform(state.dpr, 0, 0, state.dpr, 0, 0);
  }

  function frame(ts) {
    if (!state.running) return;
    state.raf = requestAnimationFrame(frame);
    if (ts - state.lt < FRAME_MS) return;
    state.lt = ts;
    state.mx += (state.tx - state.mx) * 0.08;
    state.my += (state.ty - state.my) * 0.08;
    paint(ts * 0.001);
  }

  function applyRunning() {
    if (state.animate) {
      if (!state.running) { state.running = true; state.raf = requestAnimationFrame(frame); }
    } else {
      state.running = false;
      paint(0);
    }
  }

  window.initBackground = function (opts) {
    canvas = document.getElementById('bg-canvas');
    if (!canvas) return;
    ctx = canvas.getContext('2d');
    state.animate = opts.animate !== false;
    state.auto    = opts.autoTheme !== false;
    state.theme   = opts.theme || 'Daytime';
    resize();
    window.addEventListener('resize', function () { resize(); if (!state.running) paint(0); });
    document.addEventListener('mousemove', function (e) { state.tx = e.clientX / state.W; state.ty = e.clientY / state.H; });
    document.addEventListener('mouseleave', function () { state.tx = 0.5; state.ty = 0.5; });
    applyRunning();
  };

  window.setBgTheme = function (theme, auto) { state.theme = theme; state.auto = !!auto; if (!state.running) paint(0); };
  window.setBgAnimate = function (on) { state.animate = on; applyRunning(); };

})();
