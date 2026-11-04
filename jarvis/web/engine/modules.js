/* The console engine's modules. Each one is a self-contained bolt-on: it builds its own parts,
   animates them from the shared engine state and answers hover with live rows.

     { id, no, name, role,       numbered nameplate: "04 TURBO · brain"
       href,                     where a click goes (optional)
       anchor: [x, y, z],        where its callout tag points, in engine units
       tag: [dx, dy],            which way the tag sits from the anchor, in screen px at home zoom
       explode: [x, y, z],       where the module slides to in the exploded view, in engine units
       status(s, ctx) -> "ok" | "warn" | "fail" | "active" | "idle" | "off"   the light on its tag
       build(k, root, ctx) -> { hit: [Object3D], update(dt, s, now), info() -> [[key, value]] } }

   k is the parts kit (engine/kit.js). ctx gives { rows(id), card(id), shared } where shared is how
   modules reach each other (the block publishes its cylinders there for the strands and firing).
   Engine units: crank along x (front pulleys at -3.15, flywheel at +3.05), y up, z towards you. */

export const STAGES = ["hear", "route", "recall", "gates", "brain", "act", "observe", "remember"];
const pct = (v) => `${Math.round(v * 100)}%`;
const LED = { ok: 0x7fdc9a, warn: 0xf0b429, fail: 0xff4d5e, off: 0x000000 };

export const MODULES = [
  // ---------- 01 intake: plenum, runners, throttle body and air filter = requests coming in ----------
  {
    id: "intake", no: 1, name: "INTAKE", role: "requests", anchor: [-2.9, 2.85, 0], tag: [-10, -46],
    explode: [0, 1.1, 0], status: (s, ctx) => s.intake > 0.1 || s.thinking ? "active" : s.listening ? "ok" : "idle",
    build(k, root) {
      const { THREE, M, add, box, cyl, tube } = k;
      const plenum = add(box(4.4, 0.5, 0.7), M.steel, root, [0, 2.55, 0]);
      const throttle = add(cyl(0.32, 0.32, 0.4, 28), M.bright, root, [-2.45, 2.55, 0], [0, 0, Math.PI / 2]);
      const filter = add(cyl(0.4, 0.4, 0.5, 28), M.dark, root, [-3.05, 2.55, 0], [0, 0, Math.PI / 2]);
      for (let p = 0; p < 7; p++) add(cyl(0.41, 0.41, 0.03, 28), M.steel, filter, [0, -0.2 + p * 0.066, 0]);
      const glow = add(new THREE.CircleGeometry(0.3, 24), k.glowMat(0xdfe9f5), root, [-3.31, 2.55, 0], [0, -Math.PI / 2, 0]);
      const runners = [];
      for (const side of [-1, 1]) for (const x of [-2.1, -0.7, 0.7, 2.1]) {
        const dir = new THREE.Vector3(0, Math.SQRT1_2, side * Math.SQRT1_2);
        const cx = x + side * 0.18, head = dir.clone().multiplyScalar(2.2).add(new THREE.Vector3(cx + side * 0.18, 0, 0));
        runners.push(add(tube([[cx * 0.95, 2.45, 0], [cx, 2.6, side * 0.75], head], 0.09, 16), M.steel, root));
      }
      return {
        hit: [plenum, throttle, filter, ...runners],
        update(dt, s) { s.intake *= Math.pow(0.3, dt); glow.material.opacity = s.intake * 0.85; },
        info: (s) => [["voice", s.listening ? "listening" : "off"], ["now", s.thinking ? "a request is in" : "nothing coming in"], ["requests", String(s.turns)]],
      };
    },
  },

  // ---------- 02 block: the V8, eight cylinders = the eight pipeline stages ----------
  {
    id: "block", no: 2, name: "BLOCK", role: "pipeline", anchor: [0.7, 1.55, 1.75], tag: [44, -18],
    explode: [0, 0, 0], status: (s, ctx) => s.error > 0.3 ? "fail" : s.rpm > 3 ? "active" : "ok",
    build(k, root, ctx) {
      const { THREE, M, add, box, cyl } = k;
      const BANK = THREE.MathUtils.degToRad(45), THROW = 0.34, ROD = 1.25, BORE = 0.4;
      const XS = [-2.1, -0.7, 0.7, 2.1];
      const banks = [
        { dir: new THREE.Vector3(0, Math.cos(BANK), -Math.sin(BANK)), side: -1, shift: -0.18 },
        { dir: new THREE.Vector3(0, Math.cos(BANK), Math.sin(BANK)), side: 1, shift: 0.18 },
      ];
      const FIRE = [0, 270, 180, 90, 90, 180, 270, 0].map((d) => THREE.MathUtils.degToRad(d));
      const crankcase = add(box(5.6, 1.1, 2.2), M.ghost, root, [0, -0.15, 0]);
      const cylinders = [], castings = [crankcase];
      banks.forEach((bank, b) => {
        const housing = k.group(root);
        housing.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), bank.dir);
        for (const z of [-0.52, 0.52]) castings.push(add(box(5.6, 1.6, 0.12), M.ghost, housing, [0, 1.25, z]));
        castings.push(add(box(5.7, 0.42, 1.24), M.ghost, housing, [0, 2.2, 0]));
        castings.push(add(box(5.5, 0.32, 1.0), M.ghostDark, housing, [0, 2.55, 0]));
        for (let r = 0; r < 9; r++) add(box(0.06, 0.06, 0.9), M.steel, housing, [-2.4 + r * 0.6, 2.73, 0]);
        XS.forEach((x0, j) => {
          const i = b * 4 + j, x = x0 + bank.shift;
          add(cyl(BORE + 0.04, BORE + 0.04, 1.5, 28, true), M.bore, housing, [x, 1.2, 0]);
          const flash = add(new THREE.CircleGeometry(BORE * 0.95, 28), k.glowMat(0xf0b429), housing, [x, 1.97, 0], [Math.PI / 2, 0, 0]);
          const tag = k.label(String(i + 1).padStart(2, "0") + " " + STAGES[i].toUpperCase(), 0.95, 0.2, { size: 0.6 });
          tag.rotation.x = -Math.PI / 2; tag.position.set(x, 2.72, 0.28); housing.add(tag);
          add(cyl(0.05, 0.05, 0.3, 10), M.bright, housing, [x, 2.78, -0.25]);
          cylinders.push({ i, x, bank, flash, heat: 0, piston: null, rod: null, phase: FIRE[i], tag });
        });
      });
      for (const c of cylinders) {
        c.piston = add(cyl(BORE, BORE, 0.42, 28), M.bright, root);
        for (let j = 0; j < 2; j++) add(cyl(BORE + 0.005, BORE + 0.005, 0.03, 28), M.dark, c.piston, [0, 0.12 - j * 0.08, 0]);
        c.rod = add(box(0.1, 1, 0.16), M.steel, root);
      }
      ctx.shared.cylinders = cylinders;
      ctx.shared.lastFired = 4;
      ctx.shared.fire = (i) => { if (cylinders[i]) { cylinders[i].heat = 1; ctx.shared.lastFired = i; } };
      const tmp = new THREE.Vector3(), pin = new THREE.Vector3(), top = new THREE.Vector3(), up = new THREE.Vector3(0, 1, 0);
      return {
        hit: [...castings, ...cylinders.flatMap((c) => [c.piston, c.tag])],
        update(dt, s) {
          for (const c of cylinders) {  // slider-crank: piston and rod from the crank angle
            const th = s.angle + c.phase, x = c.x + c.bank.shift;
            pin.set(x, THROW * Math.cos(th), THROW * Math.sin(th));
            const along = pin.y * c.bank.dir.y + pin.z * c.bank.dir.z;
            const off2 = pin.y * pin.y + pin.z * pin.z - along * along;
            const reach = along + Math.sqrt(Math.max(0, ROD * ROD - off2));
            top.copy(c.bank.dir).multiplyScalar(reach).add(tmp.set(x, 0, 0));
            c.piston.position.copy(c.bank.dir).multiplyScalar(reach + 0.21).add(tmp.set(x, 0, 0));
            c.piston.quaternion.setFromUnitVectors(up, c.bank.dir);
            c.rod.position.copy(pin).add(top).multiplyScalar(0.5);
            c.rod.scale.y = pin.distanceTo(top);
            c.rod.quaternion.setFromUnitVectors(up, tmp.copy(top).sub(pin).normalize());
            const tdc = Math.max(0, Math.cos(th - Math.atan2(c.bank.dir.z, c.bank.dir.y)));
            const combust = s.rpm > 40 ? Math.pow(tdc, 24) * Math.min(1, s.rpm / 1500) : 0;
            c.heat *= Math.pow(0.04, dt);
            c.flash.material.opacity = Math.min(1, combust * 0.6 + c.heat);
            c.flash.material.color.setHex(s.error > 0.3 ? 0xff4d5e : 0xf0b429);
          }
        },
        info: (s) => {
          const hot = cylinders.filter((c) => c.heat > 0.2).map((c) => STAGES[c.i]);
          return [["cylinders", "8 = 8 stages"], ["firing", hot.length ? hot.join(", ") : "none"], ["last step", s.lastStep]];
        },
      };
    },
  },

  // ---------- 03 crank + flywheel = the gates: everything turns through them ----------
  {
    id: "crank", no: 3, name: "CRANK", role: "gates", href: "design.html", anchor: [3.05, -0.85, 0.2], tag: [10, 50],
    explode: [0, 0, 0], status: (s, ctx) => ctx.card("gates"),
    build(k, root, ctx) {
      const { M, add, cyl, box } = k;
      const crank = k.group(root);
      add(cyl(0.16, 0.16, 6.2, 18), M.steel, crank, null, [0, 0, Math.PI / 2]);
      for (const x0 of [-2.1, -0.7, 0.7, 2.1]) {
        add(box(0.14, 0.95, 0.5), M.steel, crank, [x0, -0.05, 0]);
        add(new k.THREE.CylinderGeometry(0.42, 0.42, 0.14, 24, 1, false, Math.PI * 0.55, Math.PI * 0.9), M.block, crank, [x0, 0, 0], [0, 0, Math.PI / 2]);
      }
      const fly = k.group(root, [3.05, 0, 0]);
      add(cyl(0.95, 0.95, 0.14, 48), M.steel, fly, null, [0, 0, Math.PI / 2]);
      const teeth = new k.THREE.InstancedMesh(box(0.16, 0.08, 0.06), M.bright, 48);
      for (let t = 0; t < 48; t++) {
        const a = (t / 48) * Math.PI * 2, m = new k.THREE.Matrix4();
        m.makeRotationX(a); m.setPosition(0, Math.cos(a) * 0.98, Math.sin(a) * 0.98); teeth.setMatrixAt(t, m);
      }
      teeth.userData.noOutline = true; fly.add(teeth);
      // front accessory drive: crank pulley, belt, tensioner; the alternator module hangs on the third pulley
      const pulleys = [];
      const pulley = (r, y, z, w = 0.22) => {
        const g = k.group(root, [-3.15, y, z]);
        add(cyl(r, r, w, 32), M.steel, g, null, [0, 0, Math.PI / 2]);
        for (let sp = 0; sp < 4; sp++) add(box(w + 0.02, r * 1.7, 0.05), M.dark, g, null, [(sp * Math.PI) / 4, 0, 0]);
        pulleys.push({ g, r });
      };
      pulley(0.5, 0, 0); pulley(0.32, 1.35, 0); pulley(0.26, 0.95, 1.05);
      const belt = add(k.tube([[-3.15, -0.5, 0], [-3.15, 0.2, 0.55], [-3.15, 0.95, 1.31], [-3.15, 1.67, 0.2], [-3.15, 1.4, -0.32], [-3.15, 0.3, -0.52]], 0.04, 80, true), M.rubber, root);
      ctx.shared.beltAngle = 0;
      return {
        hit: [crank, fly, ...pulleys.map((p) => p.g), belt],
        update(dt, s) {
          crank.rotation.x = s.angle; fly.rotation.x = s.angle;
          pulleys.forEach((p, j) => (p.g.rotation.x = s.angle * (j ? 0.5 / p.r : 1)));
          ctx.shared.beltAngle = s.angle * (0.5 / 0.26);
        },
        info: () => ctx.rows("gates").slice(0, 4),
      };
    },
  },

  // ---------- 04 turbo = the brain: spools while it thinks ----------
  {
    id: "turbo", no: 4, name: "TURBO", role: "brain", href: "design.html", anchor: [-2.2, 0.95, 2.55], tag: [-46, 6],
    explode: [-0.45, 0, 1.1], status: (s, ctx) => s.thinking ? "active" : ctx.card("brain"),
    build(k, root, ctx) {
      const { M, add, cyl } = k;
      const turbo = k.group(root, [-2.2, 0.4, 2.55]);
      add(new k.THREE.TorusGeometry(0.45, 0.2, 14, 40), M.steel, turbo, null, [0, Math.PI / 2, 0]);
      add(cyl(0.3, 0.3, 0.36, 28, true), M.bore, turbo, null, [0, 0, Math.PI / 2]);
      const wheel = k.group(turbo, [-0.12, 0, 0]);
      for (let j = 0; j < 11; j++) {
        const a = (j / 11) * Math.PI * 2;
        add(k.box(0.2, 0.24, 0.025), M.bright, wheel, [0, Math.cos(a) * 0.15, Math.sin(a) * 0.15], [a + 0.5, 0, 0]);
      }
      // charge pipe up into the throttle body
      const pipe = add(k.tube([[-2.2, 0.85, 2.4], [-2.6, 1.9, 1.6], [-2.9, 2.55, 0.5], [-2.9, 2.55, 0.2]], 0.1, 28), M.steel, root);
      const ring = add(new k.THREE.TorusGeometry(0.66, 0.02, 6, 48), k.glowMat(0xf0b429), turbo, null, [0, Math.PI / 2, 0]);
      return {
        hit: [turbo, pipe],
        update(dt, s) {
          s.boost = Math.max(s.boost * Math.pow(0.4, dt), s.thinking ? 0.6 : 0);
          wheel.rotation.x += (s.reduced ? 0 : (s.rpm / 60) * 0.6 + s.boost * 30) * dt;
          ring.material.opacity = s.boost * 0.7;
        },
        info: (s) => [["model", s.brain], ["state", s.thinking ? "thinking" : s.warm ? "warm" : "off"], ...ctx.rows("brain").filter(([key]) => /left|L1|L3$/.test(key)).slice(0, 3)],
      };
    },
  },

  // ---------- 05 exhaust headers on the far bank = replies going out ----------
  {
    id: "exhaust", no: 5, name: "EXHAUST", role: "replies", anchor: [2.6, -0.35, -2.1], tag: [46, -30],
    explode: [0.13, 0, -1.1], status: (s, ctx) => s.reply > 0.1 ? "active" : "ok",
    build(k, root, ctx) {
      const { THREE, M, add } = k;
      const mat = new THREE.MeshStandardMaterial({ color: 0x5b5d61, metalness: 0.9, roughness: 0.35, emissive: 0xf08a29, emissiveIntensity: 0 });
      const dir = new THREE.Vector3(0, Math.SQRT1_2, -Math.SQRT1_2), side = -1;
      const headers = [];
      for (const x0 of [-2.1, -0.7, 0.7, 2.1]) {
        const x = x0 - 0.18;
        const port = dir.clone().multiplyScalar(1.95).add(new THREE.Vector3(x - 0.18, 0, side * 0.62));
        headers.push(add(k.tube([port, port.clone().add(new THREE.Vector3(0, 0, side * 0.5)), [x, 0.3, side * 2.25], [2.6, -0.35, side * 2.1]], 0.09, 28), mat, root));
      }
      const pipe = add(k.tube([[2.6, -0.35, -2.1], [3.6, -0.9, -2.0], [5.6, -1.05, -1.9]], 0.12, 24), M.steel, root);
      const muffler = add(k.cyl(0.26, 0.26, 1.0, 24), M.dark, root, [5.2, -1.03, -1.92], [0, 0, Math.PI / 2]);
      return {
        hit: [...headers, pipe, muffler],
        update(dt, s) { s.reply *= Math.pow(0.35, dt); mat.emissiveIntensity = Math.min(1.2, s.reply * 1.2 + (s.rpm / 5200) * 0.35); },
        info: (s) => [["last reply", s.reply > 0.1 ? "just sent" : "quiet"], ["replies", String(s.replies)]],
      };
    },
  },

  // ---------- 06 oil sump = memory; the oil level is how much JARVIS remembers ----------
  {
    id: "sump", no: 6, name: "SUMP", role: "memory", href: "#memory", anchor: [-1.6, -1.12, 0.86], tag: [-40, 40],
    explode: [0, -0.98, 0], status: (s, ctx) => ctx.card("memory"),
    build(k, root) {
      const { M, add, box } = k;
      const sump = add(box(5.0, 0.55, 1.7), M.ghostDark, root, [0, -0.98, 0]);
      const oil = add(box(4.9, 0.5, 1.6), new k.THREE.MeshStandardMaterial({ color: 0xb07a1c, metalness: 0.3, roughness: 0.25, transparent: true, opacity: 0.55, depthWrite: false }), root, [0, -1.0, 0]);
      oil.userData.noOutline = true;
      const dip = add(k.cyl(0.03, 0.03, 1.5, 8), M.bright, root, [-1.9, -0.45, 0.95], [0.25, 0, 0]);
      add(new k.THREE.TorusGeometry(0.08, 0.025, 6, 16), M.bright, dip, [0, 0.78, 0]);
      let level = 0;
      return {
        hit: [sump, oil, dip],
        update(dt, s) {
          const want = s.items ? Math.min(1, 0.18 + Math.log10(1 + s.items) / 5) : 0.05;
          level += (want - level) * Math.min(1, dt * 2);
          oil.scale.y = Math.max(0.02, level); oil.position.y = -1.25 + 0.25 * oil.scale.y;
        },
        info: (s) => [["items", s.items.toLocaleString()], ["links", s.links.toLocaleString()],
          ...Object.entries(s.kinds).sort((a, b) => b[1] - a[1]).slice(0, 3).map(([kind, n]) => [kind, n.toLocaleString()])],
      };
    },
  },

  // ---------- 07 ECU = desktop control, with status lights for the parts around it ----------
  {
    id: "ecu", no: 7, name: "ECU", role: "control", href: "design.html", anchor: [2.2, -0.6, 1.47], tag: [30, 46],
    explode: [0.39, -0.45, 1.23], status: (s, ctx) => ctx.card("control"),
    build(k, root, ctx) {
      const { M, add, box } = k;
      const ecu = k.group(root, [1.2, -1.05, 1.42]);
      add(box(2.2, 0.9, 0.22), M.dark, ecu);
      const plate = k.label("ECU", 0.6, 0.2, { weight: 700 }); plate.position.set(-0.75, 0.3, 0.12); ecu.add(plate);
      const CHIPS = [
        { id: "voice", name: "VOICE" }, { id: "cluster", name: "CLUSTER" }, { id: "lanmouse", name: "INPUT" },
        { id: "chrome", name: "BROWSER" }, { id: "sandbox", name: "SANDBOX" },
      ];
      CHIPS.forEach((c, j) => {
        const y = 0.32 - j * 0.15;
        c.led = add(box(0.07, 0.07, 0.04), k.ledMat(), ecu, [0.05, y, 0.12]);
        const lbl = k.label(c.name, 0.9, 0.13, { size: 0.75 }); lbl.position.set(0.58, y, 0.12); ecu.add(lbl);
      });
      // wiring loom from the ECU up to the plug rail
      const loom = add(k.tube([[0.1, -0.7, 1.45], [-0.6, 0.2, 2.0], [-1.2, 1.4, 2.0], [-1.6, 2.0, 1.7]], 0.05, 24), M.rubber, root);
      return {
        hit: [ecu, loom],
        update(dt, s) {
          for (const c of CHIPS) {
            const st = c.id === "voice" ? (s.listening ? "ok" : "off") : ctx.card(c.id);
            c.led.material.emissive.setHex(LED[st] ?? 0);
          }
        },
        info: (s) => [...ctx.rows("control").slice(0, 3), ["last tool", s.lastTool],
          ...CHIPS.filter((c) => c.id !== "voice" && ctx.card(c.id) !== "ok").map((c) => [c.name.toLowerCase(), ctx.card(c.id)])],
      };
    },
  },

  // ---------- 08 radiator + fan = health: runs hot when a part reports warn or fail ----------
  {
    id: "radiator", no: 8, name: "RADIATOR", role: "health", href: "design.html", anchor: [-4.15, 2.5, 0.9], tag: [-40, -34],
    explode: [-1.23, 0, 0], status: (s, ctx) => s.health,
    build(k, root, ctx) {
      const { M, add, box, cyl } = k;
      const rad = k.group(root, [-4.15, 0.95, 0]);
      add(box(0.12, 2.9, 0.16), M.frame, rad, [0, 0, 1.38]); add(box(0.12, 2.9, 0.16), M.frame, rad, [0, 0, -1.38]);
      add(box(0.22, 0.22, 2.9), M.steel, rad, [0, 1.45, 0]); add(box(0.22, 0.22, 2.9), M.steel, rad, [0, -1.45, 0]);
      const coreMat = new k.THREE.MeshStandardMaterial({ color: 0x23262a, metalness: 0.6, roughness: 0.55, emissive: 0x000000, emissiveIntensity: 0.9, transparent: true, opacity: 0.8, depthWrite: false });
      const core = add(box(0.08, 2.65, 2.6), coreMat, rad); core.userData.noOutline = true;
      for (let f = 0; f < 15; f++) add(box(0.1, 0.02, 2.6), M.steel, rad, [0, -1.25 + f * 0.18, 0]).userData.noOutline = true;
      const cap = add(cyl(0.1, 0.1, 0.12, 16), M.bright, rad, [0, 1.62, 0.9]);
      // fan in a shroud between the core and the pulleys
      const shroud = add(new k.THREE.TorusGeometry(0.95, 0.05, 8, 48), M.dark, root, [-3.72, 0.95, 0], [0, Math.PI / 2, 0]);
      const fan = k.group(root, [-3.72, 0.95, 0]);
      add(cyl(0.16, 0.16, 0.16, 16), M.steel, fan, null, [0, 0, Math.PI / 2]);
      for (let b = 0; b < 7; b++) { const a = (b / 7) * Math.PI * 2; add(box(0.05, 0.8, 0.26), M.steel, fan, [0, Math.cos(a) * 0.48, Math.sin(a) * 0.48], [a, 0.35, 0]); }
      const hoses = [
        add(k.tube([[-4.1, 2.2, 0.9], [-3.6, 2.4, 1.0], [-2.95, 2.25, 0.85]], 0.08, 16), M.rubber, root),
        add(k.tube([[-4.1, -0.3, -0.9], [-3.6, -0.5, -0.8], [-2.9, -0.35, -0.9]], 0.08, 16), M.rubber, root),
      ];
      const gauge = k.panel(0.95, 0.24); gauge.position.set(-4.08, 2.08, 0); gauge.rotation.y = -Math.PI / 2; root.add(gauge);
      let spin = 0;
      return {
        hit: [rad, shroud, fan, ...hoses, cap, gauge],
        update(dt, s) {
          const hot = s.health === "fail" ? 1 : s.health === "warn" ? 0.45 : 0;
          spin += (s.reduced ? 0 : (s.rpm / 60) * 0.5 + hot * 6 + 0.3) * dt;
          fan.rotation.x = spin;
          coreMat.emissive.setHex(s.health === "fail" ? 0xff4d5e : s.health === "warn" ? 0xf0b429 : 0x000000);
          coreMat.emissiveIntensity = 0.12 + hot * 0.18;
          gauge.userData.set(`TEMP ${{ ok: "NORMAL", warn: "CHECK", fail: "HOT" }[s.health]}`, s.health === "ok" ? "#cdd2d8" : s.health === "warn" ? "#f0b429" : "#ff4d5e");
        },
        info: (s) => [["temp", { ok: "normal", warn: "check", fail: "hot" }[s.health]], ...ctx.notOk().slice(0, 4)],
      };
    },
  },

  // ---------- 09 alternator on the belt = the cluster: powers the other machines ----------
  {
    id: "alternator", no: 9, name: "ALTERNATOR", role: "cluster", href: "#machines", anchor: [-2.75, 1.35, 1.5], tag: [-30, -40],
    explode: [-0.65, 0.26, 0.85], status: (s, ctx) => ctx.card("cluster"),
    build(k, root, ctx) {
      const { M, add, cyl, box } = k;
      const alt = k.group(root, [-2.72, 0.95, 1.05]);
      add(cyl(0.38, 0.38, 0.62, 28), M.ghostDark, alt, null, [0, 0, Math.PI / 2]);
      for (let f = 0; f < 8; f++) add(box(0.6, 0.04, 0.78), M.steel, alt, null, [(f / 8) * Math.PI, 0, 0]).userData.noOutline = true;
      const rotor = k.group(alt);
      for (let p = 0; p < 6; p++) { const a = (p / 6) * Math.PI * 2; add(box(0.5, 0.08, 0.14), M.bright, rotor, [0, Math.cos(a) * 0.2, Math.sin(a) * 0.2], [a, 0, 0]); }
      add(box(0.3, 0.5, 0.14), M.frame, root, [-2.72, 0.55, 0.62]);  // bracket to the block
      const led = add(box(0.08, 0.08, 0.08), k.ledMat(), alt, [0.34, 0.3, 0.2]);
      return {
        hit: [alt],
        update(dt, s) {
          rotor.rotation.x = ctx.shared.beltAngle || 0;
          led.material.emissive.setHex(LED[ctx.card("cluster")] ?? 0);
        },
        info: (s) => [...ctx.rows("cluster").slice(0, 4)],
      };
    },
  },

  // ---------- 10 fuel rails + regulator = model quota: the level is the L3 budget left ----------
  {
    id: "fuel", no: 10, name: "FUEL", role: "quota", href: "#credits", anchor: [2.95, 1.25, 1.95], tag: [48, 4],
    explode: [0.78, 0.45, 0.98], status: (s, ctx) => ctx.fuelLow == null ? "idle" : ctx.fuelLow ? "warn" : "ok",
    build(k, root, ctx) {
      const { THREE, M, add, cyl } = k;
      const parts = [];
      for (const side of [-1, 1]) {  // a rail along the outer edge of each head, an injector per cylinder
        const g = k.group(root);
        g.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), new THREE.Vector3(0, Math.SQRT1_2, side * Math.SQRT1_2));
        parts.push(add(cyl(0.07, 0.07, 5.2, 12), M.bright, g, [0, 2.05, side * 0.72], [0, 0, Math.PI / 2]));
        for (const x of [-2.1, -0.7, 0.7, 2.1]) add(cyl(0.05, 0.035, 0.28, 10), M.steel, g, [x + side * 0.18, 1.93, side * 0.64]);
      }
      const can = k.group(root, [2.95, 0.5, 1.95]);
      const shell = add(cyl(0.36, 0.36, 1.1, 28, true), M.bore, can);
      add(cyl(0.38, 0.38, 0.08, 28), M.steel, can, [0, 0.58, 0]); add(cyl(0.38, 0.38, 0.08, 28), M.steel, can, [0, -0.58, 0]);
      const fillMat = new THREE.MeshStandardMaterial({ color: 0xd9a441, emissive: 0x3a2400, metalness: 0.2, roughness: 0.3, transparent: true, opacity: 0.75, depthWrite: false });
      const fill = add(cyl(0.32, 0.32, 1, 24), fillMat, can); fill.userData.noOutline = true;
      const line = add(k.tube([[2.95, 1.08, 1.95], [2.7, 1.9, 1.85], [2.35, 2.25, 1.6]], 0.04, 16), M.steel, root);
      const gauge = k.panel(0.9, 0.22); gauge.position.set(2.95, -0.25, 2.34); root.add(gauge);
      let level = 1;
      const frac = () => {
        const v = (ctx.rows("brain").find(([key]) => /L3 left/.test(key)) || [])[1];
        const m = /(\d+)\s*\/\s*(\d+)/.exec(String(v || ""));
        return m && +m[2] ? +m[1] / +m[2] : null;
      };
      return {
        hit: [can, line, gauge, ...parts],
        update(dt) {
          const f = frac(), want = f == null ? 0.5 : f;
          ctx.fuelLow = f == null ? null : f < 0.25;
          level += (want - level) * Math.min(1, dt * 2);
          fill.scale.y = Math.max(0.02, level); fill.position.y = -0.5 + level / 2;
          fillMat.color.setHex(want < 0.25 ? 0xff4d5e : 0xd9a441);
          gauge.userData.set(f == null ? "L3 —" : `L3 ${pct(f)}`, want < 0.25 ? "#ff4d5e" : "#cdd2d8");
        },
        info: () => { const f = frac(); return [["L3 budget", f == null ? "unknown" : `${pct(f)} left`], ...ctx.rows("brain").filter(([key]) => /left/.test(key))]; },
      };
    },
  },

  // ---------- 11 gearbox = actions: the output shaft turns while a tool runs ----------
  {
    id: "gearbox", no: 11, name: "GEARBOX", role: "actions", anchor: [4.6, 0.62, 0.6], tag: [36, -40],
    explode: [1.3, 0, 0], status: (s, ctx) => s.toolSpin > 0.15 ? "active" : "ok",
    build(k, root) {
      const { THREE, M, add, box, cyl } = k;
      const bell = add(cyl(0.7, 1.02, 0.7, 32, true), M.ghost, root, [3.5, 0, 0], [0, 0, Math.PI / 2]);
      const casing = add(box(1.6, 1.1, 1.1), M.ghost, root, [4.65, -0.05, 0]);
      const gear = (r, n, pos) => {
        const g = k.group(root, pos);
        add(cyl(r, r, 0.12, 32), M.steel, g, null, [0, 0, Math.PI / 2]);
        const teeth = new THREE.InstancedMesh(box(0.13, 0.08, 0.06), M.bright, n);
        for (let t = 0; t < n; t++) { const a = (t / n) * Math.PI * 2, m = new THREE.Matrix4(); m.makeRotationX(a); m.setPosition(0, Math.cos(a) * (r + 0.03), Math.sin(a) * (r + 0.03)); teeth.setMatrixAt(t, m); }
        teeth.userData.noOutline = true; g.add(teeth);
        return g;
      };
      const g1 = gear(0.36, 22, [4.2, 0.08, 0]), g2 = gear(0.22, 14, [4.55, -0.26, 0.28]), g3 = gear(0.3, 18, [5.0, 0.04, 0]);
      const shaft = k.group(root, [5.75, 0, 0]);
      add(cyl(0.09, 0.09, 0.9, 14), M.bright, shaft, null, [0, 0, Math.PI / 2]);
      add(box(0.12, 0.42, 0.12), M.steel, shaft, [0.42, 0, 0]);
      const display = k.panel(0.9, 0.3); display.rotation.x = -Math.PI / 2; display.position.set(4.65, 0.52, 0); root.add(display);
      let a1 = 0, out = 0;
      return {
        hit: [bell, casing, g1, g2, g3, shaft, display],
        update(dt, s) {
          s.toolSpin *= Math.pow(0.5, dt);
          const drive = s.reduced ? 0 : ((s.rpm / 60) * Math.PI * 2 * 0.12);
          a1 = s.angle; out += drive * Math.min(1, s.toolSpin * 2) * dt;
          g1.rotation.x = a1; g2.rotation.x = -a1 * (0.36 / 0.22); g3.rotation.x = a1 * (0.36 / 0.3);
          shaft.rotation.x = out;
          display.userData.set(s.toolSpin > 0.15 ? `D · ${s.tools} TOOLS` : s.rpm > 3 ? `N · ${s.tools} TOOLS` : "P · PARKED", s.toolSpin > 0.15 ? "#f0b429" : "#cdd2d8");
        },
        info: (s) => [["gear", s.toolSpin > 0.15 ? "drive (a tool is running)" : s.rpm > 3 ? "neutral" : "park"], ["tools this session", String(s.tools)], ["last tool", s.lastTool]],
      };
    },
  },

  // ---------- 12 chassis = the machines JARVIS runs on: everything bolts to it ----------
  {
    id: "chassis", no: 12, name: "CHASSIS", role: "machines", href: "#machines", anchor: [5.3, -1.6, 1.25], tag: [24, 40],
    explode: [0, -1.69, 0], status: (s, ctx) => !s.devices.length ? "idle" : s.devices.some((d) => !d.online) ? "warn" : "ok",
    build(k, root, ctx) {
      const { M, add, box } = k;
      const frame = k.group(root);
      for (const z of [-1.25, 1.25]) add(box(10.6, 0.16, 0.22), M.frame, frame, [0.55, -1.62, z]);
      for (const x of [-4.4, -1.2, 2.2, 5.5]) add(box(0.18, 0.14, 2.7), M.frame, frame, [x, -1.62, 0]);
      for (const x of [-1.6, 1.6]) for (const z of [-1, 1]) {  // engine mounts: bracket + rubber
        add(box(0.34, 0.6, 0.16), M.frame, frame, [x, -1.25, z * 1.18]);
        add(k.cyl(0.13, 0.13, 0.16, 16), M.rubber, frame, [x, -0.85, z * 1.18], [Math.PI / 2, 0, 0]);
        add(box(0.34, 0.2, 0.16), M.frame, frame, [x, -0.65, z * 1.18]);
      }
      add(box(0.3, 0.6, 0.9), M.frame, frame, [4.65, -1.27, 0]);  // gearbox crossmember mount
      for (const x of [-4.4, 5.5]) for (const z of [-1.25, 1.25]) add(box(0.3, 0.12, 0.3), M.dark, frame, [x, -1.76, z]);  // feet
      // node plates: one LED per machine bolted to the rail
      const plates = [];
      for (let j = 0; j < 4; j++) {
        const led = add(box(0.1, 0.1, 0.06), k.ledMat(), frame, [3.3 + j * 0.42, -1.62, 1.37]);
        led.visible = false; plates.push(led);
      }
      return {
        hit: [frame],
        update(dt, s) {
          s.devices.slice(0, 4).forEach((d, j) => { plates[j].visible = true; plates[j].material.emissive.setHex(d.online ? LED.ok : LED.fail); });
        },
        info: (s) => s.devices.length
          ? s.devices.slice(0, 5).map((d) => [d.name, d.online ? (d.here ? "this device" : "online") : "offline"])
          : [["machines", "loading"]],
      };
    },
  },
];
