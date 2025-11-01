# backend/app/services/providers/tiktok_business.py
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Dict, List, Mapping, Optional, Literal

from sqlalchemy.orm import Session

from app.data.models.oauth_ttb import OAuthAccountTTB
from app.services.oauth_ttb import get_access_token_plain
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

    # ---------- 选项规范化 ----------
    def _clamp_int(self, value: Any, *, default: int, lo: int, hi: int) -> int:
        try:
            x = int(value)
        except Exception:
            x = int(default)
        if x < lo:
            x = lo
        if x > hi:
            x = hi
        return x

    def validate_options(self, *, scope: str, options: Mapping[str, Any]) -> Dict[str, Any]:
        """
        统一选项键名：
          - 用户可传 {limit|page_size}，provider 统一导出为 page_size
          - 用户可传 {product_limit|product_page_size}，统一为 product_page_size
          - 允许传 store_id（仅 products/all 有效）
          - 允许传 since（datetime/ISO 字符串）
          - 可选 product_eligibility: {'gmv_max','ads','all'}
        """
        normalized: Dict[str, Any] = {}

        # mode
        mode = str(options.get("mode") or "incremental").strip().lower()
        if mode not in {"incremental", "full"}:
            raise ValueError("invalid mode")
        normalized["mode"] = mode

        # page_size（接受 limit/page_size，统一成 page_size）
        page_size = options.get("page_size", options.get("limit", None))
        if page_size is not None:
            clamped = self._clamp_int(page_size, default=50, lo=1, hi=2000)
        else:
            clamped = 50
        normalized["page_size"] = clamped
        normalized["limit"] = clamped

        # product_page_size（接受 product_limit/product_page_size，统一成 product_page_size）
        product_ps = options.get("product_page_size", options.get("product_limit", None))
        if product_ps is not None:
            normalized["product_page_size"] = self._clamp_int(product_ps, default=50, lo=1, hi=2000)

        # store_id 仅在 products/all 有效
        store_id = options.get("store_id")
        if store_id:
            normalized["store_id"] = str(store_id)

        # since 透传为 ISO 字符串（若为 datetime）
        since = options.get("since")
        if since:
            if isinstance(since, datetime):
                normalized["since"] = since.astimezone(timezone.utc).isoformat()
            else:
                normalized["since"] = str(since)

        # 可选：产品可投放过滤（与 TTBSyncService.sync_products 对齐）
        product_elig = options.get("product_eligibility")
        if product_elig:
            s = str(product_elig).strip().lower()
            if s not in {"gmv_max", "ads", "all"}:
                raise ValueError("invalid product_eligibility (expected one of: gmv_max, ads, all)")
            normalized["product_eligibility"] = s  # TTBSyncService 接受小写这三种

        # 清理与 scope 无关字段
        if scope not in {"products", "all"}:
            normalized.pop("product_page_size", None)
            normalized.pop("store_id", None)
            normalized.pop("product_eligibility", None)

        return normalized

    # ---------- 执行入口 ----------
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
        meta_summary: Dict[str, Dict[str, int]] | None = None
        try:
            if scope == "all":
                for stage in ("bc", "advertisers", "stores", "products"):
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
            elif scope == "meta":
                page_size = int(options.get("page_size", 50))
                meta_phases, summary = await service.sync_meta(page_size=page_size)
                for phase_scope, stats, duration_ms in meta_phases:
                    phases.append(
                        PhaseResult(scope=phase_scope, stats=stats, duration_ms=duration_ms)
                    )
                meta_summary = summary
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

        payload = {
            "phases": [
                {"scope": phase.scope, "stats": phase.stats, "duration_ms": phase.duration_ms}
                for phase in phases
            ],
            "errors": [],
        }
        if meta_summary is not None:
            payload["summary"] = meta_summary
        return payload

    # ---------- 内部工具 ----------
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
        token, _ = get_access_token_plain(db, int(auth_id))
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
        logger.info("provider.scope.start", extra={"provider": self.provider_id, "scope": scope})

        # 统一使用 page_size，并受策略上限裁剪
        base_page_size = int(options.get("page_size", 50))
        if limits.max_entities_per_run is not None:
            base_page_size = min(base_page_size, int(limits.max_entities_per_run))

        if scope == "bc":
            stats = await service.sync_bc(page_size=base_page_size)

        elif scope == "advertisers":
            stats = await service.sync_advertisers(page_size=base_page_size)

        elif scope == "stores":
            stats = await service.sync_stores(page_size=base_page_size)

        elif scope == "products":
            product_page_size = int(options.get("product_page_size", base_page_size))
            if limits.max_entities_per_run is not None:
                product_page_size = min(product_page_size, int(limits.max_entities_per_run))

            stats = await service.sync_products(
                page_size=product_page_size,
                store_id=options.get("store_id"),
                product_eligibility=options.get("product_eligibility"),  # 'gmv_max' | 'ads' | 'all' | None
            )
        else:
            raise ValueError(f"unsupported scope: {scope}")

        end = datetime.now(timezone.utc)
        duration_ms = int((end - start).total_seconds() * 1000)
        logger.info(
            "provider.scope.finish",
            extra={"provider": self.provider_id, "scope": scope, "duration_ms": duration_ms},
        )
        return PhaseResult(scope=scope, stats=stats, duration_ms=duration_ms)


__all__ = ["TiktokBusinessProvider", "ProviderExecutionError"]

