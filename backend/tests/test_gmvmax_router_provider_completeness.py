from __future__ import annotations

import asyncio
from datetime import datetime, timedelta
import inspect
import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.celery_app import TTB_SYNC_QUEUE, celery_app
from app.features.tenants.ttb.gmv_max import router_provider
from app.services import ttb_sync_dispatch


class _FakeMutation:
    def assert_current(self, _db) -> None:
        return None

    def commit(self, db) -> None:
        commit = getattr(db, "commit", None)
        if callable(commit):
            commit()


@pytest.fixture(autouse=True)
def _active_mutation(monkeypatch):
    monkeypatch.setattr(
        router_provider,
        "active_gmvmax_mutation_lease",
        lambda _db: _FakeMutation(),
    )


class _Result:
    def __init__(self, *, rows=None, row=None):
        self._rows = list(rows or [])
        self._row = row

    def mappings(self):
        return self

    def all(self):
        return list(self._rows)

    def first(self):
        return self._row


def test_ttb_sync_publishers_use_the_canonical_events_queue(monkeypatch) -> None:
    assert celery_app.conf.task_routes["ttb.sync.*"]["queue"] == TTB_SYNC_QUEUE
    dispatch_source = inspect.getsource(ttb_sync_dispatch.dispatch_sync)
    assert "queue=TTB_SYNC_QUEUE" in dispatch_source
    assert 'queue_name = "gmvmax"' not in dispatch_source

    published: list[dict] = []

    class _TaskResult:
        def get(self, *, timeout):
            assert timeout == 300
            return {"status": "success", "errors": []}

    monkeypatch.setattr(
        router_provider.task_sync_products,
        "apply_async",
        lambda **kwargs: published.append(kwargs) or _TaskResult(),
    )
    context = SimpleNamespace(
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )
    asyncio.run(
        router_provider._sync_products_now(
            context,
            advertiser_id="adv-1",
            store_id="store-1",
        )
    )
    assert published[0]["queue"] == TTB_SYNC_QUEUE


def test_gmvmax_product_sync_rejects_partial_task_results(monkeypatch) -> None:
    class _TaskResult:
        def get(self, *, timeout):
            assert timeout == 300
            return {
                "status": "partial",
                "errors": [{"stage": "products", "code": "upstream_incomplete"}],
            }

    monkeypatch.setattr(
        router_provider.task_sync_products,
        "apply_async",
        lambda **_kwargs: _TaskResult(),
    )
    context = SimpleNamespace(
        workspace_id=7,
        auth_id=11,
        provider="tiktok-business",
    )

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(
            router_provider._sync_products_now(
                context,
                advertiser_id="adv-1",
                store_id="store-1",
            )
        )

    assert exc_info.value.status_code == 502


def test_historical_removed_creatives_uses_all_rows_beyond_1000() -> None:
    now = datetime(2026, 7, 17, 12, 0)
    evidence_rows = [
        {
            "creative_id": f"creative-{index}",
            "item_group_id": "product-1",
            "metric_campaign_id": f"campaign-{index}",
            "stat_time_day": now.date(),
            "metric_cost_cents": 500,
            "metric_gmv_cents": 0,
            "metric_orders": 0,
            "remove_event_id": index + 1,
            "remove_campaign_id": f"campaign-{index}",
            "remove_reason": "creative_guard:test",
            "remove_cost_cents": 500,
            "remove_created_at": now - timedelta(hours=index % 24),
            "metric_updated_at": now - timedelta(seconds=index),
        }
        for index in range(1001)
    ]

    class _Db:
        def __init__(self):
            self.sql: list[str] = []

        def execute(self, statement, _params):
            sql = " ".join(str(statement).split())
            self.sql.append(sql)
            if "from gmvmax_product_creative_metrics_daily m" in sql:
                return _Result(rows=evidence_rows)
            if "from ttb_advertisers" in sql:
                return _Result(row=None)
            raise AssertionError(f"unexpected SQL: {sql}")

    db = _Db()
    result = router_provider._load_historical_removed_creatives(
        db,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        store_id="store-1",
        item_group_ids=["product-1"],
        guard_config={
            "historical_blacklist_honor_add_events": False,
            "historical_blacklist_min_remove_events": 1,
            "historical_blacklist_min_distinct_campaigns": 1,
            "historical_blacklist_min_distinct_time_buckets": 1,
            "historical_blacklist_min_spend_cents": 1,
            "product_effective_prices": {"product-1": "1.00"},
        },
    )

    assert len(result) == 1001
    assert ("creative-1000", "product-1") in result
    assert all("limit 1000" not in sql.lower() for sql in db.sql)


class _AuditDb:
    def __init__(self):
        self.audit_params: list[dict] = []
        self.commits = 0

    def execute(self, statement, params):
        sql = " ".join(str(statement).split())
        if "insert into gmv_campaign_guard_events" not in sql:
            raise AssertionError(f"unexpected SQL: {sql}")
        self.audit_params.append(dict(params))
        return None

    def commit(self):
        self.commits += 1


class _Response:
    def __init__(self, batch: int):
        self.batch = batch

    def model_dump(self, **_kwargs):
        return {"batch": self.batch}


class _Client:
    async def gmv_max_creative_status_update(self, _request):
        raise AssertionError("_call_tiktok should be intercepted")


def _context(db):
    return SimpleNamespace(
        db=db,
        workspace_id=7,
        auth_id=11,
        advertiser_id="adv-1",
        client=_Client(),
    )


def test_inherit_historical_exclusions_batches_all_posts_and_groups_spus(
    monkeypatch,
) -> None:
    creatives = [
        ("creative-shared", "product-a"),
        ("creative-shared", "product-b"),
        *[
            (f"creative-{index}", f"product-{index % 3}")
            for index in range(800)
        ],
    ]
    batch_sizes: list[int] = []
    requests = []

    monkeypatch.setattr(
        router_provider,
        "_load_historical_removed_creatives",
        lambda *_args, **_kwargs: creatives,
    )

    async def _fake_call(_method, request):
        requests.append(request)
        batch_sizes.append(len(request.body.item_list))
        return _Response(len(batch_sizes))

    monkeypatch.setattr(router_provider, "_call_tiktok", _fake_call)
    db = _AuditDb()

    result = asyncio.run(
        router_provider._inherit_historical_creative_exclusions(
            _context(db),
            campaign_id="campaign-new",
            store_id="store-1",
            item_group_ids=["product-a", "product-b"],
        )
    )

    assert result["excluded"] == 801
    assert result["batches"] == 3
    assert batch_sizes == [400, 400, 1]
    shared = next(
        item
        for request in requests
        for item in request.body.item_list
        if item.item_id == "creative-shared"
    )
    assert shared.spu_id_list == ["product-a", "product-b"]
    assert db.audit_params[0]["result"] == "SUCCESS"


def test_inherit_historical_exclusion_batch_failure_is_audited_and_raised(
    monkeypatch,
) -> None:
    creatives = [(f"creative-{index}", "product-1") for index in range(401)]
    batch_sizes: list[int] = []

    monkeypatch.setattr(
        router_provider,
        "_load_historical_removed_creatives",
        lambda *_args, **_kwargs: creatives,
    )

    async def _fake_call(_method, request):
        batch_sizes.append(len(request.body.item_list))
        if len(batch_sizes) == 1:
            raise RuntimeError("poison batch")
        return _Response(len(batch_sizes))

    monkeypatch.setattr(router_provider, "_call_tiktok", _fake_call)
    db = _AuditDb()

    with pytest.raises(RuntimeError, match="historical exclusion"):
        asyncio.run(
            router_provider._inherit_historical_creative_exclusions(
                _context(db),
                campaign_id="campaign-new",
                store_id="store-1",
                item_group_ids=["product-1"],
            )
        )

    assert batch_sizes == [400, 1]
    assert db.audit_params[0]["result"] == "FAILED"
    response_payload = db.audit_params[0]["response_json"]
    assert response_payload["excluded"] == 1
    assert response_payload["failed_batches"][0]["error"] == "poison batch"
    assert db.commits == 1


def test_inherit_historical_exclusions_refuses_more_than_10000_posts(
    monkeypatch,
) -> None:
    creatives = [(f"creative-{index}", "product-1") for index in range(10_001)]
    calls = 0

    monkeypatch.setattr(
        router_provider,
        "_load_historical_removed_creatives",
        lambda *_args, **_kwargs: creatives,
    )

    async def _fake_call(_method, _request):
        nonlocal calls
        calls += 1
        return _Response(calls)

    monkeypatch.setattr(router_provider, "_call_tiktok", _fake_call)
    db = _AuditDb()

    with pytest.raises(RuntimeError, match="10,000-post"):
        asyncio.run(
            router_provider._inherit_historical_creative_exclusions(
                _context(db),
                campaign_id="campaign-new",
                store_id="store-1",
                item_group_ids=["product-1"],
            )
        )

    assert calls == 0
    assert db.audit_params[0]["result"] == "FAILED"
    assert db.commits == 1


def test_pending_manual_uploads_rotate_beyond_oldest_twelve(monkeypatch) -> None:
    class _QueueDb:
        def __init__(self):
            self.clock = 100
            self.commits = 0
            self.selected_batches: list[list[int]] = []
            self.rows = {
                index: {
                    "id": index,
                    "upload_id": f"upload-{index}",
                    "tiktok_account_id": 5,
                    "tiktok_business_id": "business-1",
                    "tiktok_item_id": f"item-{index}",
                    "item_group_id": None,
                    "upload_status": "PUBLISHED_WAITING_GMV",
                    "raw_json": {},
                    "updated_at": 0,
                }
                for index in range(1, 14)
            }

        def execute(self, statement, params):
            sql = " ".join(str(statement).split())
            if "select * from gmvmax_manual_creative_uploads" in sql:
                selected = sorted(
                    self.rows.values(),
                    key=lambda row: (row["updated_at"], row["id"]),
                )[:12]
                self.selected_batches.append([int(row["id"]) for row in selected])
                return _Result(rows=[dict(row) for row in selected])
            if (
                "update gmvmax_manual_creative_uploads" in sql
                and "set raw_json=cast(:raw_json as json)" in sql
            ):
                self.clock += 1
                row = self.rows[int(params["id"])]
                row["raw_json"] = json.loads(params["raw_json"])
                row["updated_at"] = self.clock
                return None
            raise AssertionError(f"unexpected SQL: {sql}")

        def commit(self):
            self.commits += 1

    class _AccountClient:
        async def aclose(self):
            return None

    async def _fresh_token(_db, _account_id):
        return (
            "token",
            SimpleNamespace(workspace_id=7, status="active", open_id="business-1"),
            None,
        )

    monkeypatch.setattr(
        router_provider,
        "get_fresh_tiktok_account_token_plain",
        _fresh_token,
    )
    monkeypatch.setattr(
        router_provider,
        "TikTokBusinessGMVMaxClient",
        lambda **_kwargs: _AccountClient(),
    )
    monkeypatch.setattr(
        router_provider,
        "resolve_creative_asset_store_authorized_bc_id",
        lambda *_args, **_kwargs: None,
    )

    db = _QueueDb()
    context = SimpleNamespace(
        db=db,
        binding=SimpleNamespace(bc_id=None),
        client=SimpleNamespace(),
    )
    first = asyncio.run(
        router_provider._refresh_pending_manual_uploads(
            context,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
        )
    )
    second = asyncio.run(
        router_provider._refresh_pending_manual_uploads(
            context,
            workspace_id=7,
            auth_id=11,
            advertiser_id="adv-1",
            store_id="store-1",
        )
    )

    assert first == {"checked": 12, "ready": 0, "failed": 0, "pending": 12}
    assert second == {"checked": 12, "ready": 0, "failed": 0, "pending": 12}
    assert db.selected_batches[0] == list(range(1, 13))
    assert 13 in db.selected_batches[1]
    assert db.rows[13]["raw_json"]["refresh_attempts"] == 1
    assert all(
        int(db.rows[index]["raw_json"].get("refresh_attempts") or 0) >= 1
        for index in range(1, 14)
    )
    assert db.commits == 2
