window.addEventListener('load', function () {
  window.ui = SwaggerUIBundle({
    url: '/api/admin-docs/openapi.json',
    dom_id: '#swagger-ui',
    presets: [SwaggerUIBundle.presets.apis],
  });
});
