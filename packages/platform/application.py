from contextlib import asynccontextmanager
import secrets
from time import perf_counter
from uuid import uuid4
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from packages.platform.config import settings
from app.errors import AppError
from app.logging_config import configure_logging, get_logger, request_id_context


def create_app(domain, *, startup=None, shutdown=None):
    @asynccontextmanager
    async def lifespan(app):
        settings().token()
        if startup:
            await startup()
        try:
            yield
        finally:
            if shutdown:
                await shutdown()
            from packages.platform.client import close_clients
            await close_clients()

    configure_logging()
    logger = get_logger(domain)
    app = FastAPI(title=f"EvidenceRAG {domain}", version="2.0.0", lifespan=lifespan)

    @app.exception_handler(AppError)
    async def error_handler(request, exc):
        return JSONResponse(status_code=exc.status_code, content={"error": {
            "code": exc.code, "message": exc.public_message,
            "request_id": request_id_context.get(),
        }}, headers={"Cache-Control": "no-store"})

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        token = request_id_context.set(request.headers.get("x-request-id") or str(uuid4()))
        started = perf_counter()
        try:
            if domain != "gateway" and not request.url.path.startswith("/api/v1/health"):
                if not secrets.compare_digest(request.headers.get("x-service-token", ""), settings().token()):
                    return JSONResponse(status_code=401, content={"error": {"code": "service_authentication_required"}})
            response = await call_next(request)
            response.headers["X-Request-ID"] = request_id_context.get()
            response.headers.setdefault("Cache-Control", "no-store")
            response.headers.setdefault("X-Content-Type-Options", "nosniff")
            response.headers.setdefault("X-Frame-Options", "DENY")
            response.headers.setdefault("Referrer-Policy", "no-referrer")
            response.headers.setdefault("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; connect-src 'self'; frame-ancestors 'none'; base-uri 'self'; form-action 'self'")
            if domain == "gateway" and hasattr(request.app.state, "redis"):
                try:
                    from services.gateway.metrics import record
                    await record(request.app.state.redis, response.status_code, (perf_counter()-started)*1000)
                except Exception:
                    logger.warning("metrics sink unavailable")
            logger.info("request method=%s path=%s status=%s duration_ms=%.2f", request.method,
                        request.url.path, response.status_code, (perf_counter()-started)*1000)
            return response
        finally:
            request_id_context.reset(token)

    @app.get("/api/v1/health")
    async def health():
        return {"status": "ok", "service": domain}

    if domain in {"identity", "chat", "knowledge"}:
        @app.get("/api/v1/health/ready")
        async def ready():
            from importlib import import_module
            from sqlalchemy import text
            database = import_module(f"services.{domain}.db").database
            try:
                async with database.session_factory() as session:
                    await session.execute(text("SELECT 1"))
                return {"status": "ready", "service": domain}
            except Exception:
                return JSONResponse(status_code=503, content={"status": "not_ready", "service": domain})

    return app
