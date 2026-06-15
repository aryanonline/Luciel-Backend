"""FastAPI application factory.

Wires CORS for the dashboard origin (credentials:'include'), the httpOnly cookie
session, the frozen error contract, and the v1 routers. The control plane and the
data plane (chat-widget) are mounted as separate routers but co-located in one
ASGI app (Locked Decision §7 #17 — logical separation today).
"""

from __future__ import annotations

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from app.api.errors import register_error_handlers
from app.config import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    app = FastAPI(title="Luciel Backend", version="0.1.0")

    app.add_middleware(
        CORSMiddleware,
        allow_origins=settings.dashboard_origins,
        allow_credentials=True,  # required for the httpOnly session cookie
        allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        allow_headers=["content-type", "authorization"],
    )

    register_error_handlers(app)

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    # v1 routers (mounted as arcs land).
    from app.api.v1.admin.account import router as account_router
    from app.api.v1.admin.knowledge import router as knowledge_router
    from app.api.v1.admin.luciel import router as luciel_router
    from app.api.v1.auth import router as auth_router

    app.include_router(auth_router)
    app.include_router(luciel_router)
    app.include_router(knowledge_router)
    app.include_router(account_router)

    return app


app = create_app()
