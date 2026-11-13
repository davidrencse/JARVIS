/* JARVIS as hardware: a 3D circuit board for the System design page (three.js, local copy).
   Original design, not a copy of any product: each part is a real piece of JARVIS.
     CPU + tower cooler = the brain (fan speeds up while a request is being thought about)
     RAM sticks         = memory, one stick per kind; chips and the LED bar light with item count
     M.2 drive          = the vault          chipset heatsink = the gates
     graphics card      = the action tools   I/O shield       = voice + network
   Light pulses run along the copper traces as a request moves through JARVIS (turn.step events).
   Monochrome metal like a studio render; colour means state: amber = activity, red = error.
   It renders only while visible. A settled frame (camera home, fans stopped, nothing in flight)
   does not schedule another one. Reduced motion still snaps the camera and keeps the fans still. */
import * as THREE from "./vendor/three-0.170.0.module.min.js";

// Mounted on the System design page (#design-board): it fills its box, feeds itself live events,
// and clicking a part zooms in to it. (The console's stage runs engine3d.js instead.)
const get = (path) => fetch(path).then((r) => r.json());
const host = document.getElementById("design-board");
if (host) start(host);

function start(host) {
  const canvas = host.querySelector("canvas");
  const tip = host.querySelector(".board-tip");
  let reduced = matchMedia("(prefers-reduced-motion: reduce)").matches;
  let renderer;
  try {
    renderer = new THREE.WebGLRenderer({ canvas, antialias: true, powerPreference: "high-performance" });
  } catch (err) {
    host.classList.add("no-webgl");
    host.querySelector(".board-fallback")?.removeAttribute("hidden");
    return;
  }
  renderer.outputColorSpace = THREE.SRGBColorSpace;
  renderer.toneMapping = THREE.ACESFilmicToneMapping;
  renderer.toneMappingExposure = 1.3;
  renderer.shadowMap.enabled = true;
  renderer.shadowMap.type = THREE.PCFSoftShadowMap;
  renderer.shadowMap.autoUpdate = false;  // the board is static: shadows are baked once

  const scene = new THREE.Scene();
  scene.background = new THREE.Color(0x060606);
  const camera = new THREE.PerspectiveCamera(26, 1, 0.1, 100);

  // ---------- lights: a soft studio key, a cool fill, a rim from behind ----------
  scene.add(new THREE.HemisphereLight(0xdfe6ee, 0x0a0a0a, 0.55));
  const key = new THREE.DirectionalLight(0xffffff, 2.4);
  key.position.set(-6, 14, -4); key.castShadow = true;
  key.shadow.mapSize.set(1024, 1024);
  Object.assign(key.shadow.camera, { left: -8, right: 8, top: 9, bottom: -9, near: 1, far: 40 });
  key.shadow.bias = -0.0006; key.shadow.radius = 3;
  scene.add(key);
  const rim = new THREE.DirectionalLight(0x9fb4c8, 0.9); rim.position.set(7, 6, 9); scene.add(rim);

  // ---------- materials ----------
  const M = {
    metal: new THREE.MeshStandardMaterial({ color: 0x2b2d30, metalness: 0.85, roughness: 0.38 }),
    metalLight: new THREE.MeshStandardMaterial({ color: 0x6d7176, metalness: 0.9, roughness: 0.32 }),
    fin: new THREE.MeshStandardMaterial({ color: 0xa3a8ae, metalness: 0.9, roughness: 0.3 }),
    plastic: new THREE.MeshStandardMaterial({ color: 0x141516, metalness: 0.1, roughness: 0.7 }),
    pcbGreen: new THREE.MeshStandardMaterial({ color: 0x1b1d1c, metalness: 0.2, roughness: 0.6 }),
    chip: new THREE.MeshStandardMaterial({ color: 0x0d0d0e, metalness: 0.3, roughness: 0.45 }),
    gold: new THREE.MeshStandardMaterial({ color: 0x8a7a55, metalness: 1, roughness: 0.35 }),
  };
  const led = (on) => new THREE.MeshStandardMaterial({ color: 0x111111, emissive: on ? 0xdfe9f5 : 0x000000, emissiveIntensity: 1.4, roughness: 0.4 });

  // ---------- geometry helpers ----------
  const box = (w, h, d, mat, x, y, z, parent = board) => {
    const m = new THREE.Mesh(new THREE.BoxGeometry(w, h, d), mat);
    m.position.set(x, y + h / 2, z); m.castShadow = m.receiveShadow = true; parent.add(m); return m;
  };
  // an angular armour plate: a polygon (x, z pairs) extruded upward, with a bevel
  const plate = (pts, h, mat, y = 0.16) => {
    const s = new THREE.Shape(pts.map(([x, z]) => new THREE.Vector2(x, -z)));
    const g = new THREE.ExtrudeGeometry(s, { depth: h, bevelEnabled: true, bevelThickness: 0.04, bevelSize: 0.04, bevelSegments: 2 });
    g.rotateX(-Math.PI / 2);
    const m = new THREE.Mesh(g, mat); m.position.y = y; m.castShadow = m.receiveShadow = true; board.add(m); return m;
  };
  const label = (text, w, h, opts = {}) => {
    const c = document.createElement("canvas"), k = 8; c.width = Math.round(w * 64 * k / 4); c.height = Math.round(h * 64 * k / 4);
    const g = c.getContext("2d");
    g.fillStyle = opts.bg || "rgba(0,0,0,0)"; g.fillRect(0, 0, c.width, c.height);
    g.fillStyle = opts.color || "#c9cdd2"; g.textAlign = "center"; g.textBaseline = "middle";
    g.font = `${opts.weight || 600} ${Math.round(c.height * (opts.size || 0.52))}px "Geist Mono", ui-monospace, monospace`;
    g.fillText(text, c.width / 2, c.height / 2 + 1);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
    const m = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshStandardMaterial({ map: t, transparent: true, roughness: 0.6, metalness: 0.2 }));
    m.rotation.x = -Math.PI / 2; return m;
  };

  // ---------- the board: a procedural PCB texture (silkscreen rays, traces, vias, grid) ----------
  const BW = 10, BD = 12.6;
  const board = new THREE.Group(); scene.add(board);
  const pcb = (() => {
    const c = document.createElement("canvas"); c.width = 2048; c.height = Math.round(2048 * BD / BW);
    const g = c.getContext("2d"), sx = c.width / BW, sz = c.height / BD;
    const P = (x, z) => [(x + BW / 2) * sx, (z + BD / 2) * sz];
    g.fillStyle = "#1b1c1e"; g.fillRect(0, 0, c.width, c.height);
    // fine grid
    g.strokeStyle = "rgba(255,255,255,.025)"; g.lineWidth = 1;
    for (let x = 0; x < c.width; x += 24) { g.beginPath(); g.moveTo(x, 0); g.lineTo(x, c.height); g.stroke(); }
    for (let y = 0; y < c.height; y += 24) { g.beginPath(); g.moveTo(0, y); g.lineTo(c.width, y); g.stroke(); }
    // silkscreen rays from the brain outward: the board's signature pattern
    const [cx, cz] = P(0.2, -2.6);
    for (let i = 0; i < 46; i++) {
      const a = (i / 46) * Math.PI * 2 + 0.03, len = 2600, wv = 10 + (i % 5) * 9;
      g.save(); g.translate(cx, cz); g.rotate(a);
      const grad = g.createLinearGradient(240, 0, len, 0);
      grad.addColorStop(0, "rgba(120,124,130,.0)"); grad.addColorStop(0.25, "rgba(135,139,146,.34)"); grad.addColorStop(1, "rgba(135,139,146,.07)");
      g.fillStyle = grad; g.fillRect(240, -wv / 2, len, wv); g.restore();
    }
    // copper traces in bundles, 45-degree bends
    g.strokeStyle = "rgba(170,160,135,.16)"; g.lineWidth = 3;
    const bundle = (pts, n, gap) => {
      for (let k = 0; k < n; k++) {
        g.beginPath(); pts.forEach(([x, z], i) => { const [px, pz] = P(x + k * gap, z + k * gap * 0.4); i ? g.lineTo(px, pz) : g.moveTo(px, pz); }); g.stroke();
      }
    };
    bundle([[1.4, -2.2], [2.0, -1.6], [2.4, -1.6]], 9, 0.05);
    bundle([[-0.6, -1.2], [-0.6, 0.0], [0.0, 0.6]], 8, 0.05);
    bundle([[-1.2, -0.9], [-2.6, 0.5], [-2.6, 1.4]], 10, 0.045);
    bundle([[1.2, 3.4], [2.0, 4.0], [2.4, 4.0]], 7, 0.05);
    bundle([[-3.6, 3.9], [-1.0, 3.9], [0.6, 5.2]], 6, 0.05);
    // vias
    g.fillStyle = "rgba(210,200,170,.22)";
    const rnd = (s => () => (s = (s * 16807) % 2147483647) / 2147483647)(7);
    for (let i = 0; i < 900; i++) { g.beginPath(); g.arc(rnd() * c.width, rnd() * c.height, 2.2 + rnd() * 1.6, 0, 7); g.fill(); }
    // silkscreen text
    g.fillStyle = "rgba(200,205,210,.55)"; g.font = `600 30px "Geist Mono", monospace`;
    [["JARVIS · CORE BOARD rev 26.11", -4.6, 6.0], ["MEM_A1  MEM_A2  MEM_B1  MEM_B2", 2.35, -6.05], ["PCIE_X16 · DESKTOP", -4.2, 1.15], ["M.2_1 · VAULT", -0.9, 0.05], ["CPU · BRAIN", -0.9, -4.3]]
      .forEach(([t, x, z]) => { const [px, pz] = P(x, z); g.fillText(t, px, pz); });
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
    return t;
  })();
  const base = new THREE.Mesh(new THREE.BoxGeometry(BW, 0.16, BD), new THREE.MeshStandardMaterial({ map: pcb, metalness: 0.25, roughness: 0.8 }));
  base.receiveShadow = true; board.add(base);
  // mounting holes
  for (const [x, z] of [[-4.6, -5.9], [4.6, -5.9], [-4.6, 5.9], [4.6, 5.9], [-4.6, 0.8], [4.6, 0.8]]) {
    const r = new THREE.Mesh(new THREE.TorusGeometry(0.16, 0.05, 8, 24), M.gold); r.rotation.x = Math.PI / 2; r.position.set(x, 0.09, z); board.add(r);
  }

  // ---------- I/O shield and VRM armour (left and top) ----------
  const ioShield = plate([[-4.95, -6.3], [-2.9, -6.3], [-2.9, -3.2], [-3.4, -2.6], [-4.95, -2.6]], 0.9, M.metal);
  const vrmTop = plate([[-2.75, -6.3], [1.9, -6.3], [1.9, -5.3], [1.4, -4.85], [-2.75, -4.85]], 0.55, M.metalLight);
  const vrmLeft = plate([[-2.75, -4.7], [-1.85, -4.7], [-1.85, -0.9], [-2.75, -0.2]], 0.5, M.metalLight);
  const grooves = [];
  for (let i = 0; i < 7; i++) grooves.push(box(0.08, 0.36, 1.3, M.fin, -2.6 + i * 0.12, 0.66, -5.6));  // grooves on the top VRM
  const ioLed = box(0.18, 0.06, 0.18, led(false), -4.3, 1.06, -5.9);  // voice listening LED
  for (let i = 0; i < 5; i++) box(0.5, 0.28, 0.32, M.plastic, -4.45, 0.16, -5.2 + i * 0.5);    // ports

  // ---------- CPU socket + tower cooler with fan = the brain ----------
  const CPU = new THREE.Vector3(0.2, 0, -2.6);
  box(2.7, 0.08, 2.7, M.plastic, CPU.x, 0.08, CPU.z);
  const coolerFins = new THREE.InstancedMesh(new THREE.BoxGeometry(2.3, 1.7, 0.045), M.fin, 26);
  for (let i = 0; i < 26; i++) {
    const m = new THREE.Matrix4().makeTranslation(CPU.x, 1.18, CPU.z - 1.12 + i * 0.09);
    coolerFins.setMatrixAt(i, m);
  }
  coolerFins.castShadow = coolerFins.receiveShadow = true; board.add(coolerFins);
  for (const dx of [-0.6, 0, 0.6]) {  // heat pipes
    const pipe = new THREE.Mesh(new THREE.CylinderGeometry(0.07, 0.07, 1.9, 12), M.gold);
    pipe.position.set(CPU.x + dx, 1.0, CPU.z); pipe.castShadow = true; board.add(pipe);
  }
  box(2.5, 0.12, 2.5, M.metal, CPU.x, 2.03, CPU.z);  // top cap
  const fan = makeFan(1.05, 9);
  fan.group.position.set(CPU.x, 2.16, CPU.z); board.add(fan.group);
  const brainBadge = label("J", 0.5, 0.5, { size: 0.8, weight: 700, color: "#e9edf2" });
  brainBadge.position.set(CPU.x, 2.4, CPU.z); board.add(brainBadge);

  function makeFan(r, blades) {
    const group = new THREE.Group();
    const frame = new THREE.Mesh(new THREE.TorusGeometry(r + 0.06, 0.06, 10, 48), M.metal); frame.rotation.x = Math.PI / 2; group.add(frame);
    const ringMat = new THREE.MeshStandardMaterial({ color: 0x111111, emissive: 0xf0b429, emissiveIntensity: 0, roughness: 0.4 });
    const ring = new THREE.Mesh(new THREE.TorusGeometry(r + 0.02, 0.025, 8, 64), ringMat); ring.rotation.x = Math.PI / 2; ring.position.y = 0.07; group.add(ring);
    const rotor = new THREE.Group(); group.add(rotor);
    const hub = new THREE.Mesh(new THREE.CylinderGeometry(r * 0.3, r * 0.3, 0.12, 32), M.plastic); hub.castShadow = true; rotor.add(hub);
    for (let i = 0; i < blades; i++) {
      const b = new THREE.Mesh(new THREE.BoxGeometry(r * 0.72, 0.025, r * 0.32), M.plastic);
      b.position.set(r * 0.62, 0, 0); b.rotation.x = 0.35;
      const arm = new THREE.Group(); arm.rotation.y = (i / blades) * Math.PI * 2; arm.add(b); rotor.add(arm);
    }
    return { group, rotor, ringMat, speed: 0 };
  }

  // ---------- RAM = memory: one stick per kind ----------
  const sticks = [];
  for (let i = 0; i < 4; i++) {
    const x = 2.55 + i * 0.52, z = -2.9;
    box(0.22, 0.22, 6.0, M.plastic, x, 0.08, z);                      // slot
    const g = new THREE.Group(); g.position.set(x, 0.3, z); board.add(g);
    const pcbStick = box(0.07, 1.25, 5.6, M.pcbGreen, 0, 0, 0, g);
    const spreaderL = box(0.04, 1.15, 5.5, M.metalLight, -0.07, 0.06, 0, g);
    const spreaderR = box(0.04, 1.15, 5.5, M.metalLight, 0.07, 0.06, 0, g);
    const bar = box(0.16, 0.08, 5.4, led(true), 0, 1.25, 0, g);       // LED bar: brightness = items
    const chips = [];
    for (let k = 0; k < 8; k++) chips.push(box(0.1, 0.02, 0.48, led(false), 0, 1.33, -2.3 + k * 0.66, g));
    sticks.push({ group: g, bar, chips, kind: null, count: 0, hot: 0, spreaders: [spreaderL, spreaderR], pcb: pcbStick });
  }

  // ---------- M.2 drive = the vault ----------
  const VAULT = new THREE.Vector3(-0.3, 0, 0.45);
  box(2.8, 0.05, 0.62, M.pcbGreen, VAULT.x, 0.08, VAULT.z);
  box(2.3, 0.16, 0.5, M.metalLight, VAULT.x, 0.13, VAULT.z);
  const vaultLbl = label("VAULT · OBSIDIAN", 2.0, 0.26, { size: 0.5 }); vaultLbl.position.set(VAULT.x, 0.3, VAULT.z); board.add(vaultLbl);
  const vaultLed = box(0.1, 0.04, 0.1, led(false), VAULT.x + 1.25, 0.14, VAULT.z);

  // ---------- graphics card = the action tools ----------
  const ACT = new THREE.Vector3(-0.4, 0, 2.55);
  box(8.6, 0.06, 0.32, M.plastic, -0.5, 0.08, 1.5);  // PCIe slot
  const gpu = new THREE.Group(); gpu.position.set(ACT.x, 0.16, ACT.z); board.add(gpu);
  box(8.4, 0.28, 2.1, M.pcbGreen, 0, 0, 0, gpu);
  plate([[-4.6, 1.6], [3.6, 1.6], [3.9, 1.9], [3.9, 3.55], [-4.6, 3.55]], 0.62, M.metal, 0.44);  // GPU shroud
  for (let i = 0; i < 12; i++) box(0.05, 0.12, 1.7, M.fin, -3.4 + i * 0.6, 1.04, ACT.z);  // shroud vents
  const gpuFans = [-2.2, 1.4].map((dx) => { const f = makeFan(0.78, 11); f.group.position.set(ACT.x + dx, 1.12, ACT.z); board.add(f.group); return f; });
  const actLbl = label("VISION · DESKTOP", 2.4, 0.3, { size: 0.55 }); actLbl.position.set(ACT.x - 0.4, 1.2, ACT.z + 0.75); board.add(actLbl);
  const gpuLed = box(3.2, 0.03, 0.06, led(false), ACT.x + 0.2, 1.12, ACT.z - 0.98);

  // ---------- chipset heatsink = the gates; second slot; small parts ----------
  const GATES = new THREE.Vector3(2.85, 0, 4.55);
  plate([[1.8, 3.7], [3.9, 3.7], [4.3, 4.1], [4.3, 5.5], [2.3, 5.5], [1.8, 5.0]], 0.42, M.metalLight);
  const gatesLbl = label("ROUTER · GATES", 1.9, 0.28, { size: 0.55 }); gatesLbl.position.set(GATES.x + 0.2, 0.65, GATES.z); board.add(gatesLbl);
  // second slot = skills: empty for now, a ghost card stands in it
  const skillSlot = box(6.2, 0.06, 0.3, M.plastic, -1.8, 0.08, 5.1);
  const skillCard = new THREE.Mesh(new THREE.BoxGeometry(5.8, 0.8, 0.07),
    new THREE.MeshStandardMaterial({ color: 0x9aa3ad, transparent: true, opacity: 0.12, roughness: 0.4, metalness: 0.3, depthWrite: false }));
  skillCard.position.set(-1.8, 0.54, 5.1); board.add(skillCard);
  const skillEdges = new THREE.LineSegments(new THREE.EdgesGeometry(skillCard.geometry),
    new THREE.LineDashedMaterial({ color: 0x8a9099, dashSize: 0.12, gapSize: 0.08, transparent: true, opacity: 0.8 }));
  skillEdges.computeLineDistances(); skillEdges.position.copy(skillCard.position); board.add(skillEdges);
  const skillLbl = label("SKILLS · COMING SOON", 2.8, 0.24, { size: 0.55, color: "#8f969e" }); skillLbl.position.set(-1.8, 0.1, 4.72); board.add(skillLbl);
  const localChips = [], sata = [];
  for (let i = 0; i < 6; i++) localChips.push(box(0.34, 0.2, 0.22, M.chip, -4.2 + i * 0.55, 0.08, 4.25));  // small chips = local models
  for (let i = 0; i < 4; i++) sata.push(box(0.4, 0.4, 0.26, M.plastic, 4.6, 0.08, 1.4 + i * 0.5));  // SATA = data stores
  for (let i = 0; i < 14; i++) {  // capacitors round the socket
    const a = (i / 14) * Math.PI * 2, cap = new THREE.Mesh(new THREE.CylinderGeometry(0.09, 0.09, 0.28, 14), M.metalLight);
    cap.position.set(CPU.x + Math.cos(a) * 1.75, 0.22, CPU.z + Math.sin(a) * 1.75); cap.castShadow = true; board.add(cap);
  }

  // ---------- small chips, each a real part with a status light ----------
  const CHIPS = [
    { id: "voice", name: "VOICE", x: -3.4, z: 5.75, part: "audio chip" },
    { id: "cluster", name: "CLUSTER", x: -2.2, z: 5.75, part: "network chip" },
    { id: "lanmouse", name: "INPUT", x: -1.0, z: 5.75, part: "USB header" },
    { id: "chrome", name: "BROWSER", x: 0.2, z: 5.75, part: "web controller" },
    { id: "sandbox", name: "SANDBOX", x: 1.0, z: 4.35, part: "security chip" },
    { id: "log", name: "LOG", x: 1.4, z: 5.75, part: "event log" },
  ];
  for (const c of CHIPS) {
    c.body = box(0.82, 0.12, 0.62, M.chip, c.x, 0.08, c.z);
    c.lbl = label(c.name, 0.95, 0.2, { size: 0.62 }); c.lbl.position.set(c.x, 0.22, c.z + 0.02); board.add(c.lbl);
    c.led = box(0.09, 0.04, 0.09, led(false), c.x + 0.32, 0.2, c.z - 0.24);
    for (let k = 0; k < 5; k++) { box(0.05, 0.02, 0.1, M.gold, c.x - 0.3 + k * 0.15, 0.08, c.z - 0.36); box(0.05, 0.02, 0.1, M.gold, c.x - 0.3 + k * 0.15, 0.08, c.z + 0.36); }
  }
  const LED = { ok: 0x7fdc9a, warn: 0xf0b429, fail: 0xff4d5e, off: 0x000000 };
  const cards = {};
  function setCards(list) {
    for (const c of list || []) cards[c.id] = c;
    for (const chip of CHIPS) {
      const card = chip.id === "voice" ? { state: state.listening ? "ok" : "off" } : chip.id === "log" ? { state: "ok" } : cards[chip.id];
      chip.led.material.emissive.setHex(LED[card ? card.state : "off"] ?? 0);
      chip.led.material.emissiveIntensity = 1.6;
    }
    wake(300);
  }
  let cardsFlight = null, cardsTimer = 0;
  function loadCards() {
    if (cardsFlight) return cardsFlight;
    cardsFlight = (async () => {
      try { setCards(await get("/api/system/components")); } catch (e) {}
      finally { cardsFlight = null; }
    })();
    return cardsFlight;
  }
  loadCards();
  const rows = (id) => (cards[id] ? cards[id].rows : [["status", "loading"]]);

  // ---------- traces that light up: pulses between the parts ----------
  const PATHS = {
    memory: [CPU, new THREE.Vector3(1.7, 0, -2.6), new THREE.Vector3(2.4, 0, -2.0)],
    vault: [CPU, new THREE.Vector3(0.2, 0, -0.6), new THREE.Vector3(VAULT.x + 0.6, 0, VAULT.z)],
    act: [CPU, new THREE.Vector3(-1.2, 0, -1.4), new THREE.Vector3(-2.4, 0, 0.2), new THREE.Vector3(-2.4, 0, ACT.z - 1)],
    gates: [CPU, new THREE.Vector3(1.4, 0, 0.0), new THREE.Vector3(1.4, 0, 3.0), new THREE.Vector3(GATES.x - 0.8, 0, GATES.z - 0.4)],
    voice: [new THREE.Vector3(-3.4, 0, 5.4), new THREE.Vector3(-3.4, 0, 3.9), new THREE.Vector3(-1.5, 0, 0.9), new THREE.Vector3(-0.6, 0, -1.2), CPU],
    browser: [CPU, new THREE.Vector3(0.6, 0, -0.6), new THREE.Vector3(0.9, 0, 3.8), new THREE.Vector3(0.2, 0, 5.4)],
  };
  const traceMat = new THREE.LineBasicMaterial({ color: 0x8c8370, transparent: true, opacity: 0.35 });
  const curves = {};
  for (const [name, pts] of Object.entries(PATHS)) {
    const lifted = pts.map((p) => new THREE.Vector3(p.x, 0.095, p.z));
    curves[name] = new THREE.CatmullRomCurve3(lifted, false, "catmullrom", 0.05);
    board.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(curves[name].getPoints(60)), traceMat));
  }
  const sprite = (() => {
    const c = document.createElement("canvas"); c.width = c.height = 64; const g = c.getContext("2d");
    const r = g.createRadialGradient(32, 32, 0, 32, 32, 32);
    r.addColorStop(0, "rgba(255,255,255,1)"); r.addColorStop(0.25, "rgba(255,220,150,.9)"); r.addColorStop(1, "rgba(255,180,60,0)");
    g.fillStyle = r; g.fillRect(0, 0, 64, 64); return new THREE.CanvasTexture(c);
  })();
  const pulses = [], pulsePos = new THREE.Vector3();
  const pulseMat = (color) => new THREE.SpriteMaterial({ map: sprite, color, transparent: true, depthWrite: false, blending: THREE.AdditiveBlending });
  function pulse(path, color = 0xf0b429) {
    if (!curves[path] || pulses.length > 40) return;
    const s = new THREE.Sprite(pulseMat(color)); s.scale.set(0.55, 0.55, 0.55); board.add(s);
    pulses.push({ s, path, t: 0, speed: 0.55 + Math.random() * 0.3 });
    wake(2500);
  }

  // ---------- data ----------
  const state = { thinking: 0, busyUntil: 0, lastStep: "idle", lastTool: "none yet", kinds: {}, items: 0, links: 0,
                  brain: "brain", warm: false, listening: false, nodes: 0, error: 0, events: 0 };
  function setOverview(o) {
    if (!o) return;
    const mem = o.memory || {};
    state.kinds = mem.kinds || {}; state.items = mem.items || 0; state.links = mem.links || 0;
    state.brain = (o.brain && o.brain.name) || "brain"; state.warm = !!(o.brain && o.brain.warm);
    state.listening = !!(o.voice && o.voice.listening); state.nodes = (o.cluster && o.cluster.nodes) || 0;
    const kinds = Object.entries(state.kinds).sort((a, b) => b[1] - a[1]);
    const max = Math.max(1, ...kinds.map(([, n]) => n));
    sticks.forEach((st, i) => {
      const [kind, n] = kinds[i] || [null, 0];
      st.kind = kind; st.count = n;
      const lit = kind ? 0.12 + 0.88 * (Math.log1p(n) / Math.log1p(max)) : 0;
      st.bar.material.emissiveIntensity = 0.15 + lit * 0.95;
      st.bar.material.emissive.setHex(kind ? 0xdfe9f5 : 0x000000);
      const on = Math.round(lit * st.chips.length);
      st.chips.forEach((c, k) => { c.material.emissive.setHex(k < on ? 0x9fb3c9 : 0x000000); c.material.emissiveIntensity = 0.9; });
    });
    ioLed.material.emissive.setHex(state.listening ? 0x7fdc9a : 0x000000);
    setCards();
    wake(400);
  }
  let overviewFlight = null, overviewTimer = 0;
  function loadOverview() {
    if (overviewFlight) return overviewFlight;
    overviewFlight = (async () => {
      try { setOverview(await get("/api/overview")); } catch (e) {}
      finally { overviewFlight = null; }
    })();
    return overviewFlight;
  }
  function onLive(e) {
    const kind = e && e.kind, d = (e && e.data) || {};
    if (kind && kind !== "hello") {  // every event lands in the log: blink its chip
      state.events++;
      const logLed = CHIPS.find((c) => c.id === "log").led.material;
      logLed.emissive.setHex(0xf0b429); setTimeout(() => { logLed.emissive.setHex(LED.ok); wake(); }, 220); wake();
    }
    if (kind === "turn.start") { state.thinking = 1; state.busyUntil = performance.now() + 60000; pulse("voice", 0xdfe9f5); }
    if (kind === "turn.done") {
      state.thinking = 0; state.busyUntil = performance.now() + 1500; pulse("memory");
      sticks[0] && (sticks[0].hot = 1);
      if (d.error) { state.error = 1; wake(); }
    }
    if (kind === "turn.step") {
      const id = String(d.id || ""), label = String(d.label || "");
      state.lastStep = label || id;
      if (/^(recall|memory)/.test(id)) { pulse("memory"); sticks.forEach((s) => (s.hot = Math.max(s.hot, 0.6))); }
      else if (/browser|chrome|navigate|snapshot/i.test(id + label)) pulse("browser");
      if (/^(tool|adapter|grok|step)/.test(id)) { pulse("act"); state.lastTool = label || id; state.toolUntil = performance.now() + 4000; gpuLed.material.emissive.setHex(0xf0b429); setTimeout(() => { gpuLed.material.emissive.setHex(0x000000); wake(); }, 1600); }
      else if (/^(route|judge|gate|confirm|parse)/.test(id)) pulse("gates");
      else if (/vault/i.test(id + label)) { pulse("vault"); vaultLed.material.emissive.setHex(0xf0b429); setTimeout(() => { vaultLed.material.emissive.setHex(0x000000); wake(); }, 1600); }
      if (d.state === "fail") { pulse("act", 0xff4d5e); state.error = 1; }
    }
  }
  addEventListener("jarvis:overview", (ev) => setOverview(ev.detail));
  addEventListener("jarvis:live", (ev) => onLive(ev.detail));
  loadOverview();  // first paint without waiting for the next refresh

  // ---------- what each part is, and what it does in JARVIS ----------
  const ROLE = {
    voice: "Speaks: voice.py and speech.py read the answer back aloud once a turn is done.",
    cluster: "Links your computers: the main runs the brain, memory and the hub; nodes long-poll it over an SSH tunnel and refuse code that doesn't match.",
    lanmouse: "Shared input: Lan Mouse moves one mouse and keyboard across both laptops. The guard switches it off when they're apart and back on when together.",
    chrome: "Web hands: browser_proxy.py sits between the brain and Playwright; browser_gate.py decides which clicks have to ask you first.",
    sandbox: "Terminal safety: every command JARVIS runs on Linux goes through a bubblewrap sandbox that hides secrets, browser profiles and .jarvis/.",
    log: "Records: every turn and step lands in history and the event stream, which feed the trace, the history checks and this board.",
  };
  const CHIP_STATION = { voice: "speak", cluster: "machines", lanmouse: "machines", chrome: "act", sandbox: "act", log: "log" };
  const parts = [
    { name: "brain", title: "CPU · the brain", station: "think", obj: [coolerFins, fan.group, brainBadge], href: "design.html",
      role: "Thinks when the free parser can't: ChatGPT through the Codex subscription, failing over to the Claude CLI on usage limits. brain.py plans multi-job requests across L1–L3.",
      info: () => [["model", state.brain], ["state", state.thinking ? "thinking" : state.warm ? "warm" : "idle"],
        ...rows("brain").filter(([k]) => /^L/.test(k)).slice(0, 3), ["last step", state.lastStep]] },
    ...sticks.map((st, i) => ({ name: "ram" + i, title: `RAM ${["A1", "A2", "B1", "B2"][i]} · memory`, station: "remember", obj: [st.group], href: "#memory",
      role: "Remembers: embed.py + memory.py search past items with BM25 and vectors fused by RRF. One stick per kind of memory; the LED bar fills with its item count.",
      info: () => [["kind", st.kind || "empty slot"], ["items", st.count.toLocaleString()], ["all memory", `${state.items} items · ${state.links} links`]] })),
    { name: "vault", title: "M.2 drive · the vault", station: "remember", obj: [vaultLbl], href: "knowledge.html",
      role: "Your Obsidian vault, indexed so answers can be grounded in your notes. Read-only: writes are additive and only when you ask.",
      info: () => rows("memory").filter(([k]) => /vault/.test(k)) },
    { name: "act", title: "graphics card · actions", station: "act", obj: [gpu, ...gpuFans.map((f) => f.group), actLbl], href: "design.html",
      role: "Does things: the direct executor (remote.call) and desktop control (control.py), which sees the screen and drives mouse and keyboard. It pauses when you move the mouse.",
      info: () => [...rows("control").slice(0, 3), ["last tool", state.lastTool]] },
    { name: "gates", title: "chipset · router + gates", station: "decide", obj: [gatesLbl], href: "design.html",
      role: "Decides: strict parser → intent registry → gates → history checks. Risky actions wait in the confirm queue; hard_deny fails closed on anything it can't read.",
      info: () => rows("gates").slice(0, 3) },
    { name: "io", title: "I/O shield · ears", station: "hear", obj: [ioShield], href: "design.html",
      role: "Hears: the wake-word listener (listen.py) catches “Jarvis, …” from the mic and hands the words to intake. Its LED is lit while listening.",
      info: () => [["listening", state.listening ? "yes" : "no"], ["wake word", "Jarvis"]] },
    ...CHIPS.map((c) => ({ name: c.id, title: `${c.part} · ${c.name.toLowerCase()}`, station: CHIP_STATION[c.id], obj: [c.body, c.lbl], href: "design.html",
      role: ROLE[c.id],
      info: () => c.id === "voice" ? [["listening", state.listening ? "yes" : "no"]]
        : c.id === "log" ? [["events seen", state.events.toLocaleString()], ["last step", state.lastStep]] : rows(c.id).slice(0, 3) })),
  ];
  // under the hood, and the empty skills slot: parts with no station, each tied to a page section
  parts.push(
    { name: "power", title: "VRM · power", section: "#code-h", obj: [vrmTop, ...grooves], href: "design.html",
      role: "Keeps JARVIS running: the daemon (daemon.py) hosts the listener and the cluster hub; autostart.py starts it at login (a systemd user unit here, a LaunchAgent on macOS, a Startup .vbs on Windows).",
      info: () => [["brain", state.warm ? "warm" : "cold"], ["machines", String(state.nodes)]] },
    { name: "config", title: "VRM · config", section: "#code-h", obj: [vrmLeft], href: "design.html",
      role: "Settings: jarvis.config.json holds the rules (confirm, always_ask), cluster roles, the vault path and the model lanes; settings.py reads it.",
      info: () => rows("gates").slice(0, 2) },
    { name: "local", title: "small chips · local models", section: "#code-h", obj: localChips, href: "design.html",
      role: "Runs in-process on the CPU, free: the local decider (decide.py, MiniLM, in shadow mode) and the embedder behind semantic memory.",
      info: () => [["cost", "free · no quota"], ["memory items", state.items.toLocaleString()]] },
    { name: "stores", title: "SATA ports · data stores", section: "#data-h", obj: sata, href: "design.html",
      role: "Where your data lives: memory.sqlite3, history.sqlite3, the vault index and the logs, all in .jarvis/ on this machine. Nothing is uploaded.",
      info: () => rows("memory").slice(0, 3) },
    { name: "skills", title: "empty slot · skills", section: "#skills-h", obj: [skillCard, skillLbl, skillSlot], href: "design.html",
      role: "Coming soon: skills you add to JARVIS. Each one plugs in here: the words it answers to, what it does, and what it has to ask you first.",
      info: () => [["status", "coming soon"], ["slots free", "1"]] },
  );
  const byName = (n) => parts.find((p) => p.name === n);
  const hitObjs = [], owner = new Map();
  for (const p of parts) { p.meshes = []; for (const o of p.obj) o.traverse((m) => { if (m.isMesh || m.isInstancedMesh) { hitObjs.push(m); owner.set(m, p); p.meshes.push(m); } }); }
  // small or thin parts get invisible hit boxes, so they are easy to point at
  const hitBox = (p, w, d, x, z) => { const m = new THREE.Mesh(new THREE.BoxGeometry(w, 0.7, d), new THREE.MeshBasicMaterial({ visible: false })); m.position.set(x, 0.3, z); board.add(m); hitObjs.push(m); owner.set(m, p); p.meshes.push(m); };
  hitBox(byName("vault"), 2.8, 0.8, VAULT.x, VAULT.z);
  hitBox(byName("gates"), 2.4, 1.9, GATES.x, GATES.z);
  sticks.forEach((st, i) => hitBox(byName("ram" + i), 0.5, 6.0, st.group.position.x, st.group.position.z));

  // ---------- highlight: an amber outline round the part, and its metal warms up ----------
  const AMBER = 0xf0b429, shared = new Set(Object.values(M)), tintCache = new Map();
  const tinted = (mat) => {
    if (!tintCache.has(mat)) { const m = mat.clone(); m.emissive = new THREE.Color(AMBER); m.emissiveIntensity = 0.16; tintCache.set(mat, m); }
    return tintCache.get(mat);
  };
  const hiBox = new THREE.Box3Helper(new THREE.Box3(), AMBER);
  hiBox.material.depthTest = false; hiBox.material.transparent = true; hiBox.material.opacity = 0.85; hiBox.renderOrder = 10;
  hiBox.visible = false; scene.add(hiBox);
  let lit = [];
  function highlight(list) {
    for (const [m, mat] of lit) m.material = mat;
    lit = [];
    if (!list.length) { hiBox.visible = false; return; }
    const b = new THREE.Box3();
    for (const p of list) for (const m of p.meshes) {
      b.expandByObject(m);
      if (shared.has(m.material)) { lit.push([m, m.material]); m.material = tinted(m.material); }
    }
    hiBox.box.copy(b).expandByScalar(0.05); hiBox.visible = true;
  }

  // ---------- callout: a leader line from the part to a card saying what it does ----------
  const SVGNS = "http://www.w3.org/2000/svg", E = (v) => (window.JarvisEsc ? window.JarvisEsc(v) : String(v));
  const lines = document.createElementNS(SVGNS, "svg");
  lines.setAttribute("class", "board-lines"); lines.setAttribute("aria-hidden", "true");
  host.insertBefore(lines, tip);
  let focus = null, hover = null, zoomed = null, tipW = 0, tipH = 0, calloutSig = "";
  const partFocus = (p) => ({ parts: [p], title: p.title, role: p.role, rows: p.info() });
  function setFocus(f) {
    focus = f;
    highlight(f ? f.parts : []);
    if (f) tip.innerHTML = `<h4>${E(f.title)}</h4><p>${E(f.role)}</p>` +
      f.rows.map(([k, v]) => `<div><span>${E(k)}</span><b>${E(v)}</b></div>`).join("");
    tip.hidden = !f;
    tip.classList.toggle("is-route", !!(f && f.route));
    if (f) { tipW = tip.offsetWidth; tipH = tip.offsetHeight; calloutSig = ""; }
    else if (calloutSig) { calloutSig = ""; lines.innerHTML = ""; }
    wake(400);
  }
  const v3 = new THREE.Vector3();
  function drawCallout() {
    if (!focus || !hiBox.visible) {
      if (calloutSig) { calloutSig = ""; lines.innerHTML = ""; }
      return;
    }
    const w = viewW, h = viewH;
    hiBox.box.getCenter(v3); v3.y = hiBox.box.max.y; v3.project(camera);
    const ax = (v3.x + 1) / 2 * w, ay = (1 - v3.y) / 2 * h;
    // Half-pixel signature: a settled camera does not rebuild the SVG every frame.
    const sig = (ax * 2 | 0) + "," + (ay * 2 | 0) + "," + w + "," + h + "," + tipW + "," + tipH;
    if (sig === calloutSig) return;
    calloutSig = sig;
    const cw = tipW || 260, ch = tipH || 80, pad = 12;
    let cx, cy, ex, ey, pts;
    if (w < cw * 2 + 140) {  // narrow: card above or below, line drops to it
      cx = Math.max(pad, Math.min(w - cw - pad, ax - cw / 2));
      cy = ay > h / 2 ? pad : h - ch - pad;
      ex = Math.max(cx + 12, Math.min(cx + cw - 12, ax)); ey = ay > h / 2 ? cy + ch : cy;
      pts = [[ax, ay], [ax, (ay + ey) / 2], [ex, (ay + ey) / 2], [ex, ey]];
    } else {  // wide: card on the near side, a 45° run then a straight lead into it
      const left = ax < w / 2;
      cx = left ? pad : w - cw - pad;
      cy = Math.max(pad, Math.min(h - ch - pad, ay - ch / 2 - 40));
      ex = left ? cx + cw : cx; ey = Math.max(cy + 14, Math.min(cy + ch - 14, ay - 40));
      const run = Math.min(Math.abs(ay - ey), Math.abs(ax - ex) * 0.6);
      const mx = ax + (left ? -run : run);
      pts = [[ax, ay], [mx, ay - Math.sign(ay - ey) * run], [ex, ey]];
    }
    tip.style.left = cx + "px"; tip.style.top = cy + "px";
    lines.setAttribute("viewBox", `0 0 ${w} ${h}`);
    lines.innerHTML = `<polyline points="${pts.map((p) => p.map((n) => n.toFixed(1)).join(",")).join(" ")}"/>` +
      `<circle class="a" cx="${ax.toFixed(1)}" cy="${ay.toFixed(1)}" r="4"/><circle cx="${ex.toFixed(1)}" cy="${ey.toFixed(1)}" r="2.5"/>`;
  }

  // ---------- hover (the camera stays put: pointing only picks parts) ----------
  const ray = new THREE.Raycaster(), ptr = new THREE.Vector2(-9, -9);
  function hoverAt(x, y) {  // host-local CSS px; size cached by ResizeObserver
    ptr.set((x / viewW) * 2 - 1, -(y / viewH) * 2 + 1);
    ray.setFromCamera(ptr, camera);
    const hit = ray.intersectObjects(hitObjs, false)[0];
    const p = hit ? owner.get(hit.object) : null;
    if (p !== hover) {
      hover = p; canvas.style.cursor = p && p.href ? "pointer" : "";
      setFocus(p ? partFocus(p) : held());
      dispatchEvent(new CustomEvent("jarvis:board-part", { detail: { station: p ? p.station : null, name: p ? p.name : null } }));
    }
  }
  function leave() {
    if (!hover) return; hover = null; canvas.style.cursor = ""; setFocus(held());
    dispatchEvent(new CustomEvent("jarvis:board-part", { detail: { station: null } }));
  }
  // what the callout falls back to when the pointer leaves the board: a station the page is
  // pointing at, else the route in flight
  let stationHeld = null;
  const held = () => (zoomed && partFocus(zoomed)) || stationHeld || (route && route.focus) || null;
  addEventListener("jarvis:station", (ev) => {
    const d = ev.detail || {}, list = (STATION_PARTS[d.id] || []).map(byName);
    stationHeld = d.id && list.length ? { station: true, parts: list, title: `${d.title || d.id} · ${d.hw || list[0].title}`,
      role: d.plain || list[0].role, rows: d.rows || [] } : null;
    if (!hover) setFocus(held());
  });
  canvas.addEventListener("pointermove", (e) => { hoverAt(e.offsetX, e.offsetY); });
  canvas.addEventListener("pointerleave", leave);

  // ---------- keyboard: Tab to the board, arrows walk the parts in reading order, Enter zooms ----------
  {
    canvas.tabIndex = 0;
    canvas.setAttribute("aria-label", "Board parts. Arrow keys move between parts, Enter zooms in, Escape zooms out.");
    const said = document.createElement("p"); said.className = "sr-only"; said.setAttribute("aria-live", "polite"); host.appendChild(said);
    const at = (p) => { const b = new THREE.Box3(); for (const m of p.meshes) b.expandByObject(m); return b.getCenter(new THREE.Vector3()); };
    const order = parts.map((p) => ({ p, c: at(p) }))   // rows of about 1.5 units, left to right inside a row
      .sort((a, b) => Math.round(a.c.z / 1.5) - Math.round(b.c.z / 1.5) || a.c.x - b.c.x).map((o) => o.p);
    let ki = -1;
    canvas.addEventListener("keydown", (e) => {
      const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
      if (step) {
        e.preventDefault();
        ki = ((ki < 0 && step < 0 ? 0 : ki) + step + order.length) % order.length;
        const p = order[ki];
        hover = p; setFocus(partFocus(p));
        said.textContent = `${p.title}. ${p.role}`;
        dispatchEvent(new CustomEvent("jarvis:board-part", { detail: { station: p.station || null, name: p.name } }));
      } else if ((e.key === "Enter" || e.key === " ") && hover) {
        e.preventDefault(); zoomTo(zoomed === hover ? null : hover);
        said.textContent = zoomed ? `Zoomed in on ${zoomed.title}.` : "Whole board.";
      }
    });
    canvas.addEventListener("blur", () => { ki = -1; leave(); });
  }

  // ---------- zoom: click a part and the camera glides in to it ----------
  const bar = host.querySelector(".board-zoom");
  function zoomTo(p) {
    zoomed = p;
    if (p) {
      const b = new THREE.Box3(); for (const m of p.meshes) b.expandByObject(m);
      const c = b.getCenter(new THREE.Vector3()), sz = b.getSize(new THREE.Vector3());
      const t = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
      const span = Math.max(sz.z * 0.62, sz.x * 0.62 / camera.aspect, sz.y * 0.5, 0.9);
      camGoal = { x: c.x, y: c.y * 0.5, z: c.z, d: Math.max(3.6, (span * 1.35) / t + sz.y), el: 0.98 };
    } else camGoal = null;
    if (bar) {
      bar.hidden = !p;
      if (p) bar.querySelector("b").textContent = p.title;
    }
    host.classList.toggle("is-zoomed", !!p);
    if (tip) tip.classList.toggle("is-zoomed", !!p);
    if (!hover) setFocus(held()); else if (p) setFocus(partFocus(p));
    dispatchEvent(new CustomEvent("jarvis:board-zoom", { detail: { name: p ? p.name : null, station: p ? p.station : null } }));
    wake(1500);
  }
  if (bar) bar.addEventListener("click", (e) => {
    const b = e.target.closest("button"); if (!b) return;
    if (b.dataset.z === "out") zoomTo(null);
    else if (b.dataset.z === "more" && zoomed) {
      if (zoomed.station) dispatchEvent(new CustomEvent("jarvis:open-station", { detail: { station: zoomed.station } }));
      else document.querySelector(zoomed.section)?.scrollIntoView({ behavior: reduced ? "auto" : "smooth", block: "start" });
    }
  });
  addEventListener("keydown", (e) => { if (e.key === "Escape" && zoomed) zoomTo(null); });
  // the page can point at and zoom to parts too (the rail's "under the hood" and "skills" rows)
  addEventListener("jarvis:part", (ev) => {
    const d = ev.detail || {}, p = d.name && byName(d.name);
    if (d.zoom) { zoomTo(p && zoomed !== p ? p : null); return; }
    stationHeld = p ? partFocus(p) : null;
    if (!hover) setFocus(held());
  });
  addEventListener("jarvis:zoom-station", (ev) => {
    const id = ev.detail && ev.detail.id, list = (STATION_PARTS[id] || []);
    const p = list.length ? byName(STATION_ANCHOR[id] || list[0]) : null;
    if (p) zoomTo(zoomed === p ? null : p);
  });

  // ---------- diagnostics: a scan line sweeps the board and each checked part lights its result ----------
  const DIAG = { ok: 0x7fdc9a, warn: 0xf0b429, fail: 0xff4d5e };
  let diag = null;
  const scanBar = new THREE.Mesh(new THREE.BoxGeometry(BW + 0.4, 0.03, 0.05),
    new THREE.MeshBasicMaterial({ color: 0xdfe9f5, transparent: true, opacity: 0, depthTest: false }));
  scanBar.renderOrder = 12; scanBar.visible = false; board.add(scanBar);
  function clearDiag() {
    clearHold();
    if (!diag) return;
    for (const r of diag.results) if (r.helper) { scene.remove(r.helper); r.helper.geometry.dispose(); r.helper.material.dispose(); }
    diag = null; scanBar.visible = false;
  }
  addEventListener("jarvis:diagnose", (ev) => {
    clearDiag();
    const results = ((ev.detail && ev.detail.results) || []).map((r) => {
      const p = byName(r.name); if (!p) return null;
      const b = new THREE.Box3(); for (const m of p.meshes) b.expandByObject(m);
      return { ...r, part: p, box: b.expandByScalar(0.04), z: b.getCenter(new THREE.Vector3()).z, helper: null };
    }).filter(Boolean);
    diag = { results, total: (ev.detail && ev.detail.total) || results.length, t: 0, hold: 0, done: false };
    scanBar.visible = true; wake(1000);
  });
  // true while the scan or the fade is on screen. The 7s hold is a still frame: a timer
  // resumes the loop when the outlines should fade, instead of redrawing the whole time.
  function stepDiag(dt) {
    if (!diag) return false;
    const SWEEP = reduced ? 0.01 : 2.2;
    if (diag.t < 1) {
      diag.t = Math.min(1, diag.t + dt / SWEEP);
      const z = -BD / 2 + diag.t * BD;
      scanBar.position.set(0, 0.35, z); scanBar.material.opacity = Math.sin(diag.t * Math.PI) * 0.9;
      for (const r of diag.results) if (!r.helper && r.z <= z) {  // the line has reached it: show its result
        r.helper = new THREE.Box3Helper(r.box, DIAG[r.state] || DIAG.ok);
        r.helper.material.depthTest = false; r.helper.material.transparent = true; r.helper.renderOrder = 10; scene.add(r.helper);
      }
      return true;
    }
    if (!diag.done) {
      diag.done = true; scanBar.visible = false;
      // end on the worst part, with what's wrong and the fix; or a clean bill of health
      const bad = diag.results.filter((r) => r.state !== "ok").sort((a, b) => (b.state === "fail") - (a.state === "fail"));
      const top = bad[0];
      // one memory warning lights five parts (vault + four sticks): count issues, not parts
      const issues = [...new Set(bad.flatMap((r) => r.problems.map((p) => p.label)))];
      const short = (r) => r.part.title.split(" · ").pop();
      const others = [...new Set(bad.map(short))].filter((x) => x !== (top && short(top)));
      const f = top ? { route: true, parts: [top.part], title: `Diagnostics · ${top.part.title}`,
          role: `${issues.length} issue${issues.length === 1 ? "" : "s"} found. ${top.problems[0] ? top.problems[0].label + "." : ""}`,
          rows: [["result", top.state === "fail" ? "broken" : "to improve"], ...top.problems.slice(0, 2).map((p) => ["fix", p.fix || "—"]),
                 ["also", others.join(", ") || "none"]] }
        : { route: true, parts: [byName("brain")], title: "Diagnostics · all clear",
          role: `Every part that has a check passed: ${diag.results.length} parts, ${diag.total} checks read live.`, rows: [["result", "ok"]] };
      diag.focus = f;
      if (!hover && !zoomed) setFocus(f);
      armHold(7000);
      return false;
    }
    if (diag.hold < 7) { armHold((7 - diag.hold) * 1000); return false; }
    diag.hold += dt;
    const fade = Math.max(0, 1 - (diag.hold - 7) / 1.5);
    for (const r of diag.results) if (r.helper) r.helper.material.opacity = fade;
    if (fade <= 0) { const mine = focus === diag.focus; clearDiag(); if (mine && !hover) setFocus(held()); return false; }
    return true;
  }

  // ---------- logic: a request's real route, station by station, along Manhattan traces ----------
  // The stations are the ones the server's router uses (/api/system/design paths); each maps to
  // the parts that do that job on the board.
  const STATION_PARTS = { hear: ["io"], decide: ["gates"], remember: ["ram0", "ram1", "ram2", "ram3", "vault"], think: ["brain"],
                          act: ["act", "chrome"], speak: ["voice"], log: ["log"], machines: ["cluster"] };
  const STATION_ANCHOR = { remember: "ram1", act: "act" };
  let route = null;
  function anchorOf(st) {
    const b = new THREE.Box3();
    for (const m of byName(STATION_ANCHOR[st] || STATION_PARTS[st][0]).meshes) b.expandByObject(m);
    const c = b.getCenter(new THREE.Vector3()); c.y = 0.13; return c;
  }
  function clearRoute() {
    if (!route) return;
    board.remove(route.line, route.dot); route.line.geometry.dispose(); route.line.material.dispose(); route.dot.material.dispose();
    route = null;
  }
  function runRoute(stations, tier) {
    clearRoute();
    const stops = (stations || []).filter((s) => STATION_PARTS[s]);
    if (!stops.length) return;
    const curve = new THREE.CurvePath(), at = stops.map(anchorOf);
    for (let i = 1; i < at.length; i++) {  // along x first, then z: circuit-style right angles
      const a = at[i - 1], b = at[i], corner = new THREE.Vector3(b.x, 0.13, a.z);
      curve.add(new THREE.LineCurve3(a, corner)); curve.add(new THREE.LineCurve3(corner, b));
    }
    const N = 240, total = at.length > 1 ? curve.getLength() : 0, lens = at.length > 1 ? curve.getCurveLengths() : [];
    const line = new THREE.Line(new THREE.BufferGeometry().setFromPoints(at.length > 1 ? curve.getSpacedPoints(N) : [at[0], at[0]]),
      new THREE.LineBasicMaterial({ color: AMBER, transparent: true, opacity: 0.95, depthTest: false }));
    line.renderOrder = 9; line.geometry.setDrawRange(0, 0);
    const dot = new THREE.Sprite(pulseMat(AMBER)); dot.scale.set(0.9, 0.9, 0.9); dot.renderOrder = 11; dot.material.depthTest = false;
    board.add(line, dot);
    route = { curve, line, dot, stops, tier, at: -1, t: 0, hold: 0, anchor: at[0],
      stopT: stops.map((_, k) => (k === 0 || !total ? 0 : lens[2 * k - 1] / total)),
      dur: Math.max(1.2, 0.75 * (stops.length - 1)), focus: null };
    wake(1000);
  }
  const routePos = new THREE.Vector3();
  function routeFocus(k) {
    const st = route.stops[k], list = STATION_PARTS[st].map(byName);
    return { route: true, parts: list, title: `${String(k + 1).padStart(2, "0")} / ${String(route.stops.length).padStart(2, "0")} · ${st.toUpperCase()}`,
      role: list[0].role, rows: [["part", list[0].title], ["tier", route.tier || "—"], ["next", route.stops[k + 1] || "done"]] };
  }
  function stepRoute(dt) {
    if (!route) return false;
    if (route.t < 1) {
      route.t = Math.min(1, route.t + dt / route.dur);
      // equal time per station, however long the trace between them
      const legs = route.stops.length - 1, u = route.t * legs, k = Math.min(Math.floor(u), Math.max(0, legs - 1)) + (route.t >= 1 ? 1 : 0);
      const a = route.stopT[Math.min(Math.floor(u), legs)] || 0, b = route.stopT[Math.min(Math.floor(u) + 1, legs)] ?? 1;
      const along = legs ? Math.min(1, a + (b - a) * (u - Math.floor(u))) : 0;
      const pos = legs ? route.curve.getPointAt(route.t >= 1 ? 1 : along, routePos) : route.anchor;
      route.dot.position.set(pos.x, 0.2, pos.z);
      route.line.geometry.setDrawRange(0, Math.ceil((route.t >= 1 ? 1 : along) * 241));
      if (k !== route.at) {
        route.at = k; route.focus = routeFocus(k); if (!hover && !stationHeld) setFocus(route.focus);
        dispatchEvent(new CustomEvent("jarvis:board-stop", { detail: { station: route.stops[k] } }));
      }
    } else {
      route.hold += dt;
      route.dot.material.opacity = Math.max(0, 1 - route.hold);
      if (route.hold > 3) route.line.material.opacity = Math.max(0, 0.95 - (route.hold - 3) * 0.6);
      if (route.hold > 4.6) {
        const mine = focus && focus.route; clearRoute(); if (mine && !hover) setFocus(held());
        dispatchEvent(new CustomEvent("jarvis:board-stop", { detail: { station: null } }));
        return false;
      }
    }
    return true;
  }
  addEventListener("jarvis:route", (ev) => runRoute(ev.detail && ev.detail.path, ev.detail && ev.detail.tier));
  canvas.addEventListener("click", () => { if (hover) zoomTo(hover); else if (zoomed) zoomTo(null); });
  // ---------- size, camera, loop (rAF only while the picture is changing) ----------
  // dpr 2 through a 1440p buffer; larger stages drop toward 1 so a 4K canvas is not a 33MP render.
  function cappedDpr(w, h) {
    const cap = 2560 * 1440 * 4;
    let dpr = Math.min(devicePixelRatio || 1, 2);
    if (w * h * dpr * dpr > cap) dpr = Math.min(dpr, Math.sqrt(cap / (w * h)));
    return Math.max(1, Math.min(2, dpr));
  }
  let viewW = 1, viewH = 1, laidOut = false, usedDpr = 0;
  let visible = true, last = 0, raf = 0, holdTimer = 0;
  let resizeObs = null, ioObs = null, events = null;
  // One entry for every mutation (wake and invalidate are the same function).
  // Schedules a frame when the board is on screen; that frame schedules the next
  // only while step() reports the picture is still moving.
  function invalidate() { if (!raf && shown()) raf = requestAnimationFrame(loop); }
  function wake() { invalidate(); }
  function clearHold() { if (holdTimer) { clearTimeout(holdTimer); holdTimer = 0; } }
  function armHold(ms) {
    if (holdTimer) return;
    holdTimer = setTimeout(() => {
      holdTimer = 0;
      if (!diag || !diag.done) return;
      diag.hold = Math.max(diag.hold, 7);
      wake();
    }, Math.max(0, ms));
  }
  function size() {
    const cw = host.clientWidth, ch = host.clientHeight;
    laidOut = cw > 0 && ch > 0;
    if (!laidOut) { if (raf) { cancelAnimationFrame(raf); raf = 0; } return; }
    viewW = cw; viewH = ch;
    const dpr = cappedDpr(cw, ch);
    if (dpr !== usedDpr) { usedDpr = dpr; renderer.setPixelRatio(dpr); }
    renderer.setSize(cw, ch, false); camera.aspect = cw / ch;
    // fit the whole board in its slot, both ways
    const t = Math.tan(THREE.MathUtils.degToRad(camera.fov / 2));
    camera.userData.dist = Math.max((BD * 0.56) / t, (BW * 0.6) / (t * camera.aspect));
    camera.clearViewOffset();
    camera.updateProjectionMatrix();
    if (focus && !tip.hidden) { tipW = tip.offsetWidth; tipH = tip.offsetHeight; }
    calloutSig = "";
    wake(300);
  }
  const shown = () => visible && !document.hidden && laidOut;
  function onIntersect(es) {
    visible = es.some((x) => x.isIntersecting);
    if (visible) wake(800); else if (raf) { cancelAnimationFrame(raf); raf = 0; }
  }
  function onVis() {
    if (document.hidden) { if (raf) { cancelAnimationFrame(raf); raf = 0; } return; }
    loadOverview(); loadCards(); wake(800);
  }
  document.addEventListener("visibilitychange", onVis);
  const reduceMq = matchMedia("(prefers-reduced-motion: reduce)");
  reduceMq.addEventListener?.("change", () => { reduced = reduceMq.matches; wake(); });
  new MutationObserver(() => invalidate()).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
  matchMedia("(prefers-color-scheme: dark)").addEventListener?.("change", () => invalidate());
  function arm() {
    if (!resizeObs) { resizeObs = new ResizeObserver(size); resizeObs.observe(host); }
    if (!ioObs) { ioObs = new IntersectionObserver(onIntersect); ioObs.observe(host); }
    if (!cardsTimer) cardsTimer = setInterval(() => { if (!document.hidden) loadCards(); }, 30000);
    if (!overviewTimer) overviewTimer = setInterval(() => { if (!document.hidden) loadOverview(); }, 20000);
    if (window.EventSource && !events) {
      events = new EventSource("/api/events");
      events.addEventListener("message", (ev) => { let e; try { e = JSON.parse(ev.data); } catch (x) { return; } onLive(e); });
    }
  }
  function disarm() {
    if (raf) cancelAnimationFrame(raf);
    raf = 0;
    clearHold();
    if (resizeObs) { resizeObs.disconnect(); resizeObs = null; }
    if (ioObs) { ioObs.disconnect(); ioObs = null; }
    if (cardsTimer) { clearInterval(cardsTimer); cardsTimer = 0; }
    if (overviewTimer) { clearInterval(overviewTimer); overviewTimer = 0; }
    if (events) { events.close(); events = null; }
  }
  addEventListener("pagehide", disarm);
  addEventListener("pageshow", (ev) => { if (!ev.persisted) return; arm(); if (!document.hidden) onVis(); });
  let shadowsBaked = false, routeDt = 0.016;
  const cam = { x: null, y: null, z: null, d: null, el: null };
  const CAM_KEYS = ["x", "y", "z", "d", "el"];
  const homeGoal = { x: 0, y: 0, z: 0.45, d: 30, el: 1.12 };
  let camGoal = null;
  function loop(now) {
    raf = 0;
    if (!shown()) return;  // hidden tab, off-screen, or a 0×0 host: stop entirely
    const raw = (now - last) / 1000 || 0.016, dt = Math.min(0.05, raw); last = now;
    routeDt = Math.min(0.25, raw);  // the route keeps real time even when frames are slow
    const again = step(dt, now);
    renderer.render(scene, camera); drawCallout();
    if (!shadowsBaked) { renderer.shadowMap.needsUpdate = true; shadowsBaked = true; renderer.render(scene, camera); }
    if (again && shown() && !raf) raf = requestAnimationFrame(loop);
  }
  function step(dt, now) {
    // A fixed, high, slightly tilted studio angle: the board never follows the mouse. Clicking a
    // part glides the camera in to it (camGoal); the whole board is the default goal.
    if (!camGoal) homeGoal.d = camera.userData.dist || 30;
    const goal = camGoal || homeGoal;
    const k = reduced ? 1 : 1 - Math.exp(-dt * 4.5);
    let drifting = false;
    for (const key of CAM_KEYS) {
      cam[key] = cam[key] == null ? goal[key] : cam[key] + (goal[key] - cam[key]) * k;
      if (Math.abs(goal[key] - cam[key]) > 1e-3) drifting = true;
    }
    const az = -0.08, d = cam.d, el = cam.el;
    camera.position.set(cam.x + Math.sin(az) * Math.cos(el) * d, cam.y + Math.sin(el) * d, cam.z + Math.cos(az) * Math.cos(el) * d);
    camera.lookAt(cam.x, cam.y, cam.z);
    // Powered off while JARVIS is idle; it spins up while working and winds down after.
    const working = state.thinking || now < state.busyUntil;
    const toolsOn = now < (state.toolUntil || 0);
    const ease = (f, target, up, down) => { f.speed += (target - f.speed) * Math.min(1, (target > f.speed ? up : down) * dt); };
    const fanTarget = reduced ? 0 : working ? 16 : 0;
    ease(fan, fanTarget, 1.6, 0.55);
    if (fanTarget === 0 && fan.speed < 0.05) fan.speed = 0;
    fan.rotor.rotation.y -= fan.speed * dt;
    fan.ringMat.emissiveIntensity = Math.min(1.6, fan.speed / 10);
    let fans = fanTarget > 0 || fan.speed > 0;
    gpuFans.forEach((f, i) => {
      const target = reduced ? 0 : toolsOn ? 13 : working ? 3 : 0;
      ease(f, target, 1.4, 0.5);
      if (target === 0 && f.speed < 0.05) f.speed = 0;
      f.rotor.rotation.y += f.speed * dt * (i ? 1 : -1);
      f.ringMat.emissiveIntensity = Math.min(1.4, f.speed / 9);
      if (target > 0 || f.speed > 0) fans = true;
    });
    let hot = false;
    for (const st of sticks) {
      if (st.hot > 0.01 && st.kind) {
        st.hot *= 0.96; st.bar.material.emissive.setHex(st.hot > 0.4 ? 0xf0b429 : 0xdfe9f5);
        if (st.hot > 0.01) hot = true;
      }
    }
    for (let i = pulses.length - 1; i >= 0; i--) {
      const p = pulses[i]; p.t += dt * p.speed;
      if (p.t >= 1) { board.remove(p.s); p.s.material.dispose(); pulses.splice(i, 1); continue; }
      curves[p.path].getPointAt(p.t, pulsePos); p.s.position.set(pulsePos.x, 0.18, pulsePos.z);
      p.s.material.opacity = Math.sin(p.t * Math.PI);
    }
    const routeLive = stepRoute(routeDt), diagLive = stepDiag(routeDt);
    if (state.error) { state.error *= 0.97; brainBadge.material.color.setRGB(1, 1 - state.error * 0.7, 1 - state.error * 0.7); if (state.error < 0.02) { state.error = 0; brainBadge.material.color.setRGB(1, 1, 1); } }
    return drifting || fans || hot || pulses.length > 0 || routeLive || diagLive || state.error > 0;
  }
  arm(); size(); wake();
  document.fonts?.ready.then(() => { calloutSig = ""; wake(); });
}
