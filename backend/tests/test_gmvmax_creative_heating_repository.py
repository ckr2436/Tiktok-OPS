from __future__ import annotations

from datetime import datetime, timedelta, timezone

import asyncio
from sqlalchemy import event

from app.data.models.oauth_ttb import OAuthAccountTTB, OAuthProviderApp
from app.data.models.ttb_gmvmax import TTBGmvMaxCreativeHeating
from app.data.models.workspaces import Workspace
from app.data.repositories.tiktok_business.gmvmax_heating import (
    get_heating_for_creative,
    list_active_heating_configs,
    list_heating_configs,
    update_heating_action_result,
    update_heating_evaluation,
    upsert_creative_heating,
)


_PROVIDER = "tiktok-business"
_CAMPAIGN_ID = "cmp-1"
_CREATIVE_ID = "cr-1"
_HEATING_ID_SEQ = 1


@event.listens_for(TTBGmvMaxCreativeHeating, "before_insert")
def _assign_heating_id(mapper, connection, target) -> None:  # pragma: no cover - sqlite helper
    global _HEATING_ID_SEQ
    if target.id is not None:
        return
    target.id = _HEATING_ID_SEQ
    _HEATING_ID_SEQ += 1


def _setup_workspace_and_account(db_session):
    global _HEATING_ID_SEQ
    _HEATING_ID_SEQ = 1
    workspace = Workspace(id=1, name="Tenant", company_code="0001")
    db_session.add(workspace)
    db_session.flush()

    provider_app = OAuthProviderApp(
        id=1,
        provider=_PROVIDER,
        name="Provider",
        client_id="client",
        client_secret_cipher=b"secret",
        redirect_uri="https://example.com/callback",
    )
    db_session.add(provider_app)
    db_session.flush()

    account = OAuthAccountTTB(
        id=1,
        workspace_id=workspace.id,
        provider_app_id=provider_app.id,
        alias="Account",
        access_token_cipher=b"cipher",
        token_fingerprint=b"f" * 32,
    )
    db_session.add(account)
    db_session.flush()

    return workspace.id, account.id


def test_upsert_creative_heating_insert_and_update(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)

    first = asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
            mode="BOOST",
            target_daily_budget=150.5,
            currency="USD",
            max_duration_minutes=120,
            note="Initial boost",
            evaluation_window_minutes=90,
            min_clicks=40,
            min_ctr=0.02,
            auto_stop_enabled=True,
        )
    )
    db_session.flush()

    assert first.status == "PENDING"
    assert first.mode == "BOOST"
    assert float(first.target_daily_budget) == 150.5
    assert first.currency == "USD"
    assert first.max_duration_minutes == 120
    assert first.note == "Initial boost"
    assert first.evaluation_window_minutes == 90
    assert first.min_clicks == 40
    assert float(first.min_ctr) == 0.02
    assert first.auto_stop_enabled is True
    assert first.is_heating_active is False

    updated = asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
            mode="SET_BUDGET",
            target_daily_budget=200,
            budget_delta=25,
            currency="USD",
            max_duration_minutes=90,
            note="Adjusted",
            creative_name="Hero Video",
            auto_stop_enabled=False,
        )
    )
    db_session.flush()

    assert updated is first
    assert updated.mode == "SET_BUDGET"
    assert float(updated.target_daily_budget) == 200.0
    assert float(updated.budget_delta) == 25.0
    assert updated.max_duration_minutes == 90
    assert updated.note == "Adjusted"
    assert updated.creative_name == "Hero Video"
    assert updated.auto_stop_enabled is False


def test_update_heating_action_result(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)
    row = asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
        )
    )
    db_session.flush()

    timestamp = datetime.now(tz=timezone.utc)
    updated = asyncio.run(
        update_heating_action_result(
            db_session,
            heating_id=row.id,
            status="APPLIED",
            action_type="APPLY_BOOST",
            action_time=timestamp,
            request_payload={"creative_id": _CREATIVE_ID},
            response_payload={"result": "OK"},
            error_message=None,
        )
    )
    db_session.flush()

    assert updated.status == "APPLIED"
    assert updated.last_action_type == "APPLY_BOOST"
    assert updated.last_action_time == timestamp
    assert updated.last_action_request == {"creative_id": _CREATIVE_ID}
    assert updated.last_action_response == {"result": "OK"}
    assert updated.last_error is None
    assert updated.is_heating_active is True


def test_list_and_get_heating_configs(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)

    base_time = datetime(2024, 1, 1, tzinfo=timezone.utc)

    asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-1",
            mode="BOOST",
        )
    )
    asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-2",
            mode="BOOST",
        )
    )
    second = asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id="cmp-2",
            creative_id="cr-3",
            mode="BOOST",
        )
    )
    db_session.flush()

    asyncio.run(
        update_heating_action_result(
            db_session,
            heating_id=second.id,
            status="FAILED",
            action_type="APPLY_BOOST",
            action_time=base_time + timedelta(minutes=5),
            error_message="error",
        )
    )
    db_session.flush()

    listed = asyncio.run(
        list_heating_configs(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_ids=["cr-1", "cr-2"],
        )
    )
    assert len(listed) == 2
    assert all(row.campaign_id == _CAMPAIGN_ID for row in listed)

    single = asyncio.run(
        get_heating_for_creative(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id="cr-2",
        )
    )
    assert single is not None
    assert single.creative_id == "cr-2"


def test_active_heating_and_evaluation_updates(db_session):
    workspace_id, auth_id = _setup_workspace_and_account(db_session)
    row = asyncio.run(
        upsert_creative_heating(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
        )
    )
    db_session.flush()

    assert (
        asyncio.run(
            list_active_heating_configs(
                db_session,
                workspace_id=workspace_id,
                provider=_PROVIDER,
                auth_id=auth_id,
            )
        )
        == []
    )

    timestamp = datetime.now(tz=timezone.utc)
    asyncio.run(
        update_heating_action_result(
            db_session,
            heating_id=row.id,
            status="APPLIED",
            action_type="APPLY_BOOST",
            action_time=timestamp,
        )
    )
    db_session.flush()

    active_after = asyncio.run(
        list_active_heating_configs(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
        )
    )
    assert active_after and active_after[0].id == row.id

    evaluation_time = datetime.now(tz=timezone.utc)
    asyncio.run(
        update_heating_evaluation(
            db_session,
            heating_id=row.id,
            evaluated_at=evaluation_time,
            evaluation_result="ok",
            is_heating_active=True,
        )
    )
    db_session.flush()

    refreshed = asyncio.run(
        get_heating_for_creative(
            db_session,
            workspace_id=workspace_id,
            provider=_PROVIDER,
            auth_id=auth_id,
            campaign_id=_CAMPAIGN_ID,
            creative_id=_CREATIVE_ID,
        )
    )
    assert refreshed is not None
    assert refreshed.last_evaluation_result == "ok"
    assert refreshed.last_evaluated_at == evaluation_time
