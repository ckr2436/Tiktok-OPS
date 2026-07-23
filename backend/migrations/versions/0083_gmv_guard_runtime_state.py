"""separate GMV guard runtime state from strategy configuration

Revision ID: 0083_gmv_guard_runtime_state
Revises: 0082_tiktok_shop_oauth
Create Date: 2026-07-11 10:30:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0083_gmv_guard_runtime_state"
down_revision = "0082_tiktok_shop_oauth"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("gmv_campaign_realtime_state")
    }
    if "runtime_json" not in columns:
        op.add_column(
            "gmv_campaign_realtime_state",
            sa.Column("runtime_json", mysql.JSON(), nullable=True),
        )
    if "state_version" not in columns:
        op.add_column(
            "gmv_campaign_realtime_state",
            sa.Column(
                "state_version",
                mysql.BIGINT(unsigned=True),
                nullable=False,
                server_default=sa.text("0"),
            ),
        )

    op.execute(
        """
        update gmv_campaign_realtime_state r
        join gmv_strategy_configs s on s.id=r.strategy_id
        set r.runtime_json=json_set(
                coalesce(r.runtime_json, json_object()),
                '$.smart_guard_state',
                coalesce(json_extract(s.config_json, '$.smart_guard_state'), json_object()),
                '$.creative_guard_state',
                coalesce(json_extract(s.config_json, '$.creative_guard_state'), json_object())
            ),
            r.state_version=greatest(r.state_version, 1)
        where json_contains_path(
            coalesce(s.config_json, json_object()),
            'one',
            '$.smart_guard_state',
            '$.creative_guard_state'
        )
        """
    )
    op.execute(
        """
        update gmv_strategy_configs s
        join gmv_campaign_realtime_state r on r.strategy_id=s.id
        set s.config_json=json_remove(
            coalesce(s.config_json, json_object()),
            '$.smart_guard_state',
            '$.creative_guard_state'
        )
        where json_contains_path(
            coalesce(s.config_json, json_object()),
            'one',
            '$.smart_guard_state',
            '$.creative_guard_state'
        )
        """
    )


def downgrade() -> None:
    bind = op.get_bind()
    columns = {
        column["name"]
        for column in sa.inspect(bind).get_columns("gmv_campaign_realtime_state")
    }
    if "runtime_json" in columns:
        op.execute(
            """
            update gmv_strategy_configs s
            join gmv_campaign_realtime_state r on r.strategy_id=s.id
            set s.config_json=json_set(
                coalesce(s.config_json, json_object()),
                '$.smart_guard_state',
                coalesce(json_extract(r.runtime_json, '$.smart_guard_state'), json_object()),
                '$.creative_guard_state',
                coalesce(json_extract(r.runtime_json, '$.creative_guard_state'), json_object())
            )
            """
        )
    if "state_version" in columns:
        op.drop_column("gmv_campaign_realtime_state", "state_version")
    if "runtime_json" in columns:
        op.drop_column("gmv_campaign_realtime_state", "runtime_json")
