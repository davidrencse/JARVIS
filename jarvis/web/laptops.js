// Connections → Computers: a small live WebGL laptop in each computer card.
// Self-contained: app.js still draws the cards; this watches #devices-top, keeps one
// canvas (and one GL context) per computer across the 8 s re-renders, and hides the flat
// laptop icon only while a canvas is in place. No WebGL → the icon stays as it was.
// Colour means state: the screen and power LED use --ok / --warn; the shell is neutral.
(() => {
  "use strict";
  const top = document.getElementById("devices-top");
  if (!top) return;

  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const inst = new Map();           // "hub:name" | "node:name" -> instance
  let noGL = false, raf = 0, last = 0;
  const DEG = Math.PI / 180;

  // ---------- colours from the live theme tokens ----------
  const cctx = document.createElement("canvas").getContext("2d", { willReadFrequently: true });
  function rgb(v, fb) {
    if (!v) return fb;
    cctx.fillStyle = "#010203"; cctx.fillStyle = v;
    if (cctx.fillStyle === "#010203" && v.toLowerCase() !== "#010203") return fb;
    cctx.clearRect(0, 0, 1, 1); cctx.fillRect(0, 0, 1, 1);
    const d = cctx.getImageData(0, 0, 1, 1).data;
    return [d[0] / 255, d[1] / 255, d[2] / 255];
  }
  const mix = (a, b, k) => a.map((x, i) => x + (b[i] - x) * k);
  const css = (c, a = 1) => `rgba(${Math.round(c[0] * 255)},${Math.round(c[1] * 255)},${Math.round(c[2] * 255)},${a})`;
  function readTheme() {
    const cs = getComputedStyle(document.documentElement);
    const g = (n, fb) => rgb(cs.getPropertyValue(n).trim(), fb);
    const bg = g("--bg", [0.02, 0.03, 0.03]);
    const light = (bg[0] + bg[1] + bg[2]) / 3 > 0.45;
    const ok = g("--ok", [0.56, 0.82, 0.62]), warn = g("--warn", [0.94, 0.77, 0.42]);
    return {
      light, ok, warn,
      // on the dark glass a light theme's deep green would vanish: lift it toward white
      okScr: light ? mix(ok, [1, 1, 1], 0.45) : ok,
      warnScr: light ? mix(warn, [1, 1, 1], 0.4) : warn,
      shell: light ? [0.66, 0.675, 0.70] : [0.44, 0.46, 0.49],
      bezel: [0.045, 0.05, 0.055],
      shadow: light ? 0.32 : 0.55,
    };
  }
  let theme = readTheme();

  // ---------- tiny mat4 (column-major) ----------
  const M = {
    id() { const m = new Float32Array(16); m[0] = m[5] = m[10] = m[15] = 1; return m; },
    mul(a, b) {
      const o = new Float32Array(16);
      for (let c = 0; c < 4; c++) for (let r = 0; r < 4; r++) {
        let s = 0; for (let k = 0; k < 4; k++) s += a[k * 4 + r] * b[c * 4 + k]; o[c * 4 + r] = s;
      }
      return o;
    },
    tr(x, y, z) { const m = M.id(); m[12] = x; m[13] = y; m[14] = z; return m; },
    rx(t) { const m = M.id(), c = Math.cos(t), s = Math.sin(t); m[5] = c; m[6] = s; m[9] = -s; m[10] = c; return m; },
    ry(t) { const m = M.id(), c = Math.cos(t), s = Math.sin(t); m[0] = c; m[2] = -s; m[8] = s; m[10] = c; return m; },
    persp(fov, a, n, f) {
      const t = 1 / Math.tan(fov / 2), m = new Float32Array(16);
      m[0] = t / a; m[5] = t; m[10] = (f + n) / (n - f); m[11] = -1; m[14] = 2 * f * n / (n - f); return m;
    },
    look(e, t) {
      const nz = norm([e[0] - t[0], e[1] - t[1], e[2] - t[2]]);
      const nx = norm(cross([0, 1, 0], nz)), ny = cross(nz, nx);
      const m = M.id();
      m[0] = nx[0]; m[4] = nx[1]; m[8] = nx[2];
      m[1] = ny[0]; m[5] = ny[1]; m[9] = ny[2];
      m[2] = nz[0]; m[6] = nz[1]; m[10] = nz[2];
      m[12] = -dot(nx, e); m[13] = -dot(ny, e); m[14] = -dot(nz, e);
      return m;
    },
  };
  function norm(v) { const l = Math.hypot(v[0], v[1], v[2]) || 1; return [v[0] / l, v[1] / l, v[2] / l]; }
  function cross(a, b) { return [a[1] * b[2] - a[2] * b[1], a[2] * b[0] - a[0] * b[2], a[0] * b[1] - a[1] * b[0]]; }
  function dot(a, b) { return a[0] * b[0] + a[1] * b[1] + a[2] * b[2]; }

  // ---------- geometry: interleaved pos3 normal3 uv2 ----------
  function box(w, h, d, cx = 0, cy = 0, cz = 0) {
    const x = w / 2, y = h / 2, z = d / 2, out = [];
    const faces = [
      [[1, 0, 0], [[x, -y, z], [x, -y, -z], [x, y, -z], [x, y, z]]],
      [[-1, 0, 0], [[-x, -y, -z], [-x, -y, z], [-x, y, z], [-x, y, -z]]],
      [[0, 1, 0], [[-x, y, z], [x, y, z], [x, y, -z], [-x, y, -z]]],
      [[0, -1, 0], [[-x, -y, -z], [x, -y, -z], [x, -y, z], [-x, -y, z]]],
      [[0, 0, 1], [[-x, -y, z], [x, -y, z], [x, y, z], [-x, y, z]]],
      [[0, 0, -1], [[x, -y, -z], [-x, -y, -z], [-x, y, -z], [x, y, -z]]],
    ];
    const uv = [[0, 0], [1, 0], [1, 1], [0, 1]];
    for (const [n, c] of faces) for (const i of [0, 1, 2, 0, 2, 3])
      out.push(c[i][0] + cx, c[i][1] + cy, c[i][2] + cz, ...n, ...uv[i]);
    return new Float32Array(out);
  }
  // Horizontal quad at height y; uv(u along +x, v from z1 (0) to z0 (1)).
  function hquad(x0, x1, z0, z1, y, ny) {
    const p = [[x0, z1, 0, 0], [x1, z1, 1, 0], [x1, z0, 1, 1], [x0, z0, 0, 1]], out = [];
    for (const i of [0, 1, 2, 0, 2, 3]) out.push(p[i][0], y, p[i][1], 0, ny, 0, p[i][2], p[i][3]);
    return new Float32Array(out);
  }

  // Two silhouettes: a thin ultrabook and a thicker gaming chassis (read from the card's label).
  function shape(gaming) {
    const W = 2.2, D = 1.5, H = gaming ? 0.15 : 0.085, T = gaming ? 0.075 : 0.05, DL = 1.42;
    return {
      W, D, H, T, DL,
      base: box(W, H, D, 0, H / 2, 0),
      vent: gaming ? box(W * 0.56, H * 0.22, 0.02, 0, H * 0.5, D / 2 + 0.006) : null,
      keys: hquad(-0.84, 0.84, -D / 2 + 0.14, 0.02, H + 0.002, 1),
      pad: hquad(-0.36, 0.36, 0.16, 0.62, H + 0.002, 1),
      led: box(0.09, 0.022, 0.012, 0.88, H * 0.5, D / 2 + 0.006),
      shadow: hquad(-1.75, 1.75, -1.25, 1.25, 0.0005, 1),
      lid: box(W, T, DL, 0, T / 2, DL / 2),
      bezel: hquad(-W / 2 + 0.03, W / 2 - 0.03, 0.03, DL - 0.03, -0.0015, -1),
      screen: hquad(-W / 2 + 0.09, W / 2 - 0.09, 0.13, DL - 0.07, -0.003, -1),
    };
  }

  const VS = `attribute vec3 aP; attribute vec3 aN; attribute vec2 aU;
uniform mat4 uM, uVP; varying vec3 vN, vW; varying vec2 vU;
void main(){ vec4 w = uM*vec4(aP,1.0); vW = w.xyz; vN = (uM*vec4(aN,0.0)).xyz; vU = aU; gl_Position = uVP*w; }`;
  const FS = `precision mediump float;
uniform int uMode; uniform vec3 uCol, uGlow, uEye; uniform float uGlowAmt, uAlpha; uniform sampler2D uTex;
varying vec3 vN, vW; varying vec2 vU;
void main(){
  if (uMode == 3) { vec2 q = (vU - 0.5) * 2.0; float a = smoothstep(1.0, 0.0, length(q)); gl_FragColor = vec4(0.0, 0.0, 0.0, a * a * uAlpha); return; }
  if (uMode == 4) { gl_FragColor = vec4(uCol, 1.0); return; }
  vec3 n = normalize(vN); vec3 v = normalize(uEye - vW);
  if (uMode == 1) {
    vec3 c = texture2D(uTex, vU).rgb;
    float glare = pow(max(dot(normalize(reflect(-v, n)), normalize(vec3(-0.5, 0.8, 0.4))), 0.0), 18.0) * 0.18;
    gl_FragColor = vec4(c + glare, 1.0); return;
  }
  vec3 l = normalize(vec3(0.45, 1.0, 0.6)); vec3 h = normalize(l + v);
  float dif = max(dot(n, l), 0.0);
  float spec = pow(max(dot(n, h), 0.0), 48.0) * 0.32;
  float rim = pow(1.0 - max(dot(n, v), 0.0), 3.0);
  vec3 base = uCol;
  if (uMode == 2) {
    vec2 f = fract(vU * vec2(15.0, 5.0));
    float key = step(0.13, f.x) * step(f.x, 0.87) * step(0.16, f.y) * step(f.y, 0.84);
    base = mix(uCol * 0.35, uCol * 0.62, key);
    base += uGlow * uGlowAmt * 0.22 * (1.0 - key);
  }
  vec3 c = base * (0.42 + 0.68 * dif) + spec + rim * 0.22 * mix(vec3(1.0), uGlow, 0.35 * uGlowAmt);
  gl_FragColor = vec4(c, 1.0);
}`;

  function makeGL(canvas) {
    const gl = canvas.getContext("webgl", { alpha: true, antialias: true, premultipliedAlpha: true, powerPreference: "low-power" });
    if (!gl) return null;
    const sh = (type, src) => {
      const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      return s;
    };
    const p = gl.createProgram();
    gl.attachShader(p, sh(gl.VERTEX_SHADER, VS)); gl.attachShader(p, sh(gl.FRAGMENT_SHADER, FS));
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    gl.useProgram(p);
    const u = {};
    for (const n of ["uM", "uVP", "uMode", "uCol", "uGlow", "uEye", "uGlowAmt", "uAlpha", "uTex"]) u[n] = gl.getUniformLocation(p, n);
    const a = { P: gl.getAttribLocation(p, "aP"), N: gl.getAttribLocation(p, "aN"), U: gl.getAttribLocation(p, "aU") };
    const tex = gl.createTexture();
    gl.bindTexture(gl.TEXTURE_2D, tex);
    for (const [k, v] of [[gl.TEXTURE_MIN_FILTER, gl.LINEAR], [gl.TEXTURE_MAG_FILTER, gl.LINEAR], [gl.TEXTURE_WRAP_S, gl.CLAMP_TO_EDGE], [gl.TEXTURE_WRAP_T, gl.CLAMP_TO_EDGE]])
      gl.texParameteri(gl.TEXTURE_2D, k, v);
    gl.uniform1i(u.uTex, 0);
    gl.enable(gl.DEPTH_TEST); gl.enable(gl.BLEND); gl.blendFunc(gl.ONE, gl.ONE_MINUS_SRC_ALPHA);
    return { gl, u, a, tex };
  }

  function upload(g, shapeObj) {
    const { gl } = g, meshes = {};
    for (const [k, data] of Object.entries(shapeObj)) {
      if (!(data instanceof Float32Array)) continue;
      const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, gl.STATIC_DRAW);
      meshes[k] = { b, n: data.length / 8 };
    }
    return meshes;
  }

  // ---------- one laptop ----------
  function create(key, hub, label, order) {
    if (noGL) return null;
    const canvas = document.createElement("canvas");
    canvas.className = "cx-3d";
    canvas.setAttribute("aria-hidden", "true");
    let g;
    try { g = makeGL(canvas); } catch (e) { console.warn("[laptops]", e); g = null; }
    if (!g) { noGL = true; return null; }
    const gaming = /\b(g1[56]|gaming|rog|tuf|legion|omen|alienware|nitro|predator|katana|raider)\b/i.test(label);
    const s = shape(gaming);
    const scr = document.createElement("canvas"); scr.width = 256; scr.height = 160;
    const it = {
      key, hub, canvas, g, s, meshes: upload(g, s), scr, sctx: scr.getContext("2d"),
      online: true, peers: [], lid: 2 * DEG, yaw: hub ? 0.42 : -0.42, hover: null,
      seed: [...key].reduce((h, ch) => (h * 31 + ch.charCodeAt(0)) % 997, 7) / 97,
      born: performance.now() / 1000 + 0.15 + order * 0.14, lastScr: -1, visible: true, dirty: true, w: 0, h: 0,
    };
    canvas.addEventListener("webglcontextlost", (e) => {
      e.preventDefault();
      canvas.parentNode?.classList.remove("has-3d");
      canvas.remove(); inst.delete(key);
    });
    io?.observe(canvas);
    return it;
  }

  function drawScreen(it, t) {
    const c = it.sctx, W = 256, H = 160, ok = theme.okScr, warn = theme.warnScr;
    c.globalAlpha = 1;
    c.fillStyle = css(mix([0.02, 0.024, 0.028], it.online ? ok : warn, 0.05)); c.fillRect(0, 0, W, H);
    if (!it.online) {
      const a = 0.35 + 0.25 * Math.sin(t * 1.6);
      c.fillStyle = css(warn, a); c.beginPath(); c.arc(W / 2, H / 2, 6, 0, 7); c.fill();
      return;
    }
    // status bar
    c.fillStyle = css(ok, 0.16); c.fillRect(0, 0, W, 14);
    c.fillStyle = css(ok, 0.85); c.fillRect(8, 5, 26, 4);
    for (let i = 0; i < 3; i++) { c.fillStyle = css(ok, 0.35); c.fillRect(W - 14 - i * 9, 5, 5, 4); }
    if (it.hub) {
      // hub: pulse rings from the centre, one spoke per node (ok online, warn offline)
      const cx = W / 2, cy = H / 2 + 7, R = 58;
      for (let k = 0; k < 3; k++) {
        const f = (t * 0.32 + k / 3) % 1;
        c.strokeStyle = css(ok, (1 - f) * 0.7); c.lineWidth = 2;
        c.beginPath(); c.arc(cx, cy, 8 + f * R, 0, 7); c.stroke();
      }
      const peers = it.peers.length ? it.peers : [];
      peers.forEach((on, i) => {
        const ang = -Math.PI / 2 + (i + 0.5) * (2 * Math.PI / peers.length) + t * 0.15;
        const x = cx + Math.cos(ang) * 44, y = cy + Math.sin(ang) * 40;
        c.strokeStyle = css(on ? ok : warn, 0.4); c.lineWidth = 1.5; c.setLineDash(on ? [] : [3, 3]);
        c.beginPath(); c.moveTo(cx, cy); c.lineTo(x, y); c.stroke(); c.setLineDash([]);
        const p = on ? (t * 0.9 + i * 0.37) % 1 : -1;           // a packet travelling the spoke
        if (p >= 0) { c.fillStyle = css(ok, 0.9); c.fillRect(cx + (x - cx) * p - 1.5, cy + (y - cy) * p - 1.5, 3, 3); }
        c.fillStyle = css(on ? ok : warn, 1); c.beginPath(); c.arc(x, y, 5, 0, 7); c.fill();
      });
      c.fillStyle = css(ok, 1); c.beginPath(); c.arc(cx, cy, 7, 0, 7); c.fill();
    } else {
      // node: a scrolling job log with a blinking cursor
      const lh = 13, speed = 16, s = t * speed + it.seed * 100, off = s % lh, first = Math.floor(s / lh);
      const rows = Math.ceil((H - 20) / lh) + 1;
      c.save(); c.beginPath(); c.rect(0, 16, W, H - 16); c.clip();
      for (let i = 0; i < rows; i++) {
        const k = first + i, r = Math.abs(Math.sin(k * 12.9898 + it.seed) * 43758.5453) % 1;
        const y = 22 + i * lh - off, indent = r > 0.7 ? 18 : 8;
        c.fillStyle = css(ok, 0.85); c.fillRect(indent, y, 6, 5);
        c.fillStyle = css(ok, r > 0.85 ? 0.75 : 0.42); c.fillRect(indent + 10, y, 30 + r * 170, 5);
      }
      c.restore();
      if (Math.floor(t * 2) % 2) { c.fillStyle = css(ok, 0.95); c.fillRect(8, H - 14, 8, 8); }
    }
    // glass sheen
    const gr = c.createLinearGradient(0, 0, W, H);
    gr.addColorStop(0, "rgba(255,255,255,0.07)"); gr.addColorStop(0.45, "rgba(255,255,255,0)");
    c.fillStyle = gr; c.fillRect(0, 0, W, H);
  }

  function render(it, t) {
    const { g, s, meshes, canvas } = it, { gl, u, a } = g;
    const dpr = Math.min(window.devicePixelRatio || 1, 2);
    // clientWidth forces layout; the CSS size is fixed until resize, theme, or zoom.
    let w = it.cssW, h = it.cssH;
    if (!it.sized || it.dirty || dpr !== it.dpr) {
      it.dpr = dpr;
      w = Math.round(canvas.clientWidth * dpr);
      h = Math.round(canvas.clientHeight * dpr);
      it.cssW = w; it.cssH = h;
    }
    if (!w || !h) { it.sized = false; return; }
    it.sized = true;
    if (w !== it.w || h !== it.h) { canvas.width = it.w = w; canvas.height = it.h = h; }
    gl.viewport(0, 0, w, h);
    gl.clearColor(0, 0, 0, 0); gl.clear(gl.COLOR_BUFFER_BIT | gl.DEPTH_BUFFER_BIT);

    const eye = [0, 2.0, 5.2];
    gl.uniformMatrix4fv(u.uVP, false, M.mul(M.persp(25 * DEG, w / h, 0.1, 30), M.look(eye, [0, 0.6, 0])));
    gl.uniform3fv(u.uEye, eye);
    const open = Math.max(0, Math.min(1, it.lid / (100 * DEG)));
    const glow = it.online ? open : 0;
    gl.uniform3fv(u.uGlow, theme.ok); gl.uniform1f(u.uGlowAmt, glow);

    const bob = reduce.matches ? 0 : Math.sin(t * 1.1 + it.seed) * 0.035;
    const spin = M.ry(it.yaw), root = M.mul(M.tr(0, bob + 0.02, 0), spin);
    const lidM = M.mul(root, M.mul(M.tr(0, s.H, -s.D / 2), M.rx(-it.lid)));

    const draw = (mesh, m, mode, col) => {
      if (!mesh) return;
      gl.bindBuffer(gl.ARRAY_BUFFER, mesh.b);
      gl.enableVertexAttribArray(a.P); gl.vertexAttribPointer(a.P, 3, gl.FLOAT, false, 32, 0);
      gl.enableVertexAttribArray(a.N); gl.vertexAttribPointer(a.N, 3, gl.FLOAT, false, 32, 12);
      gl.enableVertexAttribArray(a.U); gl.vertexAttribPointer(a.U, 2, gl.FLOAT, false, 32, 24);
      gl.uniformMatrix4fv(u.uM, false, m); gl.uniform1i(u.uMode, mode);
      if (col) gl.uniform3fv(u.uCol, col);
      gl.drawArrays(gl.TRIANGLES, 0, mesh.n);
    };

    gl.depthMask(false);
    gl.uniform1f(u.uAlpha, theme.shadow * (1 - bob * 4));
    draw(meshes.shadow, spin, 3);
    gl.depthMask(true);

    const shell = theme.shell;
    draw(meshes.base, root, 0, shell);
    draw(meshes.vent, root, 0, mix(shell, [0, 0, 0], 0.55));
    draw(meshes.keys, root, 2, shell);
    draw(meshes.pad, root, 0, mix(shell, [1, 1, 1], 0.08));
    const pulse = reduce.matches ? 1 : 0.65 + 0.35 * Math.sin(t * 2.4 + it.seed);
    draw(meshes.led, root, 4, it.online ? mix([0, 0, 0], theme.ok, pulse) : mix([0, 0, 0], theme.warn, 0.4 + 0.4 * pulse));
    draw(meshes.lid, lidM, 0, shell);
    draw(meshes.bezel, lidM, 0, theme.bezel);
    if (it.lastScr < 0 || reduce.matches || t - it.lastScr > 1 / 15) {
      drawScreen(it, reduce.matches ? 0 : t); it.lastScr = t;
      gl.bindTexture(gl.TEXTURE_2D, g.tex);
      gl.texImage2D(gl.TEXTURE_2D, 0, gl.RGBA, gl.RGBA, gl.UNSIGNED_BYTE, it.scr);
    }
    draw(meshes.screen, lidM, 1);
  }

  function step(it, dt, t) {
    const lidTarget = (it.online ? 106 : 14) * DEG;
    const baseYaw = it.hub ? 0.42 : -0.42;   // the two ends of the wire turn toward each other
    if (reduce.matches) {
      it.lid = lidTarget; it.yaw = it.hover == null ? baseYaw : baseYaw + it.hover * 0.5;
    } else {
      if (t > it.born) it.lid += (lidTarget - it.lid) * (1 - Math.exp(-dt * 3.4));
      const yawTarget = it.hover == null ? baseYaw + Math.sin(t * 0.42 + it.seed * 3) * 0.13 : baseYaw + it.hover * 0.6;
      it.yaw += (yawTarget - it.yaw) * (1 - Math.exp(-dt * (it.hover == null ? 1.6 : 5)));
    }
    render(it, t);
    if (it.sized) it.dirty = false;
  }

  function frame(now) {
    raf = 0;
    if (document.visibilityState !== "visible") return;
    const dt = Math.min(0.05, (now - last) / 1000); last = now;
    const t = now / 1000;
    let live = false;
    const buried = !!top.closest("[hidden]");
    for (const it of inst.values()) {
      if (buried || !it.canvas.isConnected || !it.visible) continue;
      if (reduce.matches && !it.dirty) continue;
      step(it, dt, t); live = true;
    }
    if (live && !reduce.matches) raf = requestAnimationFrame(frame);
  }
  function kick() { if (!raf) { last = performance.now(); raf = requestAnimationFrame(frame); } }

  const io = "IntersectionObserver" in window ? new IntersectionObserver((es) => {
    for (const e of es) for (const it of inst.values()) if (it.canvas === e.target) { it.visible = e.isIntersecting; it.dirty = true; }
    kick();
  }) : null;

  // ---------- attach to whatever app.js just drew ----------
  function scan() {
    const cards = [...top.querySelectorAll(".cx-links .cx-node:not(.cx-server)")];
    const peers = [...top.querySelectorAll(".cx-links .cx-nodes .cx-node")].map((n) => n.classList.contains("on"));
    const seen = new Set();
    cards.forEach((card, i) => {
      const head = card.querySelector(":scope > header"), name = card.querySelector("h3")?.textContent.trim();
      if (!head || !name) return;
      const hub = card.classList.contains("cx-hub"), key = (hub ? "hub:" : "node:") + name;
      seen.add(key);
      let it = inst.get(key);
      if (!it) { it = create(key, hub, head.querySelector("p")?.textContent || "", i); if (!it) return; inst.set(key, it); }
      it.online = hub || card.classList.contains("on");
      it.peers = peers; it.card = card; it.dirty = true; it.goneAt = 0;
      if (it.canvas.parentNode !== head) head.insertBefore(it.canvas, head.firstChild);
      head.classList.add("has-3d");
    });
    const now = Date.now();
    for (const [k, it] of inst) {
      if (seen.has(k)) continue;
      it.goneAt ||= now;
      if (now - it.goneAt > 30000) {           // a computer left the cluster: free its context
        io?.unobserve(it.canvas);
        it.g.gl.getExtension("WEBGL_lose_context")?.loseContext();
        inst.delete(k);
      }
    }
    kick();
  }
  let queued = false;
  new MutationObserver(() => { if (!queued) { queued = true; queueMicrotask(() => { queued = false; scan(); }); } })
    .observe(top, { childList: true });

  // pointer: the hovered card's laptop turns to follow the cursor
  let hoverCard = null, hoverRect = null;
  document.addEventListener("scroll", () => { hoverRect = null; }, true);
  document.addEventListener("pointermove", (e) => {
    const card = e.target.closest?.(".cx-links .cx-node");
    if (card !== hoverCard) { hoverCard = card; hoverRect = null; }
    let changed = false;
    for (const it of inst.values()) {
      let h = null;
      if (card && it.card === card) {
        if (!hoverRect) hoverRect = card.getBoundingClientRect();
        const width = hoverRect.width || 1;
        h = Math.max(-1, Math.min(1, ((e.clientX - hoverRect.left) / width) * 2 - 1));
      }
      if (h !== it.hover) { it.hover = h; it.dirty = true; changed = true; }
    }
    if (changed) kick();
  }, { passive: true });

  const retheme = () => { theme = readTheme(); for (const it of inst.values()) { it.dirty = true; it.lastScr = -1; } kick(); };
  new MutationObserver(retheme).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme", "class"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", retheme);
  reduce.addEventListener?.("change", () => { for (const it of inst.values()) it.dirty = true; kick(); });
  document.addEventListener("visibilitychange", kick);
  addEventListener("hashchange", () => setTimeout(kick, 50));
  addEventListener("resize", () => { hoverRect = null; for (const it of inst.values()) it.dirty = true; kick(); });
  // Opening the Connections tab un-hides #view-devices; watch for that too.
  const view = document.getElementById("view-devices");
  if (view) new MutationObserver(() => { for (const it of inst.values()) it.dirty = true; kick(); })
    .observe(view, { attributes: true, attributeFilter: ["hidden"] });
  scan();
})();
