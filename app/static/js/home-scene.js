/* ParkPilot homepage hero: a live-looking 3D parking lot (Three.js r149, served from /static/vendor).
   Bay colours match the booking map: green available, red reserved, amber held/soon. */
(function () {
  'use strict';
  // Phones: show the fixed reserve bar only while the hero button is out of view.
  var bar = document.querySelector('.home-sticky-cta'), heroBtn = document.getElementById('reserve-hero');
  if (bar && heroBtn && 'IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { bar.classList.toggle('is-hidden', es[0].intersectionRatio >= 0.99); }, { threshold: [0, 0.5, 0.99, 1] }).observe(heroBtn);
  }
  var canvas = document.getElementById('lot3d');
  if (!canvas || !window.THREE) return;
  var T = THREE, root = document.documentElement, cs = getComputedStyle(root);
  var reduce = matchMedia('(prefers-reduced-motion: reduce)').matches;
  var rtl = root.dir === 'rtl';
  function col(name, fallback) {
    var v = cs.getPropertyValue(name).trim();
    try { return new T.Color(v || fallback); } catch (e) { return new T.Color(fallback); }
  }
  var brand = col('--brand', '#0F4C5C').lerp(new T.Color('#ffffff'), 0.35);
  var accent = col('--accent', '#E09F1F');

  var renderer;
  try { renderer = new T.WebGLRenderer({ canvas: canvas, antialias: true, alpha: true }); }
  catch (e) { canvas.hidden = true; return; }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));

  var scene = new T.Scene();
  scene.fog = new T.Fog(0x08141b, 50, 135);
  var cam = new T.PerspectiveCamera(30, 1, 0.5, 300);
  scene.add(new T.HemisphereLight(0xa9cbd8, 0x0a141a, 0.95));
  var sun = new T.DirectionalLight(0xffffff, 0.85);
  sun.position.set(-18, 30, 14);
  scene.add(sun);

  function M(c, o) { return new T.MeshStandardMaterial(Object.assign({ color: c, roughness: 0.8, metalness: 0.05 }, o || {})); }
  function box(w, h, d, mat, x, y, z, parent) {
    var m = new T.Mesh(new T.BoxGeometry(w, h, d), mat);
    m.position.set(x, y, z);
    (parent || scene).add(m);
    return m;
  }

  var N = 14, W = 2.8, D = 5.4, AISLE = 8, HALF = N * W / 2;
  box(160, 0.2, 100, M(0x0b1a22), 0, -0.2, 0);
  box(N * W + 8, 0.2, 2 * D + AISLE + 6, M(0x13303c), 0, -0.05, 0);
  var lineMat = M(0xdfe9ee, { roughness: 1 });
  [-1, 1].forEach(function (r) {
    for (var i = 0; i <= N; i++) box(0.12, 0.03, D, lineMat, (i - N / 2) * W, 0.06, r * (AISLE / 2 + D / 2));
  });
  for (var x = -HALF; x < HALF; x += 3) box(1.4, 0.03, 0.12, lineMat, x + 0.7, 0.06, 0);

  // Entrance gate in the tenant's brand colour.
  var gateMat = M(brand, { emissive: brand, emissiveIntensity: 0.7 });
  box(0.5, 5, 0.5, gateMat, -HALF - 3.5, 2.5, -4.6);
  box(0.5, 5, 0.5, gateMat, -HALF - 3.5, 2.5, 4.6);
  box(0.5, 0.6, 9.7, gateMat, -HALF - 3.5, 5, 0);

  var mats = {
    avail: M(0x2fa866, { emissive: 0x1d7a49, emissiveIntensity: 0.3 }),
    res: M(0xd24a3f, { emissive: 0x7d241d, emissiveIntensity: 0.25 }),
    soon: M(0xc77a1e, { emissive: 0x7a4a10, emissiveIntensity: 0.4 }),
    held: M(accent, { emissive: accent, emissiveIntensity: 0.8 })
  };
  var palette = [0xc9d6dc, 0x2c3e48, 0x7f93a0, 0xe8e8e8, 0x3a6f8a, 0x8b2f2a];
  function pick(a) { return a[Math.random() * a.length | 0]; }

  function makeCar(hex) {
    var g = new T.Group();
    box(1.9, 0.7, 4.1, M(hex, { roughness: 0.45, metalness: 0.3 }), 0, 0.55, 0, g);
    box(1.6, 0.55, 2.1, M(0x10202a, { roughness: 0.2, metalness: 0.6 }), 0, 1.12, -0.2, g);
    var head = new T.MeshBasicMaterial({ color: 0xfff2c4 }), tail = new T.MeshBasicMaterial({ color: 0xff4a3a });
    [-0.6, 0.6].forEach(function (s) {
      box(0.36, 0.18, 0.08, head, s, 0.6, 2.06, g);
      box(0.36, 0.14, 0.08, tail, s, 0.62, -2.06, g);
    });
    scene.add(g);
    return g;
  }

  var bays = [];
  [-1, 1].forEach(function (r) {
    for (var i = 0; i < N; i++) {
      var m = new T.Mesh(new T.PlaneGeometry(W - 0.35, D - 0.5), mats.avail);
      m.rotation.x = -Math.PI / 2;
      var b = { row: r, x: (i - (N - 1) / 2) * W, z: r * (AISLE / 2 + D / 2), mesh: m, state: 'avail', car: null };
      m.position.set(b.x, 0.08, b.z); // above the asphalt top (y=0.05); at 0.04 the coloured bays were buried and invisible
      scene.add(m);
      var roll = Math.random();
      if (roll < 0.34) { b.state = 'res'; b.car = makeCar(pick(palette)); b.car.position.set(b.x, 0, b.z); b.car.rotation.y = r < 0 ? Math.PI : 0; }
      else if (roll < 0.44) b.state = 'soon';
      m.material = mats[b.state];
      bays.push(b);
    }
  });
  function setState(b, s) { b.state = s; b.mesh.material = mats[s]; }

  // A plane crossing the sky: airport context for the lot.
  var plane = new T.Group(), pm = M(0xcfdde4, { roughness: 0.5 });
  var fus = new T.Mesh(new T.CylinderGeometry(0.9, 0.9, 14, 12), pm); fus.rotation.z = Math.PI / 2; plane.add(fus);
  var nose = new T.Mesh(new T.ConeGeometry(0.9, 2.6, 12), pm); nose.rotation.z = -Math.PI / 2; nose.position.x = 8.3; plane.add(nose);
  box(3.6, 0.18, 16, pm, 0.5, 0, 0, plane);
  box(1.8, 2.6, 0.18, pm, -6.4, 1.5, 0, plane);
  box(1.4, 0.15, 5, pm, -6.4, 0.3, 0, plane);
  var navR = new T.Mesh(new T.SphereGeometry(0.3, 8, 8), new T.MeshBasicMaterial({ color: 0xff3b30 })); navR.position.set(0.5, 0, 8);
  var navG = new T.Mesh(new T.SphereGeometry(0.3, 8, 8), new T.MeshBasicMaterial({ color: 0x34d17a })); navG.position.set(0.5, 0, -8);
  plane.add(navR, navG);
  plane.position.set(-95, 27, -34);
  scene.add(plane);

  var pointer = { x: 0, y: 0 }, smooth = { x: 0, y: 0 };
  window.addEventListener('pointermove', function (e) {
    pointer.x = e.clientX / innerWidth - 0.5; pointer.y = e.clientY / innerHeight - 0.5;
  }, { passive: true });

  var dist = 60;
  function resize() {
    var w = canvas.clientWidth || 1, h = canvas.clientHeight || 1, wide = w >= 900;
    renderer.setSize(w, h, false);
    cam.aspect = w / h;
    cam.fov = wide ? 30 : 40;
    dist = wide ? 60 : 62 * Math.min(1.5, Math.max(1, 0.95 / cam.aspect));
    // Narrow screens look from further away, so push the fog back or the whole lot disappears into it.
    scene.fog.near = wide ? 50 : 120; scene.fog.far = wide ? 135 : 260;
    if (wide && !canvas.hasAttribute('data-center')) cam.setViewOffset(w, h, (rtl ? 1 : -1) * w * 0.2, 0, w, h); else cam.clearViewOffset();
    cam.updateProjectionMatrix();
    if (reduce) draw(0);
  }

  function ease(p) { return p < 0.5 ? 2 * p * p : 1 - Math.pow(-2 * p + 2, 2) / 2; }
  var drivers = [], leaving = [], nextSpawn = 1.2;
  function spawn() {
    var free = bays.filter(function (b) { return b.state === 'avail'; });
    if (!free.length || drivers.length > 1) return;
    var b = pick(free); setState(b, 'held');
    var car = makeCar(pick(palette));
    car.position.set(-HALF - 14, 0, 0); car.rotation.y = Math.PI / 2;
    drivers.push({ car: car, bay: b, phase: 0, p: 0 });
    var taken = bays.filter(function (x) { return x.state === 'res' && x.car; });
    if (taken.length > 6) { var r = pick(taken); leaving.push(r.car); r.car = null; setState(r, 'avail'); }
  }
  function step(dt) {
    nextSpawn -= dt;
    if (nextSpawn <= 0) { spawn(); nextSpawn = 3 + Math.random() * 1.5; }
    drivers = drivers.filter(function (d) {
      var c = d.car;
      if (d.phase === 0) {
        c.position.x += 9 * dt;
        if (c.position.x >= d.bay.x) { c.position.x = d.bay.x; d.phase = 1; }
        return true;
      }
      d.p = Math.min(1, d.p + dt / 1.5);
      var e = ease(d.p), target = d.bay.row < 0 ? Math.PI : 0;
      c.position.z = d.bay.z * e;
      c.rotation.y = Math.PI / 2 + (target - Math.PI / 2) * e;
      if (d.p < 1) return true;
      setState(d.bay, 'res'); d.bay.car = c;
      return false;
    });
    leaving = leaving.filter(function (c) {
      c.scale.multiplyScalar(Math.max(0, 1 - dt * 3));
      if (c.scale.x > 0.05) return true;
      scene.remove(c); return false;
    });
    plane.position.x += 4 * dt;
    if (plane.position.x > 95) plane.position.x = -95;
  }

  function draw(t) {
    smooth.x += (pointer.x - smooth.x) * 0.05; smooth.y += (pointer.y - smooth.y) * 0.05;
    var az = 0.62 + smooth.x * 0.3 + Math.sin(t * 0.08) * 0.08, el = 0.62 - smooth.y * 0.12;
    cam.position.set(dist * Math.cos(el) * Math.sin(az), dist * Math.sin(el), dist * Math.cos(el) * Math.cos(az));
    cam.lookAt(0, 0, 0);
    mats.held.emissiveIntensity = 0.65 + 0.35 * Math.sin(t * 6);
    var blink = Math.sin(t * 5) > 0;
    navR.visible = blink; navG.visible = !blink;
    renderer.render(scene, cam);
  }

  var clock = new T.Clock(), running = false, raf = 0, visible = true;
  function frame() {
    if (!running) return;
    raf = requestAnimationFrame(frame);
    var dt = Math.min(clock.getDelta(), 0.05);
    step(dt); draw(clock.elapsedTime);
  }
  function sync() {
    var should = visible && !document.hidden && !reduce;
    if (should && !running) { running = true; clock.getDelta(); frame(); }
    else if (!should && running) { running = false; cancelAnimationFrame(raf); }
  }
  window.addEventListener('resize', resize);
  document.addEventListener('visibilitychange', sync);
  if ('IntersectionObserver' in window) {
    new IntersectionObserver(function (es) { visible = es[0].isIntersecting; sync(); }).observe(canvas);
  }
  resize();
  if (reduce) { drivers = []; draw(0); } else sync();
  canvas.classList.add('is-ready');
})();
