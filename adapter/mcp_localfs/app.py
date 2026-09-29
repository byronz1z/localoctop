"""FastAPI application assembly.

    create_app(settings) -> FastAPI

Wires together:
    POST/GET/DELETE {mcp_path}   the MCP streamable-HTTP surface for Octop
    WS      {ws_path}            the outbound bridge endpoint for clients
    GET     /healthz             liveness/readiness (bridge counts included)
    GET     /admin/sessions      ops view of connected bridges (loopback /
                                 admin-token guarded)
"""

from __future__ import annotations

import contextlib
import logging
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from . import __version__
from .audit import AuditLog
from .bridge_ws import build_router as build_ws_router
from .config import Settings, load_settings
from .errors import BridgeProtocolError, CODE_AUTH_FAILED
from .mcp_app import build_router as build_mcp_router
from .sessions import SessionRegistry

logger = logging.getLogger("mcp_localfs")


def configure_logging(level: str) -> None:
    logging.basicConfig(
        level=getattr(logging, level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or load_settings()
    configure_logging(settings.log_level)

    registry = SessionRegistry(request_timeout=settings.bridge_timeout)
    audit = AuditLog(settings.audit_log_path)

    mcp_router = build_mcp_router(settings, registry, audit, settings.mcp_path)

    @contextlib.asynccontextmanager
    async def lifespan(_: FastAPI):
        # The MCP SDK session manager starts lazily on the first request (so
        # ASGI hosts that never emit lifespan events — e.g. httpx's
        # ASGITransport in tests — still work); the lifespan only owns the
        # orderly shutdown.
        yield
        endpoint = getattr(mcp_router, "localfs_endpoint", None)
        if endpoint is not None:
            await endpoint.shutdown()

    app = FastAPI(
        title="mcp-localfs adapter",
        version=__version__,
        docs_url=None,       # no interactive docs in production
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.settings = settings
    app.state.registry = registry
    app.state.audit = audit

    app.include_router(mcp_router)
    app.include_router(build_ws_router(settings, registry, settings.ws_path))

    @app.get("/healthz")
    async def healthz() -> dict[str, Any]:
        sessions = registry.snapshot()
        return {
            "status": "ok",
            "version": __version__,
            "bridges_connected": len(sessions),
            "disabled": settings.disabled,
            "write_globally_enabled": settings.allow_write,
        }

    @app.get("/admin/sessions")
    async def admin_sessions(request: Request) -> JSONResponse:
        # Guarded by an admin token when configured; otherwise loopback-only.
        admin_token = settings.static_tokens.get("__admin__") or _env_admin_token()
        auth = request.headers.get("authorization", "")
        authorized = False
        if admin_token:
            authorized = auth == f"Bearer {admin_token}"
        else:
            client = request.client
            authorized = bool(client and client.host in ("127.0.0.1", "::1", "localhost"))
        if not authorized:
            err = BridgeProtocolError(CODE_AUTH_FAILED, "admin access denied")
            return JSONResponse({"error": err.to_dict()}, status_code=403)
        return JSONResponse({"sessions": registry.snapshot()})

    # ---- token management (任务书 v1.1 §七: split mcp/bridge tokens) ----

    async def _admin_guard(request: Request) -> bool:
        admin_token = settings.static_tokens.get("__admin__") or _env_admin_token()
        auth = request.headers.get("authorization", "")
        if admin_token:
            return auth == f"Bearer {admin_token}"
        client = request.client
        return bool(client and client.host in ("127.0.0.1", "::1", "localhost"))

    @app.post("/admin/tokens/issue")
    async def admin_issue_token(request: Request) -> JSONResponse:
        """Mint one token. Body: {"user_id": str, "kind": "mcp"|"bridge"}.
        The plaintext is returned exactly once; only its SHA-256 hash is kept."""
        from .tokens import KIND_BRIDGE, KIND_MCP

        if not await _admin_guard(request):
            err = BridgeProtocolError(CODE_AUTH_FAILED, "admin access denied")
            return JSONResponse({"error": err.to_dict()}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"code": -32602, "message": "invalid JSON body"}}, status_code=400)
        user_id = str(body.get("user_id", "")).strip()
        kind = str(body.get("kind", "")).strip().lower()
        if not user_id:
            return JSONResponse({"error": {"code": -32602, "message": "user_id is required"}}, status_code=400)
        if kind not in (KIND_MCP, KIND_BRIDGE):
            return JSONResponse(
                {"error": {"code": -32602, "message": "kind must be 'mcp' or 'bridge'"}}, status_code=400)
        token = settings.token_store.issue(kind, user_id)
        return JSONResponse({
            "user_id": user_id,
            "kind": kind,
            "token": token,  # shown once; only the SHA-256 hash is stored
        })

    @app.post("/admin/tokens/revoke")
    async def admin_revoke_tokens(request: Request) -> JSONResponse:
        """Revoke tokens. Body: {"user_id": str, "kind": "mcp"|"bridge"} —
        each direction is revoked independently."""
        from .tokens import KIND_BRIDGE, KIND_MCP

        if not await _admin_guard(request):
            err = BridgeProtocolError(CODE_AUTH_FAILED, "admin access denied")
            return JSONResponse({"error": err.to_dict()}, status_code=403)
        try:
            body = await request.json()
        except Exception:
            return JSONResponse({"error": {"code": -32602, "message": "invalid JSON body"}}, status_code=400)
        user_id = str(body.get("user_id", "")).strip()
        kind = str(body.get("kind", "")).strip().lower()
        if not user_id or kind not in (KIND_MCP, KIND_BRIDGE):
            return JSONResponse(
                {"error": {"code": -32602, "message": "user_id and kind ('mcp'|'bridge') are required"}},
                status_code=400)
        revoked = settings.token_store.revoke_user(kind, user_id)
        return JSONResponse({"user_id": user_id, "kind": kind, "revoked": revoked})

    @app.get("/admin/tokens")
    async def admin_list_tokens(request: Request, user_id: str = "") -> JSONResponse:
        """List token metadata (kind/hint/created/revoked — never plaintext)."""
        from .tokens import KIND_BRIDGE, KIND_MCP

        if not await _admin_guard(request):
            err = BridgeProtocolError(CODE_AUTH_FAILED, "admin access denied")
            return JSONResponse({"error": err.to_dict()}, status_code=403)
        tokens = []
        for kind in (KIND_MCP, KIND_BRIDGE):
            for rec in settings.token_store.list_user(kind, user_id):
                if not user_id or rec["user_id"] == user_id:
                    tokens.append(rec)
        return JSONResponse({"tokens": tokens})

    return app


def _env_admin_token() -> str:
    import os

    return os.environ.get("LOCALFS_ADMIN_TOKEN", "")
