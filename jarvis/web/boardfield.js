/* The pipeline wheel's "field" hook (wheel.js calls window.JarvisField), now backed by the 3D
   circuit board (board3d.js) instead of the old memory field (field.js, kept but not loaded).
   The wheel still draws its ring and labels on a transparent layer; this passes its layout and
   hover to the board, which loads later as a module and reads them from window.JarvisBoardHook. */
(function () {
  "use strict";
  const hook = window.JarvisBoardHook = { G: null, size: null, board: null, view: null };
  const board = () => hook.board;
  window.JarvisField = function () {
    return {
      layout(W, H, dpr, G) { hook.G = G; hook.size = { W, H }; board()?.place(G, W, H); },
      burst(x, y) { board()?.burst?.(x, y); },     // a stage fired: the engine sends a strand to it
      draw(o) { hook.view = o && o.view; board()?.setView?.(o && o.view); return false; },  // follow the wheel's zoom/pan
      pick(sx, sy) { board()?.hoverAt(sx, sy); return null; },
      unhover() { board()?.leave(); },
      setGraph() {},
    };
  };
})();
