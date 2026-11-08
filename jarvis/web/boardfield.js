/* The pipeline wheel's "field" hook (wheel.js calls window.JarvisField), backed by the 3D
   engine (engine3d.js).
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
      pick(sx, sy) { board()?.hoverAt(sx, sy); },
      unhover() { board()?.leave(); },
    };
  };
})();
