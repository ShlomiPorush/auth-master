/* Shared <head> elements, written synchronously by every page: the favicon and
   the stylesheet. The server prepends window.__BASE_PATH__ when it serves this
   file, so both work under a ROOT_PATH. Fonts are referenced from ui.css with
   relative URLs (../fonts/...). */
var uiHeadBase = window.__BASE_PATH__ || "";
/* The sidebar folded into the rail (NL-7) is applied here, before the first
   paint, so it never flashes open; blocked storage leaves it open. */
try {
  if (window.localStorage.getItem("uiNavCollapsed") === "1") document.documentElement.classList.add("ui-nav-collapsed");
} catch (e) { /* storage blocked: not remembered */ }
document.write(
  '<link rel="icon" type="image/svg+xml" href="' + uiHeadBase + '/images/favicon.svg" />' +
  '<link rel="stylesheet" href="' + uiHeadBase + '/css/ui.css" />'
);
