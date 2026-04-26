/* Shared <head> elements — injected synchronously by all pages.
   Each page only needs:
     <head>
       <title>…</title>
       <meta name="description" content="…" />
       <script src="js/head.js"></script>
       <script src="js/theme.js"></script>
     </head>
   __BASE_PATH__ is injected by the server when serving this file.
*/
var _B = window.__BASE_PATH__ || "";
document.write('\
<link rel="icon" type="image/svg+xml" href="' + _B + '/images/favicon.svg" />\
<link rel="preconnect" href="https://fonts.googleapis.com" />\
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin />\
<link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet" />\
<link href="' + _B + '/css/tailwind.css" rel="stylesheet" />\
<link href="' + _B + '/css/shared.css" rel="stylesheet" />\
');
