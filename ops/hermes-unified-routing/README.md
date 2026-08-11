# Hermes Unified AI Routing

All production Hermes text and multimodal roles use the local GMV AI gateway
at `http://127.0.0.1:8650/v1`. The gateway owns provider credentials,
Sub2API-first routing, circuit breaking and metadata-only audit records.

`routing-policy.json` declares business roles and ordered model tiers. Provider
order inside a tier is controlled from the platform AI routing page and is
stored on the materialized routes, so a later model discovery does not erase an
operator choice.

Deploy with `backend/scripts/configure_hermes_unified_gateway.py`; it updates
only provider-facing configuration, creates a timestamped backup, strips old
upstream provider keys from the Hermes home, and preserves the rest of each
runtime's behavior and isolation settings.
