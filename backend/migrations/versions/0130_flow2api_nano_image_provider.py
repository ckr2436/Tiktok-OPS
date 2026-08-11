"""Retire the invalid Sub2API Nano route before Flow2API cutover.

Revision ID: 0130_flow2api_nano_image
Revises: 0129_content_runtime_events
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0130_flow2api_nano_image"
down_revision = "0129_content_runtime_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(sa.text("""
        UPDATE ai_model_routes
        SET is_enabled = 0,
            is_verified = 0,
            health_status = 'RETIRED',
            circuit_open_until = NULL,
            last_error_class = 'RETIRED',
            last_error_message =
                'Replaced by independent Flow2API Nano Banana Pro route'
        WHERE provider_key = 'sub2api'
          AND logical_model_id = 'nano_banana_pro'
          AND (
              adapter_type = 'sub2api_gemini_images'
              OR provider_model_id = 'gemini-3-pro-image'
          )
    """))
    # The former Gemini-only Sub2API credential carried no valid video scope.
    # Once Nano is removed from the Sub2API catalog, leaving that row active
    # would make legacy default-scope inference reinterpret it as an Omni video
    # credential. Preserve the audit row but fail it closed.
    op.execute(sa.text("""
        UPDATE kie_api_keys
        SET is_active = 0,
            is_default = 0
        WHERE provider_key = 'sub2api'
          AND CAST(scopes_json AS CHAR) LIKE '%image:nano_banana_pro%'
          AND CAST(scopes_json AS CHAR) NOT LIKE '%video:%'
    """))


def downgrade() -> None:
    # Do not reactivate the invalid provider contract on downgrade. Preserve
    # its audit row but clear the migration-specific terminal classification.
    op.execute(sa.text("""
        UPDATE ai_model_routes
        SET health_status = 'UNKNOWN',
            last_error_class = NULL,
            last_error_message = NULL
        WHERE provider_key = 'sub2api'
          AND logical_model_id = 'nano_banana_pro'
          AND health_status = 'RETIRED'
    """))
