// Porcelain backdrop rendered on the GPU: a few soft, glossy blobs (metaballs) drifting
// over a pale field, lit from above with contact shadows, the material the console's
// frosted cards sit on. Graphite in the dark theme. The highlight brightens with the
// microphone level while JARVIS is listening. Renders once and stops when the visitor
// prefers reduced motion or the tab is hidden.
(function () {
  const canvas = document.getElementById("terrain");
  const gl = canvas.getContext("webgl", { antialias: false, alpha: false, preserveDrawingBuffer: false });
  window.Terrain = { level: 0, redraw() {} };
  if (!gl) { canvas.style.background = "var(--bg)"; return; }

  const vs = `attribute vec2 p; void main(){ gl_Position = vec4(p, 0.0, 1.0); }`;
  const fs = `
precision highp float;
uniform vec2 res; uniform float t; uniform float light; uniform float level;
float hash2(vec2 p){ return fract(sin(dot(p, vec2(12.9898, 78.233))) * 43758.5453); }
// Metaball field: sum of r^2 / d^2 for five slowly orbiting blobs.
float field(vec2 p){
  float f = 0.0;
  for (int i = 0; i < 5; i++) {
    float fi = float(i);
    vec2 c = vec2(0.18 + 0.17 * fi + 0.06 * sin(t * 0.05 + fi * 1.7),
                  0.50 + 0.30 * sin(fi * 2.3 + t * 0.04) * cos(fi * 1.1));
    float r = 0.075 + 0.035 * fract(fi * 0.618);
    vec2 d = p - c;
    f += r * r / max(dot(d, d), 1e-5);
  }
  return f;
}
void main(){
  vec2 uv = gl_FragCoord.xy / res;
  float aspect = res.x / res.y;
  vec2 p = vec2(uv.x * aspect / 1.6, uv.y);
  float day = step(0.5, light);
  vec3 ground = mix(vec3(0.075, 0.075, 0.082), vec3(0.905, 0.905, 0.915), day);
  vec3 col = ground + (uv.y - 0.5) * mix(0.03, 0.05, day);
  // contact shadow: the field sampled a little below and blurred by its own falloff
  float sh = field(p + vec2(0.0, 0.035));
  col *= 1.0 - smoothstep(0.35, 1.2, sh) * mix(0.35, 0.16, day);
  float f = field(p);
  if (f > 0.9) {
    float e = 0.0025;
    vec2 g = vec2(field(p + vec2(e, 0.0)) - field(p - vec2(e, 0.0)), field(p + vec2(0.0, e)) - field(p - vec2(0.0, e)));
    float edge = smoothstep(0.9, 1.05, f);
    vec3 n = normalize(vec3(-g * 0.06, 1.0));
    vec3 l = normalize(vec3(-0.35, 0.55, 0.75));
    float diff = clamp(dot(n, l), 0.0, 1.0);
    vec3 h = normalize(l + vec3(0.0, 0.0, 1.0));
    float spec = pow(clamp(dot(n, h), 0.0, 1.0), 48.0) * (0.55 + level * 0.5);
    float rim = pow(1.0 - n.z, 2.0);
    vec3 body = mix(vec3(0.13, 0.13, 0.14), vec3(0.985, 0.985, 0.99), day);
    vec3 blob = body * (0.62 + 0.42 * diff) + spec * mix(vec3(0.55), vec3(1.0), day) - rim * mix(0.02, 0.10, day);
    col = mix(col, blob, edge);
  }
  col += (hash2(gl_FragCoord.xy + t) - 0.5) * 0.012;
  vec2 q = uv - 0.5;
  col *= 1.0 - dot(q, q) * mix(0.5, 0.12, day);
  gl_FragColor = vec4(col, 1.0);
}`;
  function compile(type, src) {
    const s = gl.createShader(type); gl.shaderSource(s, src); gl.compileShader(s);
    if (!gl.getShaderParameter(s, gl.COMPILE_STATUS)) throw new Error(gl.getShaderInfoLog(s));
    return s;
  }
  let prog;
  try {
    prog = gl.createProgram();
    gl.attachShader(prog, compile(gl.VERTEX_SHADER, vs));
    gl.attachShader(prog, compile(gl.FRAGMENT_SHADER, fs));
    gl.linkProgram(prog);
  } catch (e) { console.warn("terrain shader", e); return; }
  gl.useProgram(prog);
  const buf = gl.createBuffer();
  gl.bindBuffer(gl.ARRAY_BUFFER, buf);
  gl.bufferData(gl.ARRAY_BUFFER, new Float32Array([-1, -1, 1, -1, -1, 1, 1, 1]), gl.STATIC_DRAW);
  const loc = gl.getAttribLocation(prog, "p");
  gl.enableVertexAttribArray(loc);
  gl.vertexAttribPointer(loc, 2, gl.FLOAT, false, 0, 0);
  const U = { res: gl.getUniformLocation(prog, "res"), t: gl.getUniformLocation(prog, "t"),
              light: gl.getUniformLocation(prog, "light"), level: gl.getUniformLocation(prog, "level") };

  // The glass blurs it anyway, so half resolution is plenty and keeps the GPU idle.
  const SCALE = 0.5;
  function resize() {
    const w = Math.max(1, Math.round(innerWidth * SCALE)), h = Math.max(1, Math.round(innerHeight * SCALE));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    gl.viewport(0, 0, w, h);
  }
  const reduce = matchMedia("(prefers-reduced-motion: reduce)");
  const start = performance.now() - 40000;
  let smooth = 0, raf = 0, last = 0;
  function draw(now) {
    raf = 0;
    resize();
    smooth += (window.Terrain.level - smooth) * 0.18;
    gl.uniform2f(U.res, canvas.width, canvas.height);
    gl.uniform1f(U.t, (reduce.matches ? 40000 : now - start) / 1000);
    gl.uniform1f(U.light, document.documentElement.dataset.theme === "light" ? 1 : 0);
    gl.uniform1f(U.level, Math.min(1, smooth));
    gl.drawArrays(gl.TRIANGLE_STRIP, 0, 4);
    if (!reduce.matches && !document.hidden) schedule();
  }
  // ~20 fps is enough for a drift this slow; full rate only while the mic is live.
  function schedule() {
    if (raf) return;
    raf = requestAnimationFrame((now) => {
      if (window.Terrain.level > 0.01 || smooth > 0.01 || now - last > 50) { last = now; draw(now); }
      else { raf = 0; schedule(); }
    });
  }
  window.Terrain.redraw = () => { if (!raf) raf = requestAnimationFrame(draw); };
  addEventListener("resize", window.Terrain.redraw);
  document.addEventListener("visibilitychange", () => { if (!document.hidden) window.Terrain.redraw(); });
  reduce.addEventListener?.("change", window.Terrain.redraw);
  window.Terrain.redraw();
})();
