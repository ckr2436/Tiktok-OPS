"""seed default platform provider"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa
from sqlalchemy import orm


revision = "0004_seed_platform_providers"
down_revision = "0003_platform_policy_domain"
branch_labels = None
depends_on = None


PLATFORM_PROVIDERS = sa.table(
    "platform_providers",
    sa.column("id", sa.BigInteger()),
    sa.column("key", sa.String(length=64)),
    sa.column("display_name", sa.String(length=128)),
    sa.column("is_enabled", sa.Boolean()),
)


DEFAULT_PROVIDERS = (
    {"key": "tiktok-business", "display_name": "TikTok Business", "is_enabled": True},
)


def upgrade() -> None:
    bind = op.get_bind()
    session = orm.Session(bind=bind)

    try:
        for provider in DEFAULT_PROVIDERS:
            key = provider["key"]
            exists = session.execute(
                sa.select(sa.literal(1)).select_from(PLATFORM_PROVIDERS).where(PLATFORM_PROVIDERS.c.key == key)
            ).first()
            if exists is None:
                session.execute(sa.insert(PLATFORM_PROVIDERS).values(**provider))
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def downgrade() -> None:
    # No-op: seeded providers are left intact to avoid data loss.
    pass
