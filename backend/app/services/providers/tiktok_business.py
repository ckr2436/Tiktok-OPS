from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping

from sqlalchemy.orm import Session

from app.data.models.oauth_ttb import OAuthAccountTTB
from app.services.oauth_ttb import get_access_token_for_auth_id, get_credentials_for_auth_id
from app.services.policy_engine import PolicyEngine, PolicyLimits
from app.services.ttb_api import TTBApiClient
from app.services.ttb_sync import TTBSyncService


@dataclass(slots=True)
class PhaseResult:
    scope: str
    stats: Dict[str, Any]
    duration_ms: int


class ProviderExecutionError(Exception):
    def __init__(self, *, stage: str, original: Exception, phases: List[PhaseResult]):
        super().__init__(str(original))
        self.stage = stage
        self.original = original
        self.phases = phases


class TiktokBusinessProvider:
    provider_id = "tiktok-business"

    def validate_options(self, *, scope: str, options: Mapping[str, Any]) -> Dict[str, Any]:
        normalized: Dict[str, Any] = {}
        mode = str(options.get("mode") or "incremental").lower()
        if mode not in {"incremental", "full"}:
            raise ValueError("invalid mode")
        normalized["mode"] = mode

        limit = options.get("limit")
        if limit is None:
            limit_value = 200
        else:
            try:
                limit_value = int(limit)
            except (TypeError, ValueError) as exc:  # noqa: PERF203
                raise ValueError("limit must be an integer") from exc
        if limit_value < 1 or limit_value > 2000:
            raise ValueError("limit must be between 1 and 2000")
        normalized["limit"] = limit_value

        product_limit = options.get("product_limit")
        if product_limit is not None:
            try:
                product_limit_value = int(product_limit)
            except (TypeError, ValueError) as exc:  # noqa: PERF203
                raise ValueError("product_limit must be an integer") from exc
            if product_limit_value < 1 or product_limit_value > 2000:
                raise ValueError("product_limit must be between 1 and 2000")
            normalized["product_limit"] = product_limit_value

        shop_id = options.get("shop_id")
        if shop_id:
            normalized["shop_id"] = str(shop_id)

        since = options.get("since")
        if since:
            if isinstance(since, datetime):
                normalized["since"] = since.astimezone(timezone.utc).isoformat()
            else:
                normalized["since"] = str(since)

        if scope not in {"products", "all"}:
            normalized.pop("product_limit", None)
            normalized.pop("shop_id", None)

        return normalized

    async def run_scope(self, *, db: Session, envelope: dict, scope: str, logger) -> Dict[str, Any]:
        workspace_id = int(envelope["workspace_id"])
        auth_id = int(envelope["auth_id"])
        options = envelope.get("options") or {}

        account = db.get(OAuthAccountTTB, auth_id)
        if not account or account.workspace_id != workspace_id:
            raise ValueError("binding not found")

        limits = self._policy_limits(db, workspace_id=workspace_id, auth_id=auth_id)
        client = self._build_client(db, auth_id=auth_id, limits=limits)
        service = TTBSyncService(db, client, workspace_id=workspace_id, auth_id=auth_id)

        phases: List[PhaseResult] = []
        try:
            if scope == "all":
                for stage in ("bc", "advertisers", "shops", "products"):
                    try:
                        phases.append(
                            await self._run_single(
                                service,
                                scope=stage,
                                options=options,
                                limits=limits,
                                delay=bool(phases),
                                logger=logger,
                            )
                        )
                    except Exception as exc:  # noqa: BLE001
                        raise ProviderExecutionError(stage=stage, original=exc, phases=phases) from exc
            else:
                try:
                    phases.append(
                        await self._run_single(
                            service,
                            scope=scope,
                            options=options,
                            limits=limits,
                            delay=False,
                            logger=logger,
                        )
                    )
                except Exception as exc:  # noqa: BLE001
                    raise ProviderExecutionError(stage=scope, original=exc, phases=phases) from exc
        finally:
            await service.client.aclose()

        return {
            "phases": [
                {
                    "scope": phase.scope,
                    "stats": phase.stats,
                    "duration_ms": phase.duration_ms,
                }
                for phase in phases
            ],
            "errors": [],
        }

    def _policy_limits(self, db: Session, *, workspace_id: int, auth_id: int) -> PolicyLimits:
        engine = PolicyEngine(db)
        decision = engine.evaluate_policy(
            workspace_id=workspace_id,
            provider_key=self.provider_id,
            resource_type="sync",
            candidate_ids={"auth_id": str(auth_id)},
            now_utc=datetime.now(timezone.utc),
        )
        if not decision.allowed and decision.enforcement_mode.value == "enforce":
            raise PermissionError(decision.reason or "policy denied")
        return decision.limits

    def _build_client(self, db: Session, *, auth_id: int, limits: PolicyLimits) -> TTBApiClient:
        token = get_access_token_for_auth_id(db, auth_id)
        qps = float(limits.rate_limit_rps or 10.0)
        return TTBApiClient(access_token=token, qps=qps)

    async def _run_single(
        self,
        service: TTBSyncService,
        *,
        scope: str,
        options: Mapping[str, Any],
        limits: PolicyLimits,
        delay: bool,
        logger,
    ) -> PhaseResult:
        if delay and limits.cooldown_seconds:
            await asyncio.sleep(limits.cooldown_seconds)

        start = datetime.now(timezone.utc)
        logger.info(
            "provider.scope.start",
            extra={"provider": self.provider_id, "scope": scope},
        )
        effective_limit = int(options.get("limit", 200))
        if limits.max_entities_per_run is not None:
            effective_limit = min(effective_limit, int(limits.max_entities_per_run))
        if scope == "bc":
            stats = await service.sync_bc(limit=effective_limit)
        elif scope == "advertisers":
            app_id, secret, _ = get_credentials_for_auth_id(service.db, service.auth_id)
            stats = await service.sync_advertisers(
                limit=effective_limit,
                app_id=app_id,
                secret=secret,
            )
        elif scope == "shops":
            stats = await service.sync_shops(limit=effective_limit)
        elif scope == "products":
            product_limit = options.get("product_limit")
            if product_limit is not None:
                effective_product_limit = int(product_limit)
            else:
                effective_product_limit = effective_limit
            if limits.max_entities_per_run is not None:
                effective_product_limit = min(effective_product_limit, int(limits.max_entities_per_run))
            stats = await service.sync_products(
                limit=effective_product_limit,
                shop_id=options.get("shop_id"),
            )
        else:
            raise ValueError(f"unsupported scope: {scope}")
        end = datetime.now(timezone.utc)
        logger.info(
            "provider.scope.finish",
            extra={
                "provider": self.provider_id,
                "scope": scope,
                "duration_ms": int((end - start).total_seconds() * 1000),
            },
        )
        return PhaseResult(scope=scope, stats=stats, duration_ms=int((end - start).total_seconds() * 1000))

__all__ = ["TiktokBusinessProvider", "ProviderExecutionError"]
