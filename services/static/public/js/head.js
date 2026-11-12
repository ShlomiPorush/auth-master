/* Shared <head> elements, written synchronously by every page: the favicon and
   the stylesheet. The server prepends window.__BASE_PATH__ when it serves this
   file, so both work under a ROOT_PATH. Fonts are referenced from ui.css with
   relative URLs (../fonts/...). */
var uiHeadBase = window.__BASE_PATH__ || "";
document.write(
  '<link rel="icon" type="image/svg+xml" href="' + uiHeadBase + '/images/favicon.svg" />' +
  '<link rel="stylesheet" href="' + uiHeadBase + '/css/ui.css" />'
);
