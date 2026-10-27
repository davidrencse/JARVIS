// The memory field: JARVIS's real memory graph (/api/graph/heap) drawn on the GPU behind the
// pipeline wheel. Every item is a node, every link a glowing filament; kind hubs ("message",
// "reply", "note"...) are the bright cores. Pulses run along links, dust drifts on a slow
// flow field, and when a stage fires a bundle of strands shoots from memory into that stage.
// Raw WebGL2, no libraries. Lines are instanced screen-space ribbons, because GL line
// primitives are always 1 px wide. wheel.js owns the view, the loop and the input.
(function () {
  "use strict";

  const SEGS = 28;              // ribbon segments per curve
  const MAX_NODES = 420, AMBIENT = 320, DUST = 2600, STRANDS = 14, MAX_BURSTS = 8;

  const EDGE_VS = `#version 300 es
  layout(location=0) in vec4 a_pts;   // p0.xy, p1.xy in unit-disc space
  layout(location=1) in vec4 a_meta;  // seed, weight, age (<0: a plain link), bend
  layout(location=2) in vec2 a_ids;   // node indices, for hover highlighting
  uniform vec2 u_center; uniform float u_radius; uniform vec3 u_view; uniform vec2 u_res;
  uniform float u_time, u_width, u_segs;
  out float v_t, v_side, v_seed, v_w, v_age, v_hot;
  uniform float u_hover;
  vec2 bez(vec2 a, vec2 c, vec2 b, float t) { float u = 1. - t; return u*u*a + 2.*u*t*c + t*t*b; }
  void main() {
    int seg = gl_VertexID / 6, k = gl_VertexID % 6;
    float cx = (k == 1 || k == 4 || k == 5) ? 1. : 0.;
    float cy = (k == 2 || k == 3 || k == 5) ? 1. : -1.;
    float t = (float(seg) + cx) / u_segs;
    vec2 a = a_pts.xy, b = a_pts.zw, d = b - a;
    vec2 n = normalize(vec2(-d.y, d.x) + 1e-6);
    float bend = a_meta.w + sin(u_time * .21 + a_meta.x * 40.) * .07;
    vec2 c = (a + b) * .5 + n * length(d) * bend;
    vec2 p = bez(a, c, b, t);
    vec2 tang = normalize(bez(a, c, b, min(1., t + .01)) - bez(a, c, b, max(0., t - .01)) + 1e-6);
    vec2 s = u_view.yz + (u_center + p * u_radius) * u_view.x;
    float hot = (a_ids.x == u_hover || a_ids.y == u_hover) ? 1. : 0.;
    float w = u_width * (.7 + a_meta.y * .9 + hot * .8) * (a_meta.z >= 0. ? 1.4 : 1.);
    s += vec2(-tang.y, tang.x) * cy * w;
    gl_Position = vec4(s / u_res * 2. - 1., 0., 1.); gl_Position.y = -gl_Position.y;
    v_t = t; v_side = cy; v_seed = a_meta.x; v_w = a_meta.y; v_age = a_meta.z; v_hot = hot;
  }`;
  const EDGE_FS = `#version 300 es
  precision highp float;
  in float v_t, v_side, v_seed, v_w, v_age, v_hot;
  uniform vec3 u_ink; uniform float u_time, u_alpha;
  out vec4 o;
  void main() {
    float edge = 1. - abs(v_side); edge *= edge;
    float a;
    if (v_age >= 0.) {                       // a recall bundle racing into the ring
      float head = v_age * 1.35;
      float trail = smoothstep(head - .42, head, v_t) * step(v_t, head);
      a = edge * ((1. - v_age) * .22 + trail * (1.2 - v_age));
    } else {
      float base = u_alpha * (.18 + .9 * v_w);
      float ph = fract(u_time * (.035 + .09 * fract(v_seed * 3.7)) + v_seed);
      float pulse = exp(-pow((v_t - ph) * 16., 2.)) * step(.5, fract(v_seed * 11.3));
      a = edge * (base + pulse * (.55 + v_w) + v_hot * .6);
    }
    o = vec4(u_ink * a, a);
  }`;
  const NODE_VS = `#version 300 es
  layout(location=0) in vec4 a_node;  // x, y, size, seed
  layout(location=1) in float a_hub;
  uniform vec2 u_center; uniform float u_radius; uniform vec3 u_view; uniform vec2 u_res;
  uniform float u_time, u_hover;
  out vec2 v_uv; out float v_hub, v_tw, v_hot;
  void main() {
    int k = gl_VertexID % 6;
    vec2 q = vec2((k == 1 || k == 4 || k == 5) ? 1. : -1., (k == 2 || k == 3 || k == 5) ? 1. : -1.);
    float hot = float(gl_InstanceID) == u_hover ? 1. : 0.;
    float r = a_node.z * (1. + hot * 1.6) * (1. + .15 * sin(u_time * 1.7 + a_node.w * 30.));
    vec2 s = u_view.yz + (u_center + a_node.xy * u_radius) * u_view.x + q * r;
    gl_Position = vec4(s / u_res * 2. - 1., 0., 1.); gl_Position.y = -gl_Position.y;
    v_uv = q; v_hub = a_hub; v_hot = hot;
    v_tw = .55 + .45 * sin(u_time * (.6 + a_node.w) + a_node.w * 17.);
  }`;
  const NODE_FS = `#version 300 es
  precision highp float;
  in vec2 v_uv; in float v_hub, v_tw, v_hot;
  uniform vec3 u_ink; out vec4 o;
  void main() {
    float d = length(v_uv);
    float core = smoothstep(.45, .05, d);
    float glow = exp(-d * d * 4.) * .5;
    float ring = v_hub * smoothstep(.06, 0., abs(d - .82)) * .9;
    float a = (core * (.55 + .45 * v_tw) + glow * (.4 + v_hub) + ring + v_hot * glow) * step(d, 1.);
    o = vec4(u_ink * a, a);
  }`;
  const DUST_VS = `#version 300 es
  layout(location=0) in vec3 a_dust;  // angle seed, radius seed, speed seed
  uniform vec2 u_center; uniform float u_radius; uniform vec3 u_view; uniform vec2 u_res;
  uniform float u_time, u_dpr;
  out float v_a;
  void main() {
    float ang = a_dust.x * 6.2831 + u_time * (.012 + .03 * a_dust.z) * (a_dust.z > .5 ? 1. : -1.);
    float rad = sqrt(a_dust.y) * 1.08;
    vec2 p = vec2(cos(ang), sin(ang)) * rad;
    p += .035 * vec2(sin(u_time * .37 + p.y * 6. + a_dust.z * 9.), cos(u_time * .29 + p.x * 5.));
    vec2 s = u_view.yz + (u_center + p * u_radius) * u_view.x;
    gl_Position = vec4(s / u_res * 2. - 1., 0., 1.); gl_Position.y = -gl_Position.y;
    gl_PointSize = (1. + a_dust.z * 1.4) * u_dpr * clamp(pow(u_view.x, .4), .7, 2.2);
    v_a = (.12 + .35 * a_dust.z) * (.6 + .4 * sin(u_time * 2. + a_dust.x * 50.));
  }`;
  const DUST_FS = `#version 300 es
  precision highp float;
  in float v_a; uniform vec3 u_ink; out vec4 o;
  void main() { float d = length(gl_PointCoord - .5) * 2.; float a = v_a * smoothstep(1., 0., d); o = vec4(u_ink * a, a); }`;

  function program(gl, vs, fs) {
    const p = gl.createProgram();
    for (const [type, src] of [[gl.VERTEX_SHADER, vs], [gl.FRAGMENT_SHADER, fs]]) {
      const s = gl.createShader(type);
      gl.shaderSource(s, src); gl.compileShader(s);
      if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
      gl.attachShader(p, s);
    }
    gl.linkProgram(p);
    if (!gl.getProgramParameter(p, gl.LINK_STATUS)) throw new Error(gl.getProgramInfoLog(p));
    const u = {};
    for (let i = 0, n = gl.getProgramParameter(p, gl.ACTIVE_UNIFORMS); i < n; i++) {
      const name = gl.getActiveUniform(p, i).name; u[name] = gl.getUniformLocation(p, name);
    }
    return { p, u };
  }
  function rng(seed) { return () => ((seed = (seed * 16807) % 2147483647) / 2147483647); }

  // Force-directed layout in the unit disc; runs once per graph change (~256 nodes: tens of ms).
  function layoutGraph(graph) {
    const rand = rng(1337);
    let nodes = graph.nodes || [];
    const degree = new Map();
    for (const e of graph.edges || []) for (const id of [e.src, e.dst]) degree.set(id, (degree.get(id) || 0) + 1);
    if (nodes.length > MAX_NODES) nodes = [...nodes].sort((a, b) => (degree.get(b.id) || 0) - (degree.get(a.id) || 0)).slice(0, MAX_NODES);
    const index = new Map(nodes.map((n, i) => [n.id, i]));
    const edges = (graph.edges || []).filter((e) => index.has(e.src) && index.has(e.dst)).map((e) => [index.get(e.src), index.get(e.dst)]);
    const N = nodes.length, P = new Float32Array(N * 2), V = new Float32Array(N * 2);
    const hub = nodes.map((n) => (n.kind !== "item" ? 1 : 0));
    // Galaxy layout: hubs (memory kinds) spread round the disc, each hub's memories on a
    // golden-angle spiral around it, then a short relaxation so cross-links pull them organic.
    const hubs = nodes.map((n, i) => (hub[i] ? i : -1)).filter((i) => i >= 0);
    hubs.forEach((h, k) => {
      const a = (k / hubs.length) * 6.283 + .4, r = hubs.length > 1 ? .52 : 0;
      P[2 * h] = Math.cos(a) * r; P[2 * h + 1] = Math.sin(a) * r * .92;
    });
    const owner = new Int32Array(N).fill(-1);
    for (const [a, b] of edges) { if (hub[b] && !hub[a] && owner[a] < 0) owner[a] = b; else if (hub[a] && !hub[b] && owner[b] < 0) owner[b] = a; }
    const groups = new Map();
    for (let i = 0; i < N; i++) if (!hub[i]) { const g = owner[i]; if (!groups.has(g)) groups.set(g, []); groups.get(g).push(i); }
    for (const [g, members] of groups) {
      const cx = g >= 0 ? P[2 * g] : 0, cy = g >= 0 ? P[2 * g + 1] : 0;
      const spread = Math.min(.55, .12 + Math.sqrt(members.length) * .028);
      members.forEach((i, j) => {
        const th = j * 2.39996 + rand() * .3, r = spread * Math.sqrt((j + .5) / members.length) * (.85 + rand() * .3);
        P[2 * i] = cx + Math.cos(th) * r; P[2 * i + 1] = cy + Math.sin(th) * r;
      });
    }
    const ITER = 70;
    for (let it = 0; it < ITER; it++) {
      const temp = .02 * (1 - it / ITER) + .001;
      V.fill(0);
      for (let i = 0; i < N; i++) for (let j = i + 1; j < N; j++) {
        const dx = P[2 * i] - P[2 * j], dy = P[2 * i + 1] - P[2 * j + 1], d2 = dx * dx + dy * dy + 1e-4;
        if (d2 > .04) continue;
        const f = .00012 / d2;
        V[2 * i] += dx * f; V[2 * i + 1] += dy * f; V[2 * j] -= dx * f; V[2 * j + 1] -= dy * f;
      }
      for (const [a, b] of edges) {
        if (hub[a] || hub[b]) continue;   // only memory-to-memory links pull
        const dx = P[2 * b] - P[2 * a], dy = P[2 * b + 1] - P[2 * a + 1];
        V[2 * a] += dx * .02; V[2 * a + 1] += dy * .02; V[2 * b] -= dx * .02; V[2 * b + 1] -= dy * .02;
      }
      for (let i = 0; i < N; i++) {
        if (hub[i]) continue;
        const vx = V[2 * i], vy = V[2 * i + 1], m = Math.hypot(vx, vy), s = m > temp ? temp / m : 1;
        P[2 * i] += vx * s; P[2 * i + 1] += vy * s;
      }
    }
    let max = 1e-6;
    for (let i = 0; i < N; i++) max = Math.max(max, Math.hypot(P[2 * i], P[2 * i + 1]));
    for (let i = 0; i < 2 * N; i++) P[i] *= .94 / max;
    return { nodes, edges, P, hub };
  }

  window.JarvisField = function (host, before) {
    const canvas = document.createElement("canvas");
    canvas.className = "lab-gl"; canvas.setAttribute("aria-hidden", "true");
    host.insertBefore(canvas, before);
    const gl = canvas.getContext("webgl2", { alpha: false, antialias: true, premultipliedAlpha: true });
    if (!gl) { canvas.remove(); return null; }
    let progs;
    try { progs = { edge: program(gl, EDGE_VS, EDGE_FS), node: program(gl, NODE_VS, NODE_FS), dust: program(gl, DUST_VS, DUST_FS) }; }
    catch (e) { console.error("memory field", e); canvas.remove(); return null; }

    const vao = (setup) => { const v = gl.createVertexArray(); gl.bindVertexArray(v); setup(); gl.bindVertexArray(null); return v; };
    const buffer = (data, usage = gl.STATIC_DRAW) => { const b = gl.createBuffer(); gl.bindBuffer(gl.ARRAY_BUFFER, b); gl.bufferData(gl.ARRAY_BUFFER, data, usage); return b; };
    function attr(loc, size, stride, offset, divisor) {
      gl.enableVertexAttribArray(loc); gl.vertexAttribPointer(loc, size, gl.FLOAT, false, stride, offset); gl.vertexAttribDivisor(loc, divisor);
    }
    function edgeVao(data) {  // 10 floats per instance: pts(4) meta(4) ids(2)
      const b = buffer(data, gl.DYNAMIC_DRAW);
      return { b, v: vao(() => { gl.bindBuffer(gl.ARRAY_BUFFER, b); attr(0, 4, 40, 0, 1); attr(1, 4, 40, 16, 1); attr(2, 2, 40, 32, 1); }) };
    }

    // dust: static
    const dr = rng(42), dust = new Float32Array(DUST * 3);
    for (let i = 0; i < dust.length; i++) dust[i] = dr();
    const dustVao = vao(() => { buffer(dust); attr(0, 3, 12, 0, 0); });

    let graph = null, links = null, nodesVao = null, nodeCount = 0, linkCount = 0, nodeData = null;
    const bursts = []; let burstGL = edgeVao(new Float32Array(MAX_BURSTS * STRANDS * 10));
    let hover = -1, center = [0, 0], radius = 1, W = 1, H = 1, dpr = 1;

    function setGraph(raw) {
      graph = layoutGraph(raw);
      const { nodes, edges, P, hub } = graph, rand = rng(99);
      const deg = new Float32Array(nodes.length);
      for (const [a, b] of edges) { deg[a]++; deg[b]++; }
      const data = [];
      for (const [a, b] of edges) {
        const w = Math.min(1, (hub[a] + hub[b]) * .35 + Math.log1p(deg[a] + deg[b]) * .06);
        data.push(P[2 * a], P[2 * a + 1], P[2 * b], P[2 * b + 1], rand(), w, -1, (rand() - .5) * .5, a, b);
      }
      for (let i = 0; i < AMBIENT && nodes.length > 1; i++) {   // the faint tangle between distant memories
        const a = (rand() * nodes.length) | 0, b = (rand() * nodes.length) | 0;
        data.push(P[2 * a], P[2 * a + 1], P[2 * b], P[2 * b + 1], rand(), .12, -1, (rand() - .5) * 1.4, -9, -9);
      }
      links && gl.deleteBuffer(links.b);
      links = edgeVao(new Float32Array(data)); linkCount = data.length / 10;
      nodeData = new Float32Array(nodes.length * 4);
      const hubs = new Float32Array(nodes.length);
      nodes.forEach((n, i) => {
        nodeData.set([P[2 * i], P[2 * i + 1], hub[i] ? 7 : 1.6 + Math.min(3, (n.importance || .3) * 3 + deg[i] * .25), rand()], i * 4);
        hubs[i] = hub[i];
      });
      nodesVao = vao(() => { buffer(nodeData); attr(0, 4, 16, 0, 1); buffer(hubs); attr(1, 1, 4, 0, 1); });
      nodeCount = nodes.length;
    }

    function layout(w, h, ratio, G) {
      W = w; H = h; dpr = ratio;
      canvas.width = Math.round(w * ratio); canvas.height = Math.round(h * ratio);
      // Fill what the home view shows left of the ring (wheel.js home(): the inner edge sits
      // at 42% of the width, zoomed 1.35x; 1.15x on narrow screens).
      const k = w < 640 ? 1.15 : 1.35;
      radius = Math.min((.42 * w / k) * .62, (h / k) * .5);
      center = [G.cx + G.r - radius * 1.02, G.cy];
    }

    // A bundle of strands from a memory hub to a point on the ring (world coordinates).
    function burst(tx, ty, now) {
      if (!graph || !graph.nodes.length) return;
      const hubs = graph.hub.map((h, i) => (h ? i : -1)).filter((i) => i >= 0);
      const from = hubs.length ? hubs[(Math.random() * hubs.length) | 0] : (Math.random() * graph.nodes.length) | 0;
      bursts.push({ from, to: [(tx - center[0]) / radius, (ty - center[1]) / radius], born: now });
      if (bursts.length > MAX_BURSTS) bursts.shift();
    }

    function pick(sx, sy, view) {
      if (!graph) return null;
      let best = -1, bestD = 14 * 14;
      for (let i = 0; i < graph.nodes.length; i++) {
        const x = view.x + (center[0] + graph.P[2 * i] * radius) * view.k, y = view.y + (center[1] + graph.P[2 * i + 1] * radius) * view.k;
        const d = (x - sx) ** 2 + (y - sy) ** 2;
        if (d < bestD) { bestD = d; best = i; }
      }
      hover = best;
      if (best < 0) return null;
      const n = graph.nodes[best];
      const x = view.x + (center[0] + graph.P[2 * best] * radius) * view.k, y = view.y + (center[1] + graph.P[2 * best + 1] * radius) * view.k;
      const links = graph.edges.filter(([a, b]) => a === best || b === best).length;
      return { node: n, x, y, links };
    }

    function draw({ view, time, now, ink, bg, dark }) {
      gl.viewport(0, 0, canvas.width, canvas.height);
      gl.clearColor(bg[0], bg[1], bg[2], 1); gl.clear(gl.COLOR_BUFFER_BIT);
      gl.enable(gl.BLEND);
      gl.blendFunc(gl.ONE, dark ? gl.ONE : gl.ONE_MINUS_SRC_ALPHA);
      const common = (p) => {
        gl.useProgram(p.p);
        gl.uniform2f(p.u.u_center, center[0], center[1]); gl.uniform1f(p.u.u_radius, radius);
        gl.uniform3f(p.u.u_view, view.k, view.x, view.y); gl.uniform2f(p.u.u_res, W, H);
        gl.uniform1f(p.u.u_time, time); gl.uniform3f(p.u.u_ink, ink[0], ink[1], ink[2]);
        if (p.u.u_hover) gl.uniform1f(p.u.u_hover, hover);
      };
      // dust
      common(progs.dust); gl.uniform1f(progs.dust.u.u_dpr, dpr);
      gl.bindVertexArray(dustVao); gl.drawArrays(gl.POINTS, 0, DUST);
      // links + ambient tangle
      const e = progs.edge;
      common(e); gl.uniform1f(e.u.u_segs, SEGS); gl.uniform1f(e.u.u_width, Math.max(.55, Math.min(1.6, .75 * Math.sqrt(view.k))));
      gl.uniform1f(e.u.u_alpha, dark ? .32 : .5);
      if (links) { gl.bindVertexArray(links.v); gl.drawArraysInstanced(gl.TRIANGLES, 0, SEGS * 6, linkCount); }
      // recall bundles
      const live = bursts.filter((b) => now - b.born < 1.4);
      if (live.length && graph) {
        const data = new Float32Array(live.length * STRANDS * 10); let o = 0;
        for (const b of live) {
          const age = (now - b.born) / 1.4, fx = graph.P[2 * b.from], fy = graph.P[2 * b.from + 1];
          for (let s = 0; s < STRANDS; s++) {
            const spread = (s - STRANDS / 2) * .006;
            data.set([fx + spread, fy - spread, b.to[0], b.to[1] + spread * 2, s * .13 + b.born % 1, 1, age, -.18 + spread * 6, -9, -9], o); o += 10;
          }
        }
        gl.bindBuffer(gl.ARRAY_BUFFER, burstGL.b); gl.bufferSubData(gl.ARRAY_BUFFER, 0, data);
        gl.bindVertexArray(burstGL.v); gl.drawArraysInstanced(gl.TRIANGLES, 0, SEGS * 6, live.length * STRANDS);
      }
      // nodes
      if (nodesVao) { common(progs.node); gl.bindVertexArray(nodesVao); gl.drawArraysInstanced(gl.TRIANGLES, 0, 6, nodeCount); }
      gl.bindVertexArray(null);
      return live.length > 0;
    }

    function unhover() { hover = -1; }
    return { setGraph, layout, burst, pick, draw, unhover, canvas };
  };
})();
