import httpx
import pytest
from fastapi import Request
from tests_microservices.conftest import TOKEN


class Redis:
    async def eval(self, *args):
        return 0

    async def ping(self):
        return True


def test_gateway_routes_admin_monitoring_to_chat_domain():
    from services.gateway.api import target_for

    assert target_for("/api/v1/admin/feedback") == "chat"
    assert target_for("/api/v1/admin/token-usage") == "chat"
    assert target_for("/api/v1/admin/knowledge/documents/example") == "knowledge"


@pytest.mark.asyncio
async def test_gateway_redirect_explains_when_another_login_replaced_session():
    from packages.platform.auth import get_optional_auth_context
    from services.gateway.api import app

    async def replaced_session(request: Request):
        request.state.auth_session_replaced = True
        return None

    app.dependency_overrides[get_optional_auth_context] = replaced_session
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://gateway",
        ) as browser:
            response = await browser.get("/", follow_redirects=False)
    finally:
        app.dependency_overrides.pop(get_optional_auth_context, None)

    assert response.status_code == 303
    assert response.headers["location"] == "/login?next=%2F&reason=session_replaced"


@pytest.mark.asyncio
async def test_proxy_preserves_cookies_and_strips_forged_identity(monkeypatch):
    from services.gateway.api import app

    captured = []

    async def upstream(request):
        captured.append(request)
        return httpx.Response(
            200,
            headers=[
                ("set-cookie", "session=one; HttpOnly"),
                ("set-cookie", "csrf=two"),
            ],
            json={"ok": True},
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        app.state.http = client
        app.state.redis = Redis()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://public.example"
        ) as browser:
            response = await browser.post(
                "/api/v1/auth/login",
                json={"username": "example"},
                headers={
                    "X-Service-Token": "forged",
                    "X-User-Id": "admin",
                    "X-Forwarded-Host": "evil.example",
                },
            )
            assert response.status_code == 200, response.text
            assert len(response.headers.get_list("set-cookie")) == 2
            assert captured[0].headers["x-service-token"] == TOKEN
            assert "x-user-id" not in captured[0].headers
            assert captured[0].headers["x-forwarded-host"] == "public.example"
            assert (
                await browser.post("/internal/v1/authenticate", json={})
            ).status_code == 404


@pytest.mark.asyncio
async def test_proxy_streams_sse_and_forwards_replay_cursor():
    from services.gateway.api import app

    class Stream(httpx.AsyncByteStream):
        async def __aiter__(self):
            yield b"id: 3\nevent: run.completed\ndata: {}\n\n"

    async def upstream(request):
        assert request.headers["last-event-id"] == "2"
        return httpx.Response(
            200, headers={"content-type": "text/event-stream"}, stream=Stream()
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        app.state.http = client
        app.state.redis = Redis()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app), base_url="http://gateway"
        ) as browser:
            response = await browser.get(
                "/api/v1/runs/example/events", headers={"Last-Event-ID": "2"}
            )
            assert response.status_code == 200 and "id: 3" in response.text
            assert response.headers["x-accel-buffering"] == "no"


@pytest.mark.asyncio
async def test_service_client_preserves_replaced_session_reason(monkeypatch):
    from app.errors import SessionReplacedError
    from packages.platform.client import ServiceClient

    async def upstream(_request):
        return httpx.Response(
            401,
            json={
                "error": {
                    "code": "session_replaced",
                    "message": "当前账号已在其他设备登录，本设备已自动退出。",
                }
            },
        )

    async with httpx.AsyncClient(transport=httpx.MockTransport(upstream)) as client:
        monkeypatch.setattr(
            "packages.platform.client.pooled_client", lambda: client
        )
        with pytest.raises(SessionReplacedError):
            await ServiceClient("identity").post("/internal/v1/authenticate", {})
