from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
import pathlib
import sys
from typing import Iterator

import pytest
from fastapi import APIRouter, Depends, FastAPI, Query
from fastapi.testclient import TestClient

BASE_DIR = pathlib.Path(__file__).resolve().parents[1]
if str(BASE_DIR) not in sys.path:
    sys.path.append(str(BASE_DIR))

os.environ.setdefault("DATABASE_URL", "sqlite:///./test.db")

from app.core.deps import SessionUser, require_platform_admin, require_tenant_member
from app.core.config import settings
from app.core.errors import APIError, install_exception_handlers
from app.data.db import get_db
from app.features.platform.router_task_configs import router as platform_task_configs_router
from app.services import platform_tasks
from sqlalchemy import text
from sqlalchemy.orm import Session

from app.data.db import Base, SessionLocal, engine
from app.data.models.platform_tasks import (
    PlatformTaskCatalog,
    PlatformTaskConfig,
    PlatformTaskRun,
    PlatformTaskRunWorkspace,
    TenantSyncJob,
    WorkspaceTag,
    RateLimitToken,
    IdempotencyKey,
)
from app.data.models.workspaces import Workspace


test_app = FastAPI()
install_exception_handlers(test_app)
test_app.include_router(platform_task_configs_router)

tenant_router = APIRouter(
    prefix=f"{settings.API_PREFIX}/tenants" + "/{workspace_id}/oauth/{provider}/bindings/{auth_id}/sync",
    tags=["tenant.sync"],
)


def _iso(dt: datetime | None) -> str | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    else:
        dt = dt.astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat().replace("+00:00", "Z")


@tenant_router.get("/last")
def last_sync_endpoint(
    workspace_id: int,
    provider: str,
    auth_id: int,
    kind: str = Query(default="products"),
    _: SessionUser = Depends(require_tenant_member),
    db: Session = Depends(get_db),
):
    job = platform_tasks.get_last_sync_job(
        db,
        workspace_id=workspace_id,
        provider=provider,
        auth_id=auth_id,
        kind=kind,
    )
    if not job:
        raise APIError("JOB_NOT_FOUND", "No sync history", 404)
    return {
        "status": job.status,
        "triggered_at": _iso(job.triggered_at),
        "finished_at": _iso(job.finished_at),
        "duration_sec": job.duration_sec,
        "summary": job.summary,
        "next_allowed_at": _iso(job.next_allowed_at),
    }


test_app.include_router(tenant_router)


@pytest.fixture(autouse=True)
def setup_database() -> Iterator[None]:
    tables = [
        PlatformTaskCatalog.__table__,
        PlatformTaskConfig.__table__,
        WorkspaceTag.__table__,
        PlatformTaskRun.__table__,
        PlatformTaskRunWorkspace.__table__,
        TenantSyncJob.__table__,
        RateLimitToken.__table__,
        IdempotencyKey.__table__,
    ]

    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS platform_task_catalog"))
        conn.execute(text("DROP TABLE IF EXISTS platform_task_config"))
        conn.execute(text("DROP TABLE IF EXISTS workspace_tags"))
        conn.execute(text("DROP TABLE IF EXISTS platform_task_run_workspace"))
        conn.execute(text("DROP TABLE IF EXISTS platform_task_run"))
        conn.execute(text("DROP TABLE IF EXISTS tenant_sync_jobs"))
        conn.execute(text("DROP TABLE IF EXISTS rate_limit_tokens"))
        conn.execute(text("DROP TABLE IF EXISTS idempotency_keys"))
        conn.execute(text("DROP TABLE IF EXISTS workspaces"))
        conn.execute(
            text(
                """
                CREATE TABLE workspaces (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name VARCHAR(128) NOT NULL,
                    company_code VARCHAR(4) NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    updated_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL
                )
                """
            )
        )

    Base.metadata.create_all(engine, tables=tables)
    try:
        yield
    finally:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS platform_task_catalog"))
            conn.execute(text("DROP TABLE IF EXISTS platform_task_config"))
            conn.execute(text("DROP TABLE IF EXISTS workspace_tags"))
            conn.execute(text("DROP TABLE IF EXISTS platform_task_run_workspace"))
            conn.execute(text("DROP TABLE IF EXISTS platform_task_run"))
            conn.execute(text("DROP TABLE IF EXISTS tenant_sync_jobs"))
            conn.execute(text("DROP TABLE IF EXISTS rate_limit_tokens"))
            conn.execute(text("DROP TABLE IF EXISTS idempotency_keys"))
            conn.execute(text("DROP TABLE IF EXISTS workspaces"))


@pytest.fixture()
def db_session() -> Iterator[Session]:
    session: Session = SessionLocal()
    try:
        yield session
    finally:
        session.close()


def _make_platform_user(workspace_id: int = 1) -> SessionUser:
    return SessionUser(
        id=1,
        email="admin@example.com",
        username="admin",
        display_name="Admin",
        usercode="000000001",
        is_platform_admin=True,
        workspace_id=workspace_id,
        role="owner",
        is_active=True,
    )


@pytest.fixture()
def client(db_session: Session) -> Iterator[TestClient]:
    test_app.dependency_overrides[require_platform_admin] = lambda: _make_platform_user()

    def _tenant_member(workspace_id: int) -> SessionUser:
        return _make_platform_user(workspace_id)

    test_app.dependency_overrides[require_tenant_member] = _tenant_member
    with TestClient(test_app) as test_client:
        yield test_client
    test_app.dependency_overrides.clear()


def seed_data(session: Session, task_key: str = "bc_ads_shop_product_ingest") -> tuple[int, int, int]:
    ws1 = Workspace(name="Workspace A", company_code="0001")
    ws2 = Workspace(name="Workspace B", company_code="0002")
    ws3 = Workspace(name="Workspace C", company_code="0003")
    session.add_all([ws1, ws2, ws3])
    session.flush()

    session.add_all(
        [
            WorkspaceTag(id=1, workspace_id=ws1.id, tag="tier_a"),
            WorkspaceTag(id=2, workspace_id=ws2.id, tag="tier_b"),
        ]
    )

    catalog = PlatformTaskCatalog(
        task_key=task_key,
        title="BC Ads · Shop Products Pull",
        description="Pull shop products",
        visibility="platform",
        defaults_json={
            "rate_limit": {"per_workspace_min_interval_sec": 3600},
            "schedule": {"mode": "interval", "interval_sec": 7200, "timezone": "UTC"},
        },
        supports_whitelist=True,
        supports_blacklist=True,
        supports_tags=True,
    )
    catalog.id = 1
    session.add(catalog)

    config = PlatformTaskConfig(
        task_key=task_key,
        is_enabled=True,
        schedule_mode="interval",
        schedule_interval_sec=7200,
        schedule_timezone="UTC",
        rate_limit_per_workspace_min_interval_sec=3600,
        rate_limit_global_concurrency=5,
        rate_limit_per_workspace_concurrency=1,
        targeting_whitelist_workspace_ids=[ws1.id],
        targeting_blacklist_workspace_ids=[],
        targeting_include_tags=["tier_a"],
        input_payload={},
        version=4,
        updated_by="admin@example.com",
        updated_by_user_id=1,
    )
    session.add(config)

    started = datetime(2025, 10, 22, 0, 0, 1, tzinfo=timezone.utc)
    finished = started + timedelta(minutes=9, seconds=39)
    run = PlatformTaskRun(
        run_id="run-1",
        task_key=task_key,
        status="SUCCESS",
        summary="Processed 2 workspaces",
        stats_json={"workspaces_total": 2, "ok": 2, "failed": 0, "skipped": 0},
        started_at=started,
        finished_at=finished,
        duration_sec=int((finished - started).total_seconds()),
    )
    session.add(run)
    session.flush()

    session.add_all(
        [
            PlatformTaskRunWorkspace(
                id=1,
                run_id=run.run_id,
                workspace_id=ws1.id,
                status="SUCCESS",
                count=245,
            ),
            PlatformTaskRunWorkspace(
                id=2,
                run_id=run.run_id,
                workspace_id=ws2.id,
                status="FAILED",
                error_code="TTB_OAUTH_EXPIRED",
            ),
        ]
    )

    session.add(
        TenantSyncJob(
            job_id="job-1",
            workspace_id=ws1.id,
            provider="tiktok-business",
            auth_id=101,
            kind="products",
            status="SUCCESS",
            summary="Imported 128 products",
            triggered_at=datetime(2025, 10, 22, 1, 10, tzinfo=timezone.utc),
            finished_at=datetime(2025, 10, 22, 1, 10, 45, tzinfo=timezone.utc),
            next_allowed_at=datetime(2025, 10, 22, 1, 12, tzinfo=timezone.utc),
        )
    )

    session.commit()
    return ws1.id, ws2.id, ws3.id


def test_get_task_catalog(client: TestClient, db_session: Session) -> None:
    seed_data(db_session)
    resp = client.get("/api/v1/platform/tasks/catalog")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["items"]) == 1
    item = body["items"][0]
    assert item["task_key"] == "bc_ads_shop_product_ingest"
    assert item["status"]["is_enabled"] is True
    assert item["status"]["last_run_at"].startswith("2025-10-22T00:09:40")


def test_get_task_config(client: TestClient, db_session: Session) -> None:
    seed_data(db_session)
    resp = client.get("/api/v1/platform/tasks/bc_ads_shop_product_ingest/config")
    assert resp.status_code == 200
    data = resp.json()
    assert data["task_key"] == "bc_ads_shop_product_ingest"
    assert data["is_enabled"] is True
    assert data["schedule"]["interval_sec"] == 7200
    assert data["rate_limit"]["per_workspace_min_interval_sec"] == 3600


def test_update_task_config_with_idempotency(client: TestClient, db_session: Session) -> None:
    wid1, _, _ = seed_data(db_session)
    payload = {
        "is_enabled": True,
        "schedule": {
            "mode": "interval",
            "interval_sec": 3600,
            "timezone": "UTC",
        },
        "rate_limit": {
            "per_workspace_min_interval_sec": 3600,
            "global_concurrency": 10,
            "per_workspace_concurrency": 2,
        },
        "targeting": {
            "whitelist_workspace_ids": [wid1],
            "blacklist_workspace_ids": [],
            "include_tags": ["tier_a"],
            "exclude_tags": [],
        },
        "input": {},
    }
    resp = client.put(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/config",
        json=payload,
        headers={"Idempotency-Key": "config-update-1"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["ok"] is True
    assert data["version"] == 5
    assert data["target_count"] >= 1

    # Reuse idempotency key
    resp_dup = client.put(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/config",
        json=payload,
        headers={"Idempotency-Key": "config-update-1"},
    )
    # TODO: Keep this regression check to ensure SQLite id generation doesn't break idempotent reuse.
    assert resp_dup.status_code == 200
    assert resp_dup.json() == data


def test_update_task_config_dry_run(client: TestClient, db_session: Session) -> None:
    wid1, _, _ = seed_data(db_session)
    payload = {
        "is_enabled": False,
        "schedule": {
            "mode": "interval",
            "interval_sec": 3600,
            "timezone": "UTC",
        },
        "rate_limit": {
            "per_workspace_min_interval_sec": 3600,
        },
        "targeting": {
            "whitelist_workspace_ids": [wid1],
            "blacklist_workspace_ids": [],
            "include_tags": [],
            "exclude_tags": [],
        },
        "input": {},
    }
    resp = client.put(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/config",
        params={"dry_run": "true"},
        json=payload,
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["dry_run"] is True
    assert data["version"] == 4


def test_update_task_config_conflict_lists(client: TestClient, db_session: Session) -> None:
    wid1, _, _ = seed_data(db_session)
    payload = {
        "is_enabled": True,
        "schedule": {
            "mode": "interval",
            "interval_sec": 3600,
            "timezone": "UTC",
        },
        "rate_limit": {
            "per_workspace_min_interval_sec": 3600,
        },
        "targeting": {
            "whitelist_workspace_ids": [wid1],
            "blacklist_workspace_ids": [wid1],
            "include_tags": [],
            "exclude_tags": [],
        },
        "input": {},
    }
    resp = client.put(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/config",
        json=payload,
    )
    # TODO: Preserve LIST_CONFLICT semantics if targeting validation rules evolve.
    assert resp.status_code == 409
    body = resp.json()
    assert body["error"]["code"] == "LIST_CONFLICT"


def test_get_last_run(client: TestClient, db_session: Session) -> None:
    seed_data(db_session)
    resp = client.get("/api/v1/platform/tasks/bc_ads_shop_product_ingest/last_run")
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "SUCCESS"
    assert len(data["workspace_samples"]) == 2


def test_list_runs_with_pagination(client: TestClient, db_session: Session) -> None:
    seed_data(db_session)
    second_run = PlatformTaskRun(
        run_id="run-0",
        task_key="bc_ads_shop_product_ingest",
        status="FAILED",
        started_at=datetime(2025, 10, 21, 23, 50, tzinfo=timezone.utc),
        finished_at=datetime(2025, 10, 21, 23, 55, tzinfo=timezone.utc),
        duration_sec=300,
    )
    db_session.add(second_run)
    db_session.commit()

    resp = client.get(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/runs",
        params={"limit": 1},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert len(data["items"]) == 1
    cursor = data["next_cursor"]
    assert cursor

    resp_next = client.get(
        "/api/v1/platform/tasks/bc_ads_shop_product_ingest/runs",
        params={"cursor": cursor},
    )
    assert resp_next.status_code == 200
    data_next = resp_next.json()
    assert len(data_next["items"]) >= 1


def test_get_last_sync_job(client: TestClient, db_session: Session) -> None:
    wid1, _, _ = seed_data(db_session)
    resp = client.get(
        f"/api/v1/tenants/{wid1}/oauth/tiktok-business/bindings/101/sync/last",
        params={"kind": "products"},
    )
    assert resp.status_code == 200
    data = resp.json()
    # TODO: Ensure duration calculations stay aligned with TenantSyncJobView outputs.
    assert data["status"] == "SUCCESS"
    assert data["summary"] == "Imported 128 products"

