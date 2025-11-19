"""Deduplicate GMV Max campaigns and enforce scope unique constraint"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

# revision identifiers, used by Alembic.
revision = "0031_ttb_gmvmax_campaign_dedupe"
down_revision = "0030_openai_whisper_jobs"
branch_labels = None
depends_on = None

campaigns_table = sa.table(
    "ttb_gmvmax_campaigns",
    sa.column("id", sa.BigInteger()),
    sa.column("workspace_id", sa.BigInteger()),
    sa.column("auth_id", sa.BigInteger()),
    sa.column("campaign_id", sa.String(64)),
)


def _chunked(values: list[int], size: int = 500):
    for index in range(0, len(values), size):
        yield values[index : index + size]


def _delete_duplicate_rows() -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.select(
            campaigns_table.c.id,
            campaigns_table.c.workspace_id,
            campaigns_table.c.auth_id,
            campaigns_table.c.campaign_id,
        ).order_by(
            campaigns_table.c.workspace_id,
            campaigns_table.c.auth_id,
            campaigns_table.c.campaign_id,
            campaigns_table.c.id,
        )
    ).fetchall()
    seen: set[tuple[int, int, str]] = set()
    duplicates: list[int] = []
    for row in rows:
        mapping = row._mapping
        key = (
            int(mapping["workspace_id"]),
            int(mapping["auth_id"]),
            str(mapping["campaign_id"]),
        )
        if key in seen:
            duplicates.append(int(mapping["id"]))
        else:
            seen.add(key)
    if not duplicates:
        return
    for chunk in _chunked(duplicates):
        bind.execute(
            sa.delete(campaigns_table).where(campaigns_table.c.id.in_(chunk))
        )


def _ensure_unique_constraint() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {uc.get("name") for uc in inspector.get_unique_constraints("ttb_gmvmax_campaigns")}
    if "uk_ttb_gmvmax_campaign_scope" not in existing:
        op.create_unique_constraint(
            "uk_ttb_gmvmax_campaign_scope",
            "ttb_gmvmax_campaigns",
            ["workspace_id", "auth_id", "campaign_id"],
        )


def _drop_unique_constraint() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    existing = {uc.get("name") for uc in inspector.get_unique_constraints("ttb_gmvmax_campaigns")}
    if "uk_ttb_gmvmax_campaign_scope" in existing:
        op.drop_constraint(
            "uk_ttb_gmvmax_campaign_scope",
            "ttb_gmvmax_campaigns",
            type_="unique",
        )


def upgrade() -> None:
    _delete_duplicate_rows()
    _ensure_unique_constraint()


def downgrade() -> None:
    _drop_unique_constraint()
