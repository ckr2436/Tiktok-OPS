window.addEventListener('load', function () {
  Redoc.init('/api/admin-docs/openapi.json', {}, document.getElementById('redoc-root'));
});
