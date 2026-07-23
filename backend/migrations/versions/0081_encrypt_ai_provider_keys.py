"""encrypt stored AI provider API keys

Revision ID: 0081_encrypt_ai_provider_keys
Revises: 0080_ai_provider_model_routing
Create Date: 2026-07-10 23:55:00.000000
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa

from app.services.kie_api.accounts import (
    API_KEY_ENCRYPTION_PREFIX,
    decrypt_api_key,
    encrypt_api_key,
)


revision = "0081_encrypt_ai_provider_keys"
down_revision = "0080_ai_provider_model_routing"
branch_labels = None
depends_on = None


def _rows():
    bind = op.get_bind()
    return bind.execute(
        sa.text("SELECT id, api_key_ciphertext FROM kie_api_keys ORDER BY id")
    ).mappings().all()


def upgrade() -> None:
    bind = op.get_bind()
    op.alter_column(
        "kie_api_keys",
        "api_key_ciphertext",
        existing_type=sa.String(length=512),
        type_=sa.String(length=2048),
        existing_nullable=False,
    )
    for row in _rows():
        value = str(row["api_key_ciphertext"] or "").strip()
        if not value or value.startswith(API_KEY_ENCRYPTION_PREFIX):
            continue
        bind.execute(
            sa.text(
                "UPDATE kie_api_keys SET api_key_ciphertext = :ciphertext WHERE id = :key_id"
            ),
            {"ciphertext": encrypt_api_key(value), "key_id": int(row["id"])},
        )


def downgrade() -> None:
    bind = op.get_bind()
    for row in _rows():
        value = str(row["api_key_ciphertext"] or "").strip()
        if not value.startswith(API_KEY_ENCRYPTION_PREFIX):
            continue
        bind.execute(
            sa.text(
                "UPDATE kie_api_keys SET api_key_ciphertext = :ciphertext WHERE id = :key_id"
            ),
            {"ciphertext": decrypt_api_key(value), "key_id": int(row["id"])},
        )
    op.alter_column(
        "kie_api_keys",
        "api_key_ciphertext",
        existing_type=sa.String(length=2048),
        type_=sa.String(length=512),
        existing_nullable=False,
    )
