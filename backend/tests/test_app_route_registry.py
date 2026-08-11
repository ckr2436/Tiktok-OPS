from __future__ import annotations

from collections import Counter

from app.app import create_app


def test_unified_runtime_routes_are_registered_once():
    app = create_app()
    method_paths = [
        (tuple(sorted(route.methods or ())), route.path)
        for route in app.routes
        if hasattr(route, "methods")
    ]
    assert not [
        key for key, count in Counter(method_paths).items() if count > 1
    ]

    paths = {route.path for route in app.routes}
    expected_prefixes = {
        "/api/v1/platform/api-keys",
        "/api/v1/platform/flow2api",
        "/api/v1/platform/sub2api",
        "/api/v1/platform/jimeng-lab",
        "/api/v1/platform/doubao-lab",
        "/api/v1/tenants/{workspace_id}/ai-video/videos",
        "/api/v1/tenants/{workspace_id}/tiktok-shop/content-posting",
    }
    for prefix in expected_prefixes:
        assert any(path.startswith(prefix) for path in paths), prefix

    assert not any("/platform/kie-ai" in path for path in paths)
