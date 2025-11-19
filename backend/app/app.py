# app/app.py
from __future__ import annotations
from pathlib import Path
from functools import lru_cache
from typing import Dict, Any

from fastapi import FastAPI, Depends, HTTPException
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.openapi.utils import get_openapi
from starlette.staticfiles import StaticFiles

from app.core.config import settings
from app.core.errors import install_exception_handlers
from app.core.middleware import install_middleware
from app.core.deps import require_platform_admin

# --- Core / Platform ---
from app.features.healthz.router import router as healthz_router
from app.features.platform.router_auth import router as platform_auth_router
from app.features.platform.router_admin import router as platform_admin_router
from app.features.platform.router_companies import router as platform_companies_router
from app.features.platform.router_oauth_apps import router as platform_oauth_apps_router
from app.features.platform.router_oauth_callback import router as oauth_callback_router
from app.features.platform.router_tasks import router as platform_tasks_router  # 平台任务触发网关
from app.features.platform.router_platform_policies import router as platform_policies_router
from app.features.platform.kie_ai.routes import router as platform_kie_ai_router  # ★ 这里指向 routes

# --- Tenants ---
from app.features.tenants.users.router import router as tenant_users_router
from app.features.tenants.oauth_ttb.router import router as tenant_oauth_ttb_router
from app.features.tenants.schedules.router import router as tenant_schedules_router  # plan/schedule API

# ★ 新增：TTB 同步相关独立路由
from app.features.tenants.oauth_ttb.router_sync import router as sync_router
from app.features.tenants.oauth_ttb.router_sync_all import router as sync_all_router
from app.features.tenants.oauth_ttb.router_cursors import router as cursors_router
from app.features.tenants.oauth_ttb.router_jobs import router as jobs_router
from app.features.tenants.ttb.router import router as tenant_ttb_router

# 新增：kie 相关独立路由
from app.features.tenants.kie_ai.router_sora2 import router as tenant_kie_ai_router
from app.features.tenants.openai_whisper.router import (
    router as tenant_openai_whisper_router,
)

from app.services.provider_registry import load_builtin_providers


def create_app() -> FastAPI:
    app = FastAPI(
        title=settings.APP_NAME,
        version=settings.APP_VERSION,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    # Core setup
    install_middleware(app)
    install_exception_handlers(app)

    # Providers
    load_builtin_providers()

    # Routers
    app.include_router(healthz_router)

    app.include_router(platform_auth_router)
    app.include_router(platform_admin_router)
    app.include_router(platform_companies_router)
    app.include_router(platform_oauth_apps_router)
    app.include_router(platform_tasks_router)
    app.include_router(platform_policies_router)
    app.include_router(platform_kie_ai_router)

    app.include_router(tenant_users_router)
    app.include_router(tenant_oauth_ttb_router)
    app.include_router(tenant_schedules_router)

    # ★ 注册租户级 TTB 同步 API（独立文件，避免 router.py 过胖）
    app.include_router(sync_router)
    app.include_router(sync_all_router)
    app.include_router(cursors_router)
    app.include_router(jobs_router)
    app.include_router(tenant_ttb_router)

    # 租户侧 Kie AI 路由
    app.include_router(tenant_kie_ai_router)
    app.include_router(tenant_openai_whisper_router)

    app.include_router(oauth_callback_router)  # /api/oauth/tiktok-business/callback（不版本化）

    # Static & Admin Docs
    base_dir = Path(__file__).resolve().parent
    static_dir = base_dir / "static"
    if static_dir.exists():
        app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

    if settings.ADMIN_DOCS_ENABLE:
        # 管理员文档静态资源目录（建议：app/static/admin-docs 或自定义 settings.ADMIN_DOCS_DIR）
        admin_docs_dir = Path(settings.ADMIN_DOCS_DIR) if settings.ADMIN_DOCS_DIR else (static_dir / "admin-docs")
        has_assets = admin_docs_dir.exists()

        if has_assets:
            # 统一入口：/api/admin-docs/static/* 提供 swagger-ui.css / swagger-ui-bundle.js / redoc.standalone.js 等
            app.mount("/api/admin-docs/static", StaticFiles(directory=str(admin_docs_dir)), name="admin-docs-static")

        @lru_cache(maxsize=1)
        def _openapi_schema() -> Dict[str, Any]:
            schema = get_openapi(
                title=app.title,
                version=settings.APP_VERSION,
                routes=app.routes,
                description="GMV Ops API",
            )

            # Swagger UI shows duplicate sections when the tag list contains
            # repeated entries. FastAPI can accumulate duplicate tag objects
            # when multiple routers reuse the same tag name, so we deduplicate
            # them here while preserving order.
            if "tags" in schema:
                seen: set[str] = set()
                unique_tags: list[dict[str, Any]] = []
                for tag in schema["tags"] or []:
                    name = tag.get("name")
                    if not name or name in seen:
                        continue
                    seen.add(name)
                    unique_tags.append(tag)
                schema["tags"] = unique_tags

            return schema

        @app.get("/api/admin-docs/openapi.json", response_class=JSONResponse, tags=["admin-docs"])
        async def openapi_json(_: Any = Depends(require_platform_admin)):
            return _openapi_schema()

        if has_assets:
            @app.get("/api/admin-docs/docs", response_class=HTMLResponse, include_in_schema=False, tags=["admin-docs"])
            async def swagger_ui(_: Any = Depends(require_platform_admin)):
                # 注意：静态资源路径全部改为 /api/admin-docs/static/*
                html = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>GMV Ops — Swagger UI</title>
    <link rel="stylesheet" href="/api/admin-docs/static/swagger-ui.css" />
  </head>
  <body>
    <div id="swagger-ui"></div>
    <script src="/api/admin-docs/static/swagger-ui-bundle.js"></script>
    <script src="/api/admin-docs/static/swagger-init.js"></script>
  </body>
</html>"""
                return HTMLResponse(content=html)

            @app.get("/api/admin-docs/redoc", response_class=HTMLResponse, include_in_schema=False, tags=["admin-docs"])
            async def redoc_ui(_: Any = Depends(require_platform_admin)):
                # 统一容器 ID：使用 redoc-root，并与 redoc-init.js 对齐
                html = """
<!doctype html>
<html>
  <head>
    <meta charset="utf-8"/>
    <title>GMV Ops — ReDoc</title>
    <style>html,body,#redoc-root{height:100%;margin:0;padding:0}</style>
  </head>
  <body>
    <div id="redoc-root"></div>
    <script src="/api/admin-docs/static/redoc.standalone.js"></script>
    <script src="/api/admin-docs/static/redoc-init.js"></script>
  </body>
</html>"""
                return HTMLResponse(content=html)
        else:
            @app.get("/api/admin-docs/docs", include_in_schema=False)
            def _missing_admin_docs():
                raise HTTPException(
                    status_code=500,
                    detail="ADMIN_DOCS_ENABLE=true 但未找到 admin-docs 静态资源目录。",
                )

    return app


app = create_app()

