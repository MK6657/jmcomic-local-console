(function () {
  'use strict';

  // The CDN may lack a portrait variant. Try its square image once, then
  // let the page's existing placeholder handler handle a final failure.
  document.addEventListener('error', function (event) {
    const image = event.target;
    if (image.tagName !== 'IMG' || !image.src.startsWith(location.origin + '/api/cover/')) return;
    const url = new URL(image.src);
    if (!/^\/api\/cover\/[0-9]{1,20}\/portrait$/.test(url.pathname) || url.searchParams.has('fallback')) return;
    event.stopImmediatePropagation();
    url.searchParams.set('fallback', 'square');
    image.src = url.href;
  }, true);
})();
