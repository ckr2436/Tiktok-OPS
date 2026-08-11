from __future__ import annotations

import importlib

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations


def _legacy_schema(engine: sa.Engine) -> None:
    metadata = sa.MetaData()
    sa.Table(
        "website_ads_campaigns",
        metadata,
        sa.Column("id", sa.BigInteger(), primary_key=True),
        sa.Column("workspace_id", sa.BigInteger(), nullable=False),
        sa.Column("auth_id", sa.BigInteger(), nullable=False),
        sa.Column("request_key", sa.String(64), nullable=False),
        sa.UniqueConstraint(
            "workspace_id",
            "request_key",
            name="uk_web_ads_campaign_request",
        ),
    )
    metadata.create_all(engine)


def test_request_key_migration_scopes_uniqueness_by_auth(tmp_path, monkeypatch):
    migration = importlib.import_module(
        "migrations.versions.0099_website_ads_request_scope"
    )
    engine = sa.create_engine(f"sqlite:///{tmp_path / 'request-scope.db'}")
    _legacy_schema(engine)

    with engine.begin() as connection:
        connection.execute(
            sa.text(
                "insert into website_ads_campaigns "
                "(id, workspace_id, auth_id, request_key) "
                "values (1, 1, 11, 'shared-key')"
            )
        )
        monkeypatch.setattr(
            migration,
            "op",
            Operations(MigrationContext.configure(connection)),
        )
        migration.upgrade()
        connection.execute(
            sa.text(
                "insert into website_ads_campaigns "
                "(id, workspace_id, auth_id, request_key) "
                "values (2, 1, 22, 'shared-key')"
            )
        )
        with pytest.raises(sa.exc.IntegrityError):
            connection.execute(
                sa.text(
                    "insert into website_ads_campaigns "
                    "(id, workspace_id, auth_id, request_key) "
                    "values (3, 1, 11, 'shared-key')"
                )
            )

    constraints = {
        constraint.get("name"): list(constraint.get("column_names") or [])
        for constraint in sa.inspect(engine).get_unique_constraints(
            "website_ads_campaigns"
        )
    }
    assert constraints["uk_web_ads_campaign_request"] == [
        "workspace_id",
        "auth_id",
        "request_key",
    ]
