from types import SimpleNamespace
import httpx
import pytest
from sqlalchemy import select
from packages.platform.auth import require_employee, require_employee_csrf, AuthContext
from services.chat.api import app
from services.chat.models import Message
from services.chat.db import session_factory
from services.chat.worker import runner
from tests_microservices.conftest import TOKEN


@pytest.mark.asyncio
async def test_async_chat_keeps_ownership_events_and_one_final_message(
    databases, monkeypatch
):
    class Workflow:
        async def astream(self, state, stream_mode):
            yield {
                "answer_without_knowledge": {
                    "answer": "资料不足，无法回答。",
                    "citations": [],
                }
            }

    monkeypatch.setattr("services.chat.worker.get_rag_workflow", lambda: Workflow())
    context = AuthContext(
        SimpleNamespace(
            user_id="employee-1", role="employee", must_change_password=False
        )
    )
    app.dependency_overrides[require_employee] = lambda: context
    app.dependency_overrides[require_employee_csrf] = lambda: context
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://chat",
            headers={"X-Service-Token": TOKEN},
        ) as client:
            response = await client.post(
                "/api/v1/runs", json={"question": "政策是什么？", "top_k": 5}
            )
            assert response.status_code == 202, response.text
            run_id = response.json()["run_id"]
            assert (await client.get(f"/api/v1/runs/{run_id}")).json()[
                "status"
            ] == "queued"
            await runner().execute(run_id)
            await runner().execute(run_id)
            detail = (await client.get(f"/api/v1/runs/{run_id}")).json()
            assert detail["status"] == "completed", detail
            sequences = [event["sequence"] for event in detail["events"]]
            assert sequences == sorted(set(sequences))
            stream = await client.get(
                f"/api/v1/runs/{run_id}/events",
                headers={"Last-Event-ID": str(sequences[-2])},
            )
            assert stream.text.count("event:") == 1
            async with session_factory() as session:
                messages = list(
                    await session.scalars(
                        select(Message).where(
                            Message.run_id == run_id, Message.role == "assistant"
                        )
                    )
                )
                assert len(messages) == 1
            context.user.user_id = "employee-2"
            assert (await client.get(f"/api/v1/runs/{run_id}")).status_code == 404
            assert (
                await client.get(f"/api/v1/runs/{run_id}/events")
            ).status_code == 404
    finally:
        app.dependency_overrides.clear()
