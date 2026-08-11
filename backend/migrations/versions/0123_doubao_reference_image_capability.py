"""advertise verified Doubao single-reference Seedance capability

Revision ID: 0123_doubao_reference_image_capability
Revises: 0122_doubao_seedance_provider
Create Date: 2026-07-26
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0123_doubao_reference_image_capability"
down_revision = "0122_doubao_seedance_provider"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    routes = sa.Table("ai_model_routes", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(routes.c.id, routes.c.config_json).where(
            routes.c.provider_key == "doubao",
            routes.c.logical_model_id == "seedance_2_0_mini",
            routes.c.capability == "video",
        )
    ).all()
    for route_id, current_config in rows:
        config = dict(current_config or {})
        capabilities = dict(config.get("video_capabilities") or {})
        capabilities.update(
            {
                "aspect_ratios": ["9:16", "16:9", "1:1"],
                "reference_video": False,
                "reference_modes": ["reference"],
                "reference_image_counts": [0, 1],
                "durations": list(range(4, 16)),
                "resolutions": ["720p"],
            }
        )
        config["video_capabilities"] = capabilities
        bind.execute(
            routes.update().where(routes.c.id == int(route_id)).values(config_json=config)
        )


def downgrade() -> None:
    bind = op.get_bind()
    metadata = sa.MetaData()
    routes = sa.Table("ai_model_routes", metadata, autoload_with=bind)
    rows = bind.execute(
        sa.select(routes.c.id, routes.c.config_json).where(
            routes.c.provider_key == "doubao",
            routes.c.logical_model_id == "seedance_2_0_mini",
            routes.c.capability == "video",
        )
    ).all()
    for route_id, current_config in rows:
        config = dict(current_config or {})
        capabilities = dict(config.get("video_capabilities") or {})
        capabilities["reference_image_counts"] = [0]
        config["video_capabilities"] = capabilities
        bind.execute(
            routes.update().where(routes.c.id == int(route_id)).values(config_json=config)
        )
