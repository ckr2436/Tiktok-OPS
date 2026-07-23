from __future__ import annotations

from types import SimpleNamespace

from app.app import create_app
from app.services.globalaiopc import tasks as kyy_tasks
from app.services.kie_api.accounts import DEFAULT_PROVIDER_KEY, provider_catalog


def test_only_canonical_ai_video_routes_are_registered() -> None:
    paths = {route.path for route in create_app().routes}

    assert "/api/v1/platform/api-keys/keys" in paths
    assert "/api/v1/tenants/{workspace_id}/ai-video/videos/tasks" in paths
    assert (
        "/api/v1/tenants/{workspace_id}/ai-video/videos/public-reference/{task_id}/{file_id}"
        in paths
    )
    assert not any(
        marker in path
        for path in paths
        for marker in ("/platform/kie-ai", "/kie-ai/sora2", "/globalaiopc-ai")
    )


def test_provider_registry_has_no_retired_kie_provider() -> None:
    provider_ids = {item["id"] for item in provider_catalog()}

    assert DEFAULT_PROVIDER_KEY == "bandianwa"
    assert "kie-ai" not in provider_ids


def test_kyy_reference_urls_use_canonical_ai_video_endpoint(monkeypatch) -> None:
    class Query:
        def filter(self, *args, **kwargs):
            return self

        def order_by(self, *args, **kwargs):
            return self

        def all(self):
            return [SimpleNamespace(id=41), SimpleNamespace(id=42)]

    class Db:
        def query(self, *args, **kwargs):
            return Query()

    monkeypatch.setattr(kyy_tasks, "_public_base_url", lambda: "https://gmv.example")
    urls = kyy_tasks._public_reference_urls(
        Db(),
        task=SimpleNamespace(workspace_id=3, id=19),
    )

    assert urls == [
        "https://gmv.example/api/v1/tenants/3/ai-video/videos/public-reference/19/41",
        "https://gmv.example/api/v1/tenants/3/ai-video/videos/public-reference/19/42",
    ]
