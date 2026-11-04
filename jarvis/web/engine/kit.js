/* Shared parts kit for the console engine (engine3d.js + engine/modules.js):
   materials, mesh helpers, painted labels and live read-out panels. No state lives here. */
import * as THREE from "../vendor/three-0.170.0.module.min.js";

export const COLOR = { amber: 0xf0b429, ok: 0x7fdc9a, warn: 0xf0b429, fail: 0xff4d5e, off: 0x000000, edge: 0xb8bec6 };

export function makeKit() {
  const std = (o) => new THREE.MeshStandardMaterial(o);
  const M = {
    block: std({ color: 0x2c2f33, metalness: 0.75, roughness: 0.42 }),
    dark: std({ color: 0x17181a, metalness: 0.6, roughness: 0.5 }),
    steel: std({ color: 0x8c9198, metalness: 0.95, roughness: 0.26 }),
    bright: std({ color: 0xbfc4ca, metalness: 1, roughness: 0.18 }),
    rubber: std({ color: 0x0d0d0e, metalness: 0.1, roughness: 0.85 }),
    frame: std({ color: 0x3a3d42, metalness: 0.85, roughness: 0.38 }),
    // x-ray castings: see-through so the moving parts inside show
    ghost: std({ color: 0x8e959e, metalness: 0.7, roughness: 0.3, transparent: true, opacity: 0.2, depthWrite: false }),
    ghostDark: std({ color: 0x4a4f56, metalness: 0.7, roughness: 0.35, transparent: true, opacity: 0.32, depthWrite: false }),
    bore: std({ color: 0x9aa0a8, metalness: 0.6, roughness: 0.25, side: THREE.DoubleSide, transparent: true, opacity: 0.16, depthWrite: false }),
  };
  const isGhost = (m) => m === M.ghost || m === M.ghostDark;
  const ledMat = () => std({ color: 0x111111, emissive: 0x000000, emissiveIntensity: 1.6, roughness: 0.4 });
  const glowMat = (color) => new THREE.MeshBasicMaterial({ color, transparent: true, opacity: 0, blending: THREE.AdditiveBlending, depthWrite: false, side: THREE.DoubleSide });

  // add(geometry, material, parent) -> mesh, placed with an optional [x, y, z] and rotation [rx, ry, rz]
  const add = (geo, mat, parent, pos, rot) => {
    const m = new THREE.Mesh(geo, mat); parent.add(m);
    if (pos) m.position.set(...pos);
    if (rot) m.rotation.set(...rot);
    return m;
  };
  const group = (parent, pos) => { const g = new THREE.Group(); if (pos) g.position.set(...pos); parent.add(g); return g; };
  const box = (w, h, d) => new THREE.BoxGeometry(w, h, d);
  const cyl = (r1, r2, h, seg = 24, open = false) => new THREE.CylinderGeometry(r1, r2, h, seg, 1, open);
  const tube = (pts, r = 0.07, seg = 24, closed = false) =>
    new THREE.TubeGeometry(new THREE.CatmullRomCurve3(pts.map((p) => (p.isVector3 ? p : new THREE.Vector3(...p))), closed), seg, r, 8, closed);

  // A painted plate. label(text, w, h) is static; panel(w, h).set(text, color) redraws only on change.
  const paint = (c, text, opts) => {
    const g = c.getContext("2d");
    g.clearRect(0, 0, c.width, c.height);
    if (opts.bg) { g.fillStyle = opts.bg; g.fillRect(0, 0, c.width, c.height); }
    if (opts.frame) { g.strokeStyle = opts.frame; g.lineWidth = Math.max(2, c.height * 0.06); g.strokeRect(1, 1, c.width - 2, c.height - 2); }
    g.fillStyle = opts.color || "#cdd2d8"; g.textAlign = "center"; g.textBaseline = "middle";
    g.font = `${opts.weight || 600} ${Math.round(c.height * (opts.size || 0.55))}px "Geist Mono", ui-monospace, monospace`;
    g.fillText(text, c.width / 2, c.height / 2 + 1);
  };
  const plate = (w, h, text, opts = {}) => {
    const c = document.createElement("canvas"); c.width = Math.round(w * 160); c.height = Math.round(h * 160);
    paint(c, text, opts);
    const t = new THREE.CanvasTexture(c); t.colorSpace = THREE.SRGBColorSpace; t.anisotropy = 8;
    const mesh = new THREE.Mesh(new THREE.PlaneGeometry(w, h), new THREE.MeshBasicMaterial({ map: t, transparent: true, depthWrite: false, toneMapped: false }));
    let last = text + "|" + (opts.color || "");
    mesh.userData.set = (next, color) => {
      const key = next + "|" + (color || opts.color || "");
      if (key === last) return;
      last = key; paint(c, next, { ...opts, color: color || opts.color }); t.needsUpdate = true;
    };
    return mesh;
  };
  const label = (text, w, h, opts) => plate(w, h, text, opts);
  const panel = (w, h, opts = {}) => plate(w, h, "", { bg: "rgba(6,6,6,.92)", frame: "rgba(184,190,198,.45)", size: 0.6, ...opts });

  // Outline every mesh under root. Ghost castings keep a faint wire; solid parts only show it on focus.
  const outline = (root) => {
    const ghostLine = new THREE.LineBasicMaterial({ color: COLOR.edge, transparent: true, opacity: 0.35, depthWrite: false });
    const focusLine = new THREE.LineBasicMaterial({ color: COLOR.amber, transparent: true, opacity: 0, depthWrite: false });
    const meshes = [];
    root.traverse((m) => { if (m.isMesh && !m.userData.noOutline && m.geometry.type !== "PlaneGeometry" && m.geometry.type !== "CircleGeometry") meshes.push(m); });
    for (const m of meshes) m.add(new THREE.LineSegments(new THREE.EdgesGeometry(m.geometry, 28), isGhost(m.material) ? ghostLine : focusLine));
    return (on) => {
      ghostLine.color.setHex(on ? COLOR.amber : COLOR.edge); ghostLine.opacity = on ? 0.9 : 0.35;
      focusLine.opacity = on ? 0.55 : 0;
    };
  };

  return { THREE, M, ledMat, glowMat, add, group, box, cyl, tube, label, panel, outline };
}
