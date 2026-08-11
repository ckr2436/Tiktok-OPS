"""register self-hosted Doubao Seedance provider route

Revision ID: 0122_doubao_seedance_provider
Revises: 0121_flow_account_proxy_pool
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0122_doubao_seedance_provider"
down_revision = "0121_flow_account_proxy_pool"
branch_labels = None
depends_on = None

KEY_NAME = "Doubao Seedance Self-hosted Pool"


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    keys = sa.Table("kie_api_keys", metadata, autoload_with=bind)
    routes = sa.Table("ai_model_routes", metadata, autoload_with=bind)
    settings = sa.Table("ai_provider_model_settings", metadata, autoload_with=bind)

    key_id = bind.execute(
        sa.select(keys.c.id).where(keys.c.name == KEY_NAME)
    ).scalar_one_or_none()
    if key_id is None:
        result = bind.execute(
            keys.insert().values(
                name=KEY_NAME,
                provider_key="doubao",
                # This is a non-secret adapter marker. Browser credentials are
                # encrypted separately on their isolated account records.
                api_key_ciphertext="local:encrypted-browser-account-pool",
                is_active=True,
                is_default=False,
                scopes_json=["video:generate", "video:seedance_2_0_mini"],
                model_priorities_json={"seedance_2_0_mini": 1},
            )
        )
        key_id = int(result.inserted_primary_key[0])

    setting_id = bind.execute(
        sa.select(settings.c.id).where(
            settings.c.provider_key == "doubao",
            settings.c.model_id == "seedance_2_0_mini",
        )
    ).scalar_one_or_none()
    if setting_id is None:
        bind.execute(
            settings.insert().values(
                provider_key="doubao",
                model_id="seedance_2_0_mini",
                is_enabled=True,
            )
        )

    route_id = bind.execute(
        sa.select(routes.c.id).where(
            routes.c.key_id == int(key_id),
            routes.c.workload == "default",
            routes.c.logical_model_id == "seedance_2_0_mini",
            routes.c.provider_model_id == "seedance_2_0_mini",
            routes.c.capability == "video",
        )
    ).scalar_one_or_none()
    if route_id is None:
        bind.execute(
            routes.insert().values(
                key_id=int(key_id),
                provider_key="doubao",
                workload="default",
                logical_model_id="seedance_2_0_mini",
                provider_model_id="seedance_2_0_mini",
                capability="video",
                adapter_type="doubao_account_pool",
                priority=1,
                is_enabled=True,
                is_verified=True,
                health_status="HEALTHY",
                consecutive_failures=0,
                total_successes=0,
                total_failures=0,
                config_json={
                    "transport": "doubao_browser_account_pool",
                    "video_capabilities": {
                        "aspect_ratios": ["9:16", "16:9", "1:1"],
                        "reference_video": False,
                        "reference_modes": ["reference"],
                        "reference_image_counts": [0],
                        "durations": list(range(4, 16)),
                        "resolutions": ["720p"],
                    },
                },
            )
        )


def downgrade() -> None:
    # Never delete a task-referenced provider identity during rollback. Disable
    # the route and key so historical task ownership remains intact.
    bind = op.get_bind()
    metadata = sa.MetaData()
    keys = sa.Table("kie_api_keys", metadata, autoload_with=bind)
    routes = sa.Table("ai_model_routes", metadata, autoload_with=bind)
    key_ids = list(
        bind.execute(sa.select(keys.c.id).where(keys.c.name == KEY_NAME)).scalars()
    )
    if key_ids:
        bind.execute(
            routes.update().where(routes.c.key_id.in_(key_ids)).values(is_enabled=False)
        )
        bind.execute(
            keys.update().where(keys.c.id.in_(key_ids)).values(is_active=False)
        )
