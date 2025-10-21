import asyncio
import json
import pathlib
import sys
import types
from typing import Any, Dict, Iterable, List, Optional

sys.path.append(str(pathlib.Path(__file__).resolve().parents[1]))

_cryptography = types.ModuleType("cryptography")
_hazmat = types.ModuleType("cryptography.hazmat")
_primitives = types.ModuleType("cryptography.hazmat.primitives")
_ciphers = types.ModuleType("cryptography.hazmat.primitives.ciphers")
_aead = types.ModuleType("cryptography.hazmat.primitives.ciphers.aead")


class _DummyAESGCM:
    def __init__(self, *args: Any, **kwargs: Any) -> None:  # pragma: no cover - simple stub
        pass

    def encrypt(self, *args: Any, **kwargs: Any) -> bytes:  # pragma: no cover - simple stub
        return b""

    def decrypt(self, *args: Any, **kwargs: Any) -> bytes:  # pragma: no cover - simple stub
        return b""


_aead.AESGCM = _DummyAESGCM
_ciphers.aead = _aead
_primitives.ciphers = _ciphers
_hazmat.primitives = _primitives
_cryptography.hazmat = _hazmat

sys.modules.setdefault("cryptography", _cryptography)
sys.modules.setdefault("cryptography.hazmat", _hazmat)
sys.modules.setdefault("cryptography.hazmat.primitives", _primitives)
sys.modules.setdefault("cryptography.hazmat.primitives.ciphers", _ciphers)
sys.modules.setdefault("cryptography.hazmat.primitives.ciphers.aead", _aead)

import httpx
import pytest
from app.services.ttb_api import TTBApiClient
from app.services.ttb_sync import TTBSyncService


class _DummyAsyncClient:
    def __init__(self, responses: List[Dict[str, Any]], *args: Any, **kwargs: Any) -> None:
        self._responses = list(responses)
        self.requests: List[Dict[str, Any]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Optional[Dict[str, Any]] = None,
        json: Optional[Dict[str, Any]] = None,
    ) -> httpx.Response:
        self.requests.append({
            "method": method,
            "url": url,
            "params": params or {},
            "json": json,
        })
        payload = self._responses.pop(0)
        return httpx.Response(status_code=payload["status"], json=payload["json"])

    async def aclose(self) -> None:
        return None


def test_iter_adgroups_builds_query_and_paginates(monkeypatch: pytest.MonkeyPatch) -> None:
    responses = [
        {
            "status": 200,
            "json": {
                "code": 0,
                "message": "OK",
                "data": {
                    "list": [
                        {
                            "adgroup_id": "A1",
                            "advertiser_id": "123",
                        }
                    ],
                    "page_info": {"page": 1, "page_size": 1, "total_number": 2, "has_more": True},
                },
            },
        },
        {
            "status": 200,
            "json": {
                "code": 0,
                "message": "OK",
                "data": {
                    "list": [
                        {
                            "adgroup_id": "A2",
                            "advertiser_id": "123",
                        }
                    ],
                    "page_info": {"page": 2, "page_size": 1, "total_number": 2, "has_more": False},
                },
            },
        },
    ]

    holder: Dict[str, _DummyAsyncClient] = {}

    def _factory(*args: Any, **kwargs: Any) -> _DummyAsyncClient:
        client = _DummyAsyncClient(responses, *args, **kwargs)
        holder["client"] = client
        return client

    monkeypatch.setattr("app.services.ttb_api.httpx.AsyncClient", _factory)

    async def _run() -> List[Dict[str, Any]]:
        client = TTBApiClient(access_token="token")
        items: List[Dict[str, Any]] = []
        async for item in client.iter_adgroups(
            advertiser_id="123",
            fields=["adgroup_id", "advertiser_id"],
            filtering={"campaign_ids": ["cmp1"]},
            exclude_field_types_in_response=["NULL_FIELD"],
            page_size=1,
        ):
            items.append(item)
        await client.aclose()
        return items

    items = asyncio.run(_run())

    assert [it["adgroup_id"] for it in items] == ["A1", "A2"]
    dummy = holder["client"]
    assert len(dummy.requests) == 2
    first = dummy.requests[0]["params"]
    assert first["advertiser_id"] == "123"
    assert first["page"] == 1
    assert first["page_size"] == 1
    assert json.loads(first["fields"]) == ["adgroup_id", "advertiser_id"]
    assert json.loads(first["filtering"]) == {"campaign_ids": ["cmp1"]}
    assert json.loads(first["exclude_field_types_in_response"]) == ["NULL_FIELD"]
    second = dummy.requests[1]["params"]
    assert second["page"] == 2


class _FakeTTBApiClient:
    def __init__(self, items: Iterable[Dict[str, Any]]) -> None:
        self.items = list(items)
        self.calls: List[Dict[str, Any]] = []

    async def iter_adgroups(
        self,
        *,
        advertiser_id: str,
        fields: Optional[Iterable[str]] = None,
        filtering: Optional[Dict[str, Any]] = None,
        exclude_field_types_in_response: Optional[Iterable[str]] = None,
        page_size: int = 100,
    ):
        self.calls.append(
            {
                "advertiser_id": advertiser_id,
                "fields": list(fields) if fields else None,
                "filtering": filtering,
                "exclude": list(exclude_field_types_in_response) if exclude_field_types_in_response else None,
                "page_size": page_size,
            }
        )
        for item in self.items:
            yield item


class _FakeQuery:
    def __init__(self, items: List[Any]):
        self._items = items

    def filter(self, *args: Any, **kwargs: Any) -> "_FakeQuery":
        return self

    def all(self) -> List[Any]:
        return list(self._items)

    def one_or_none(self) -> Any:
        items = self.all()
        return items[0] if items else None


class _FakeSession:
    def __init__(self, advertisers: List[Any]) -> None:
        self._advertisers = advertisers
        self.added: List[Any] = []

    def query(self, model: Any) -> _FakeQuery:
        if model.__name__ == "TTBAdvertiser":
            return _FakeQuery(self._advertisers)
        return _FakeQuery([])

    def add(self, obj: Any) -> None:
        self.added.append(obj)


def test_sync_adgroups_upserts_and_tracks_cursor(monkeypatch: pytest.MonkeyPatch) -> None:
    items = [
        {
            "adgroup_id": "A100",
            "advertiser_id": "123",
            "campaign_id": "C1",
            "adgroup_name": "Test Ad Group 1",
            "operation_status": "ENABLE",
            "primary_status": "STATUS_ENABLE",
            "secondary_status": "ADGROUP_STATUS_DELIVERING",
            "budget": 150.5,
            "budget_mode": "BUDGET_MODE_TOTAL",
            "optimization_goal": "CLICK",
            "promotion_type": "WEBSITE",
            "bid_type": "BID_TYPE_COST_CAP",
            "bid_strategy": "BID_STRATEGY_COST_CAP",
            "schedule_start_time": "2024-01-01 00:00:00",
            "schedule_end_time": "2024-02-01 00:00:00",
            "create_time": "2024-01-01 00:00:00",
            "modify_time": "2024-01-02 00:00:00",
        },
        {
            "adgroup_id": "A200",
            "advertiser_id": "123",
            "campaign_id": "C2",
            "adgroup_name": "Test Ad Group 2",
            "operation_status": "DISABLE",
            "primary_status": "STATUS_DISABLE",
            "secondary_status": "ADGROUP_STATUS_PENDING",
            "budget": 300,
            "budget_mode": "BUDGET_MODE_DAILY",
            "optimization_goal": "CONVERSION",
            "promotion_type": "APP",
            "bid_type": "BID_TYPE_BID_CAP",
            "bid_strategy": "BID_STRATEGY_BID_CAP",
            "schedule_start_time": "2024-03-01 00:00:00",
            "schedule_end_time": "2024-03-31 00:00:00",
            "create_time": "2024-03-01 00:00:00",
            "modify_time": "2024-03-05 00:00:00",
        },
    ]

    fake_client = _FakeTTBApiClient(items)
    fake_cursor = types.SimpleNamespace(last_rev=None)
    captured_upserts: List[Dict[str, Any]] = []

    monkeypatch.setattr(
        "app.services.ttb_sync._get_or_create_cursor",
        lambda db, workspace_id, auth_id, resource_type: fake_cursor,
    )

    def _fake_upsert(db: Any, *, workspace_id: int, auth_id: int, item: dict) -> None:
        captured_upserts.append({"workspace_id": workspace_id, "auth_id": auth_id, "item": item})

    monkeypatch.setattr("app.services.ttb_sync._upsert_adgroup", _fake_upsert)

    fake_session = _FakeSession([types.SimpleNamespace(advertiser_id="123")])
    service = TTBSyncService(fake_session, fake_client, workspace_id=1, auth_id=2)

    result = asyncio.run(
        service.sync_adgroups(
            limit=2,
            fields=["adgroup_id"],
            filtering={"primary_status": "STATUS_ALL"},
            exclude_field_types_in_response=["NULL_FIELD"],
        )
    )

    assert result["synced"] == 2
    assert fake_cursor.last_rev is not None
    assert fake_session.added[-1] is fake_cursor
    assert [rec["item"]["adgroup_id"] for rec in captured_upserts] == ["A100", "A200"]

    call = fake_client.calls[0]
    assert call["advertiser_id"] == "123"
    assert call["page_size"] == 2
    assert call["fields"] == ["adgroup_id"]
    assert call["filtering"] == {"primary_status": "STATUS_ALL"}
    assert call["exclude"] == ["NULL_FIELD"]
