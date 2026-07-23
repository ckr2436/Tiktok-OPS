from __future__ import annotations

from datetime import datetime
import importlib

import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _legacy_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "oauth_accounts_ttb",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
    )
    campaigns = sa.Table(
        "website_ads_campaigns",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
    )
    ads = sa.Table(
        "website_ads_ads",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("campaign_local_id", sa.BigInteger(), nullable=False),
    )
    actions = sa.Table(
        "website_ads_action_logs",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("ad_local_id", sa.BigInteger(), nullable=True),
        sa.Column("actor_type", sa.String(32), nullable=False),
        sa.Column("action", sa.String(64), nullable=False),
        sa.Column("result", sa.String(32), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
    )
    metadata.create_all(engine)
    created_at = datetime(2026, 7, 17, 10, 0, 0)
    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "insert into oauth_accounts_ttb (id) values "
                "(11), (22), (31), (32)"
            )
        )
        connection.execute(
            campaigns.insert(),
            [
                {"id": 1, "workspace_id": 1, "auth_id": 11},
                {"id": 2, "workspace_id": 2, "auth_id": 22},
                {"id": 3, "workspace_id": 2, "auth_id": 22},
                {"id": 4, "workspace_id": 3, "auth_id": 31},
                {"id": 5, "workspace_id": 3, "auth_id": 32},
            ],
        )
        connection.execute(
            ads.insert(),
            [
                {"id": 101, "campaign_local_id": 1},
                {"id": 301, "campaign_local_id": 4},
            ],
        )
        connection.execute(
            actions.insert(),
            [
                {
                    "id": 1,
                    "workspace_id": 1,
                    "ad_local_id": 101,
                    "actor_type": "LEGACY",
                    "action": "PAUSE_AD",
                    "result": "SUCCESS",
                    "created_at": created_at,
                },
                {
                    "id": 2,
                    "workspace_id": 2,
                    "ad_local_id": None,
                    "actor_type": "LEGACY",
                    "action": "PAUSE_CAMPAIGN",
                    "result": "SUCCESS",
                    "created_at": created_at,
                },
                {
                    "id": 3,
                    "workspace_id": 3,
                    "ad_local_id": None,
                    "actor_type": "LEGACY",
                    "action": "PAUSE_CAMPAIGN",
                    "result": "SUCCESS",
                    "created_at": created_at,
                },
                {
                    "id": 4,
                    "workspace_id": 4,
                    "ad_local_id": None,
                    "actor_type": "LEGACY",
                    "action": "PAUSE_CAMPAIGN",
                    "result": "SUCCESS",
                    "created_at": created_at,
                },
                {
                    "id": 5,
                    "workspace_id": 3,
                    "ad_local_id": 301,
                    "actor_type": "LEGACY",
                    "action": "PAUSE_AD",
                    "result": "SUCCESS",
                    "created_at": created_at,
                },
            ],
        )


def test_action_auth_migration_backfills_only_exact_or_unambiguous_scopes(
    tmp_path,
    monkeypatch,
):
    migration = importlib.import_module(
        "migrations.versions.0098_website_ads_action_auth"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'action-auth.db'}")
    _legacy_schema(engine)

    with engine.begin() as connection:
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()

    inspector = sa.inspect(engine)
    assert "auth_id" in {
        str(column["name"])
        for column in inspector.get_columns("website_ads_action_logs")
    }
    assert any(
        index.get("name") == "idx_web_ads_action_auth_scope"
        and list(index.get("column_names") or [])
        == ["workspace_id", "auth_id", "created_at"]
        for index in inspector.get_indexes("website_ads_action_logs")
    )
    assert any(
        foreign_key.get("name") == "fk_web_ads_action_auth"
        and list(foreign_key.get("constrained_columns") or []) == ["auth_id"]
        for foreign_key in inspector.get_foreign_keys("website_ads_action_logs")
    )

    with engine.connect() as connection:
        rows = dict(
            connection.execute(
                sa.text(
                    "select id, auth_id from website_ads_action_logs order by id"
                )
            ).all()
        )
    assert rows == {
        1: 11,    # exact ad -> campaign
        2: 22,    # unique auth in the workspace
        3: None,  # ambiguous multi-auth workspace
        4: None,  # no attributable campaign
        5: 31,    # exact ad wins even in a multi-auth workspace
    }
