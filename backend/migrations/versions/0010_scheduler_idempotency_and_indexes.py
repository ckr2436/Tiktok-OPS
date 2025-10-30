"""scheduler idempotency unique & hot indexes (idempotent-safe)

- Expand alembic_version.version_num to VARCHAR(64) on MySQL (or 64-char on others)
- Deduplicate schedule_runs by (schedule_id, idempotency_key) keeping latest id
- Add UNIQUE(schedule_id, idempotency_key) for strict idempotency
- Add hot composite indexes to speed lookups

This script is safe to re-run (checks existence before creating/dropping).
"""

from __future__ import annotations

from typing import Iterable

from alembic import op
import sqlalchemy as sa
from sqlalchemy import inspect, text


# --- Keep the current long revision id so your history matches CLI output ---
revision = "0010_scheduler_idempotency_and_indexes"
down_revision = "0009_ttb_sched_status"
branch_labels = None
depends_on = None


# ---------- helpers ----------
def _is_mysql(bind) -> bool:
    return bind.dialect.name == "mysql"


def _has_index(table: str, name: str) -> bool:
    insp = inspect(op.get_bind())
    return any(ix.get("name") == name for ix in insp.get_indexes(table))


def _has_unique(table: str, name: str) -> bool:
    insp = inspect(op.get_bind())
    return any(c.get("name") == name for c in insp.get_unique_constraints(table))


def _has_primary_key(table: str, cols: Iterable[str]) -> bool:
    insp = inspect(op.get_bind())
    pk = insp.get_pk_constraint(table) or {}
    pk_cols = tuple(pk.get("constrained_columns") or [])
    return tuple(cols) == pk_cols


def _expand_alembic_version_len() -> None:
    """Ensure alembic_version.version_num >= VARCHAR(64) so long revision ids fit."""
    bind = op.get_bind()
    if _is_mysql(bind):
        # MySQL: alter length; alembic_version.version_num is PK, length change is allowed
        op.execute(text("ALTER TABLE alembic_version MODIFY version_num VARCHAR(64) NOT NULL"))
    else:
        # Generic path (PostgreSQL/SQLite): widen to 64 chars or TEXT
        try:
            with op.batch_alter_table("alembic_version") as batch_op:
                batch_op.alter_column("version_num", type_=sa.String(length=64), existing_nullable=False)
        except Exception:
            # Some engines (e.g. SQLite) don't enforce length; ignore if not supported
            pass


def _dedupe_schedule_runs() -> None:
    """Delete duplicate (schedule_id, idempotency_key) rows keeping the max(id)."""
    bind = op.get_bind()
    if _is_mysql(bind):
        # MySQL-safe duplicate cleanup using a self-join
        op.execute(
            text(
                """
                DELETE sr FROM schedule_runs sr
                JOIN (
                  SELECT schedule_id, idempotency_key, MAX(id) AS keep_id, COUNT(*) AS cnt
                  FROM schedule_runs
                  GROUP BY schedule_id, idempotency_key
                  HAVING cnt > 1
                ) d
                  ON d.schedule_id = sr.schedule_id
                 AND d.idempotency_key = sr.idempotency_key
                 AND sr.id <> d.keep_id
                """
            )
        )
    else:
        # Portable attempt using a window function (works on PostgreSQL >= 9.4).
        # If the engine doesn't support it (e.g., SQLite old), ignore gracefully.
        try:
            op.execute(
                text(
                    """
                    DELETE FROM schedule_runs
                    WHERE id IN (
                      SELECT id FROM (
                        SELECT id,
                               ROW_NUMBER() OVER (
                                 PARTITION BY schedule_id, idempotency_key
                                 ORDER BY id DESC
                               ) AS rn
                        FROM schedule_runs
                      ) t
                      WHERE t.rn > 1
                    )
                    """
                )
            )
        except Exception:
            pass


# ---------- upgrade/downgrade ----------
def upgrade() -> None:
    bind = op.get_bind()

    # 1) Before Alembic writes the long revision into alembic_version,
    #    widen the column so update won't fail.
    _expand_alembic_version_len()

    # 2) Clean up duplicates so we can add UNIQUE constraint safely
    _dedupe_schedule_runs()

    # 3) Add UNIQUE(schedule_id, idempotency_key) if missing
    uq_name = "uq_runs_schedule_id_idempotency_key"
    if not _has_unique("schedule_runs", uq_name):
        op.create_unique_constraint(
            uq_name,
            "schedule_runs",
            ["schedule_id", "idempotency_key"],
        )

    # 4) Hot composite indexes (idempotent existence checks)
    #    Existing single/other indexes in your model:
    #      - idx_runs_sched_time (schedule_id, scheduled_for)
    #      - idx_runs_ws_time    (workspace_id, scheduled_for)
    #      - idx_runs_status     (status)
    #    Add composite filters commonly used by queries/cron dashboards:
    ix1 = "idx_runs_ws_status_time"
    if not _has_index("schedule_runs", ix1):
        op.create_index(ix1, "schedule_runs", ["workspace_id", "status", "scheduled_for"])

    ix2 = "idx_runs_schedule_status_time"
    if not _has_index("schedule_runs", ix2):
        op.create_index(ix2, "schedule_runs", ["schedule_id", "status", "scheduled_for"])

    # Optional: if you frequently lookup by (schedule_id, idempotency_key) even after UNIQUE,
    # MySQL will have an internal index for UNIQUE; no need to duplicate a normal index.


def downgrade() -> None:
    # Drop composite indexes first (if they exist)
    ix2 = "idx_runs_schedule_status_time"
    if _has_index("schedule_runs", ix2):
        op.drop_index(ix2, table_name="schedule_runs")

    ix1 = "idx_runs_ws_status_time"
    if _has_index("schedule_runs", ix1):
        op.drop_index(ix1, table_name="schedule_runs")

    # Drop UNIQUE(schedule_id, idempotency_key) if present
    uq_name = "uq_runs_schedule_id_idempotency_key"
    if _has_unique("schedule_runs", uq_name):
        op.drop_constraint(uq_name, "schedule_runs", type_="unique")

    # DO NOT shrink alembic_version.version_num back; it’s harmless to stay at 64 and safer for future.

