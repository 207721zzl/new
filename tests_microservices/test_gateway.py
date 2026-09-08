import httpx
import pytest
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
