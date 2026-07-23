"""Add Hermes content factory projects, stages, and assets.

Revision ID: 0070_hermes_content_factory
Revises: 0069_hermes_agent
"""

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import mysql

revision = "0070_hermes_content_factory"
down_revision = "0069_hermes_agent"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bigint = mysql.BIGINT(unsigned=True)
    dt = mysql.DATETIME(fsp=6)
    common = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}
    op.create_table(
        "hermes_content_factory_projects",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("project_key", sa.String(64), nullable=False),
        sa.Column("workspace_id", bigint, nullable=False),
        sa.Column("user_id", bigint, nullable=True),
        sa.Column("title", sa.String(255), nullable=False),
        sa.Column("product_name", sa.String(255), nullable=False),
        sa.Column("market", sa.String(64), nullable=False, server_default="US"),
        sa.Column("status", sa.String(32), nullable=False, server_default="draft"),
        sa.Column("current_stage", sa.String(32), nullable=False, server_default="INTAKE"),
        sa.Column("product_brief", sa.Text(), nullable=True),
        sa.Column("config_json", sa.JSON(), nullable=True),
        sa.Column("state_json", sa.JSON(), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["workspace_id"], ["workspaces.id"], ondelete="CASCADE", onupdate="RESTRICT"),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="SET NULL", onupdate="RESTRICT"),
        sa.UniqueConstraint("project_key", name="uq_hermes_content_project_key"),
        **common,
    )
    op.create_index("idx_hermes_content_project_ws_user", "hermes_content_factory_projects", ["workspace_id", "user_id", "updated_at"])
    op.create_index("idx_hermes_content_project_status", "hermes_content_factory_projects", ["status", "current_stage"])
    op.create_table(
        "hermes_content_factory_stages",
        sa.Column("id", bigint, primary_key=True, autoincrement=True),
        sa.Column("project_id", bigint, nullable=False), sa.Column("workspace_id", bigint, nullable=False), sa.Column("user_id", bigint, nullable=True),
        sa.Column("stage", sa.String(32), nullable=False), sa.Column("attempt", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("status", sa.String(32), nullable=False, server_default="queued"), sa.Column("instruction", sa.Text(), nullable=True),
        sa.Column("input_json", sa.JSON(), nullable=True), sa.Column("output_json", sa.JSON(), nullable=True), sa.Column("response_text", sa.Text(), nullable=True),
        sa.Column("chat_url", sa.String(1024), nullable=True), sa.Column("error_message", sa.Text(), nullable=True), sa.Column("celery_task_id", sa.String(64), nullable=True),
        sa.Column("started_at", dt, nullable=True), sa.Column("completed_at", dt, nullable=True),
        sa.Column("created_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.Column("updated_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)"), server_onupdate=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["project_id"], ["hermes_content_factory_projects.id"], ondelete="CASCADE", onupdate="RESTRICT"),
        sa.UniqueConstraint("project_id", "stage", "attempt", name="uq_hermes_content_stage_attempt"), **common,
    )
    op.create_index("idx_hermes_content_stage_project", "hermes_content_factory_stages", ["project_id", "id"])
    op.create_index("idx_hermes_content_stage_status", "hermes_content_factory_stages", ["status"])
    op.create_table(
        "hermes_content_factory_assets",
        sa.Column("id", bigint, primary_key=True, autoincrement=True), sa.Column("project_id", bigint, nullable=False),
        sa.Column("workspace_id", bigint, nullable=False), sa.Column("user_id", bigint, nullable=True), sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("kind", sa.String(32), nullable=False), sa.Column("original_name", sa.String(255), nullable=False),
        sa.Column("file_path", sa.String(1024), nullable=False), sa.Column("mime_type", sa.String(128), nullable=True), sa.Column("size_bytes", bigint, nullable=True),
        sa.Column("meta_json", sa.JSON(), nullable=True), sa.Column("created_at", dt, nullable=False, server_default=sa.text("CURRENT_TIMESTAMP(6)")),
        sa.ForeignKeyConstraint(["project_id"], ["hermes_content_factory_projects.id"], ondelete="CASCADE", onupdate="RESTRICT"), **common,
    )
    op.create_index("idx_hermes_content_asset_project", "hermes_content_factory_assets", ["project_id", "kind", "id"])


def downgrade() -> None:
    op.drop_table("hermes_content_factory_assets")
    op.drop_table("hermes_content_factory_stages")
    op.drop_table("hermes_content_factory_projects")
