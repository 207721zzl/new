import json
import httpx
from fastapi import Request, Depends
from fastapi.responses import FileResponse, RedirectResponse, StreamingResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.background import BackgroundTask
from app.config import PROJECT_ROOT
from packages.platform.application import create_app
from packages.platform.auth import get_optional_auth_context
from packages.platform.config import settings
from services.gateway.operations import router as operations_router
from services.gateway.rate_limit import check


async def startup():
    import redis.asyncio as redis
    app.state.http = httpx.AsyncClient(timeout=httpx.Timeout(90, connect=5), trust_env=False,
        limits=httpx.Limits(max_connections=200, max_keepalive_connections=40))
    app.state.redis = redis.from_url(settings().rate_limit_url.get_secret_value(), decode_responses=True)


async def shutdown():
    await app.state.http.aclose()
    await app.state.redis.aclose()


app = create_app("gateway", startup=startup, shutdown=shutdown)
app.mount("/static", StaticFiles(directory=PROJECT_ROOT / "app/static"), name="static")
app.include_router(operations_router)


@app.get("/", include_in_schema=False)
async def home(context=Depends(get_optional_auth_context)):
    if context is None:
        return RedirectResponse("/login?next=%2F", 303)
    if context.user.role == "admin":
        return RedirectResponse("/admin", 303)
    return FileResponse(PROJECT_ROOT / "app/static/index.html")


@app.get("/login", include_in_schema=False)
async def login():
    return FileResponse(PROJECT_ROOT / "app/static/login.html")


@app.get("/admin", include_in_schema=False)
async def admin(context=Depends(get_optional_auth_context)):
    if context is None:
        return RedirectResponse("/login?next=%2Fadmin", 303)
    if context.user.role != "admin":
        return RedirectResponse("/", 303)
    return FileResponse(PROJECT_ROOT / "app/static/admin.html")


def target_for(path):
    if path.startswith(("/api/v1/auth/", "/api/v1/admin/users", "/api/v1/admin/audit-logs")):
        return "identity"
    if path.startswith(("/api/v1/knowledge/answers", "/api/v1/retrieval/", "/api/v1/answers",
                        "/api/v1/runs", "/api/v1/conversations", "/api/v1/feedback")):
        return "chat"
    if path.startswith(("/api/v1/knowledge/", "/api/v1/admin/knowledge/")):
        return "knowledge"
    return None


@app.get("/api/v1/health/ready")
async def ready():
    try:
        await app.state.redis.ping()
        return {"status": "ready", "checks": {"rate_limits": True}}
    except Exception:
        return JSONResponse(status_code=503, content={"status": "not_ready", "checks": {"rate_limits": False}})


HOP_HEADERS = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "te",
               "trailer", "transfer-encoding", "upgrade", "host", "content-length"}


@app.api_route("/api/v1/{path:path}", methods=["GET", "POST", "PUT", "PATCH", "DELETE"], include_in_schema=False)
async def proxy(path: str, request: Request):
    target = target_for(request.url.path)
    if target is None:
        return JSONResponse(status_code=404, content={"detail": "Not Found"})
    try:
        retry = await check(app.state.redis, request)
    except Exception:
        return JSONResponse(status_code=503, content={"error": {"code": "rate_limiter_unavailable", "message": "请求保护服务暂不可用。"}})
    if retry:
        return JSONResponse(status_code=429, content={"error": {"code": "rate_limit_exceeded", "message": "请求过于频繁，请稍后重试。"}}, headers={"Retry-After": str(retry)})
    # Reconstruct forwarding context; caller-supplied internal identity headers are never forwarded.
    excluded = HOP_HEADERS | {"x-service-token", "x-forwarded-host", "x-forwarded-proto", "x-forwarded-for", "cf-connecting-ip", "x-user-id", "x-user-role", "authorization"}
    headers = {key: value for key, value in request.headers.items() if key.lower() not in excluded}
    headers.update({"X-Service-Token": settings().token(), "X-Forwarded-Host": request.headers.get("host", ""),
                    "X-Forwarded-Proto": request.url.scheme})
    url = getattr(settings(), target+"_url") + request.url.path
    if request.url.query:
        url += "?"+request.url.query
    try:
        upstream = await app.state.http.send(app.state.http.build_request(request.method, url,
            headers=headers, content=request.stream()), stream=True)
    except httpx.HTTPError:
        return JSONResponse(status_code=503, content={"error": {"code": "dependency_unavailable", "message": "业务服务暂时不可用。"}})
    async def response_body():
        if upstream.is_stream_consumed:
            yield upstream.content
        else:
            async for chunk in upstream.aiter_raw():
                yield chunk
    response = StreamingResponse(response_body(), status_code=upstream.status_code,
        background=BackgroundTask(upstream.aclose))
    # Preserve duplicate Set-Cookie fields and stream bytes without buffering SSE.
    response.raw_headers = [(key, value) for key, value in upstream.headers.raw
        if key.decode().lower() not in HOP_HEADERS and not (upstream.is_stream_consumed and key.lower()==b"content-encoding")]
    if "text/event-stream" in upstream.headers.get("content-type", ""):
        response.headers["X-Accel-Buffering"] = "no"
    return response


def legacy_contract():
    return json.loads((PROJECT_ROOT / "packages/contracts/public-api.json").read_text(encoding="utf-8"))


app.openapi = legacy_contract
