"""add discovered AI models, routes, health and attempt audit

Revision ID: 0112_ai_provider_routing
Revises: 0111_ttshop_video_transcript
Create Date: 2026-07-21 18:00:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql


revision = "0112_ai_provider_routing"
down_revision = "0111_ttshop_video_transcript"
branch_labels = None
depends_on = None

UBIGINT = sa.BigInteger().with_variant(mysql.BIGINT(unsigned=True), "mysql")


def upgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    if "ai_provider_models" not in existing:
        op.create_table(
            "ai_provider_models",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("provider_key", sa.String(32), nullable=False),
            sa.Column("provider_model_id", sa.String(191), nullable=False),
            sa.Column("display_name", sa.String(255), nullable=True),
            sa.Column("capabilities_json", sa.JSON(), nullable=True),
            sa.Column("endpoint_modes_json", sa.JSON(), nullable=True),
            sa.Column("raw_json", sa.JSON(), nullable=True),
            sa.Column("discovery_source", sa.String(32), nullable=False, server_default="UPSTREAM"),
            sa.Column("lifecycle_status", sa.String(32), nullable=False, server_default="DISCOVERED"),
            sa.Column("is_available", sa.Boolean(), nullable=False, server_default=sa.text("1")),
            sa.Column("discovered_by_key_id", UBIGINT, nullable=True),
            sa.Column("first_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_seen_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("last_verified_at", sa.DateTime(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["discovered_by_key_id"], ["kie_api_keys.id"], ondelete="SET NULL", onupdate="RESTRICT"),
            sa.UniqueConstraint("provider_key", "provider_model_id", name="uk_ai_provider_model_identity"),
        )
        op.create_index("idx_ai_provider_model_available", "ai_provider_models", ["provider_key", "is_available"])
        op.create_index("idx_ai_provider_model_lifecycle", "ai_provider_models", ["lifecycle_status", "last_seen_at"])
    if "ai_model_routes" not in existing:
        op.create_table(
            "ai_model_routes",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("key_id", UBIGINT, nullable=False),
            sa.Column("provider_key", sa.String(32), nullable=False),
            sa.Column("workload", sa.String(64), nullable=False, server_default="default"),
            sa.Column("logical_model_id", sa.String(191), nullable=False),
            sa.Column("provider_model_id", sa.String(191), nullable=False),
            sa.Column("capability", sa.String(32), nullable=False),
            sa.Column("adapter_type", sa.String(64), nullable=False),
            sa.Column("priority", sa.Integer(), nullable=False, server_default="100"),
            sa.Column("is_enabled", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("is_verified", sa.Boolean(), nullable=False, server_default=sa.text("0")),
            sa.Column("health_status", sa.String(32), nullable=False, server_default="UNKNOWN"),
            sa.Column("consecutive_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_successes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("total_failures", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latency_ema_ms", sa.Integer(), nullable=True),
            sa.Column("circuit_open_until", sa.DateTime(), nullable=True),
            sa.Column("last_success_at", sa.DateTime(), nullable=True),
            sa.Column("last_failure_at", sa.DateTime(), nullable=True),
            sa.Column("last_error_class", sa.String(64), nullable=True),
            sa.Column("last_error_message", sa.String(1000), nullable=True),
            sa.Column("config_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.ForeignKeyConstraint(["key_id"], ["kie_api_keys.id"], ondelete="CASCADE", onupdate="RESTRICT"),
            sa.UniqueConstraint("key_id", "workload", "logical_model_id", "provider_model_id", "capability", name="uk_ai_model_route_identity"),
        )
        op.create_index("idx_ai_model_route_select", "ai_model_routes", ["logical_model_id", "capability", "workload", "is_enabled", "priority"])
        op.create_index("idx_ai_model_route_health", "ai_model_routes", ["health_status", "circuit_open_until"])
    if "ai_route_attempts" not in existing:
        op.create_table(
            "ai_route_attempts",
            sa.Column("id", UBIGINT, primary_key=True, autoincrement=True),
            sa.Column("route_id", UBIGINT, nullable=False),
            sa.Column("request_id", sa.String(96), nullable=False),
            sa.Column("switched_from_route_id", UBIGINT, nullable=True),
            sa.Column("status", sa.String(32), nullable=False),
            sa.Column("error_class", sa.String(64), nullable=True),
            sa.Column("upstream_status_code", sa.Integer(), nullable=True),
            sa.Column("latency_ms", sa.Integer(), nullable=True),
            sa.Column("prompt_tokens", sa.Integer(), nullable=True),
            sa.Column("completion_tokens", sa.Integer(), nullable=True),
            sa.Column("metadata_json", sa.JSON(), nullable=True),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("completed_at", sa.DateTime(), nullable=True),
            sa.ForeignKeyConstraint(["route_id"], ["ai_model_routes.id"], ondelete="CASCADE", onupdate="RESTRICT"),
            sa.ForeignKeyConstraint(["switched_from_route_id"], ["ai_model_routes.id"], ondelete="SET NULL", onupdate="RESTRICT"),
        )
        op.create_index("idx_ai_route_attempt_request", "ai_route_attempts", ["request_id", "id"])
        op.create_index("idx_ai_route_attempt_route", "ai_route_attempts", ["route_id", "created_at"])
        op.create_index("idx_ai_route_attempt_status", "ai_route_attempts", ["status", "created_at"])


def downgrade() -> None:
    bind = op.get_bind()
    existing = set(sa.inspect(bind).get_table_names())
    for table in ("ai_route_attempts", "ai_model_routes", "ai_provider_models"):
        if table in existing:
            op.drop_table(table)
