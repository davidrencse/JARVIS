// Runs before first paint so the page never flashes the wrong theme.
// The preference is "system", "light" or "dark"; dark unless chosen otherwise, and "system" follows the OS live.
(function () {
  var pref = "dark";
  try { pref = localStorage.getItem("jarvis.theme") || "dark"; } catch (e) {}
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  function apply() {
    var p = window.JarvisTheme.pref;
    document.documentElement.dataset.theme = p === "system" ? (media && !media.matches ? "light" : "dark") : p;
  }
  window.JarvisTheme = { pref: pref, apply: apply };
  if (media && media.addEventListener) media.addEventListener("change", function () {
    if (window.JarvisTheme.pref === "system") apply();
  });
  apply();
})();
