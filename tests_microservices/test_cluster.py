import httpx
import pytest
from app.auth.security import hash_password
from app.config import get_settings
from services.identity.models import User
from services.identity.db import session_factory
from services.chat.worker import runner
from tests_microservices.test_gateway import Redis


@pytest.mark.asyncio
async def test_gateway_identity_chat_integration_with_real_sessions(
    databases, monkeypatch
):
    from services.identity.api import app as identity
    from services.chat.api import app as chat
    from services.knowledge.api import app as knowledge
    from services.gateway.api import app as gateway

    apps = {"identity": identity, "chat": chat, "knowledge": knowledge}

    class Cluster(httpx.AsyncBaseTransport):
        async def handle_async_request(self, request):
            return await httpx.ASGITransport(
                app=apps[request.url.host]
            ).handle_async_request(request)

    class Workflow:
        async def astream(self, state, stream_mode):
            yield {
                "answer_without_knowledge": {"answer": "资料不足。", "citations": []}
            }

    monkeypatch.setattr("services.chat.worker.get_rag_workflow", lambda: Workflow())
    async with session_factory() as session:
        for name, role in [
            ("alice", "employee"),
            ("bob", "employee"),
            ("admin", "admin"),
        ]:
            session.add(
                User(
                    user_id=name,
                    username=name,
                    normalized_username=name,
                    display_name=name,
                    password_hash=hash_password("Password12345!"),
                    role=role,
                    status="active",
                    must_change_password=False,
                    failed_login_count=0,
                )
            )
        await session.commit()
    async with httpx.AsyncClient(transport=Cluster()) as internal:
        monkeypatch.setattr("packages.platform.client.pooled_client", lambda: internal)
        gateway.state.http = internal
        gateway.state.redis = Redis()
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=gateway), base_url="http://work.example"
        ) as browser:

            async def login(name):
                browser.cookies.clear()
                response = await browser.post(
                    "/api/v1/auth/login",
                    json={"username": name, "password": "Password12345!"},
                )
                assert response.status_code == 200, response.text
                return browser.cookies.get(get_settings().auth_csrf_cookie_name)

            csrf = await login("alice")
            response = await browser.post(
                "/api/v1/runs",
                json={"question": "审批怎么做？"},
                headers={"Origin": "http://evil.example", "X-CSRF-Token": csrf},
            )
            assert response.status_code == 403
            response = await browser.post(
                "/api/v1/runs",
                json={"question": "审批怎么做？"},
                headers={"Origin": "http://work.example", "X-CSRF-Token": csrf},
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            await runner().execute(run_id)
            assert (await browser.get("/api/v1/runs/" + run_id)).json()[
                "status"
            ] == "completed"
            await login("bob")
            assert (await browser.get("/api/v1/runs/" + run_id)).status_code == 404
            csrf = await login("admin")
            assert (await browser.get("/api/v1/conversations")).status_code == 403
            assert (await browser.get("/api/v1/admin/users")).status_code == 200
