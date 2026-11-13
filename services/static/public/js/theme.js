/* Applies the colour theme before the first paint. "system" (the default)
   follows the operating system and keeps following it; "light" and "dark" are
   remembered in this browser. Storage can be unavailable (private mode), so
   every access is guarded and the page still renders. */
(function () {
  var KEY = "uiTheme";
  var media = window.matchMedia ? window.matchMedia("(prefers-color-scheme: dark)") : null;
  // The choice made in this visit; it holds when storage is blocked or fails
  var chosen = null;

  function stored() {
    if (chosen) return chosen;
    try {
      var v = window.localStorage.getItem(KEY);
      return v === "light" || v === "dark" ? v : "system";
    } catch (e) {
      return "system";
    }
  }

  function apply() {
    var pref = stored();
    var dark = pref === "dark" || (pref === "system" && !!media && media.matches);
    document.documentElement.classList.toggle("dark", dark);
    document.documentElement.setAttribute("data-theme-pref", pref);
    try { document.dispatchEvent(new CustomEvent("ui-theme", { detail: { pref: pref, dark: dark } })); } catch (e) { /* old browsers */ }
  }

  apply();
  if (media) {
    var follow = function () { if (stored() === "system") apply(); };
    if (media.addEventListener) media.addEventListener("change", follow);
    else if (media.addListener) media.addListener(follow);
  }
  // Another tab changed the theme
  window.addEventListener("storage", function (event) { if (event.key === KEY) { chosen = null; apply(); } });

  window.uiThemeGet = stored;
  window.uiThemeSet = function (pref) {
    chosen = pref === "light" || pref === "dark" ? pref : "system";
    try {
      if (pref === "light" || pref === "dark") window.localStorage.setItem(KEY, pref);
      else window.localStorage.removeItem(KEY);
    } catch (e) { /* not remembered, still applied */ }
    apply();
  };
})();
