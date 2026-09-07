"""FastAPI 问答端点、错误协议和启动预加载测试。"""

from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.errors import ConversationNotFoundError, RetrievalError
from app.main import app, get_agent_graph, get_agent_run_store
from app.rag.models import KnowledgeAnswer
from app.rag.service import get_knowledge_qa_service


client = TestClient(app)


def make_event(sequence, event_type, node, message, status):
    return {
        "sequence": sequence,
        "type": event_type,
        "node": node,
        "message": message,
        "status": status,
        "timestamp": datetime.now(UTC).isoformat(),
    }


class MemoryRunStore:
    """接口测试使用的确定性内存存储，避免依赖开发数据库状态。"""

    def __init__(self):
        self.runs = {}
        self.messages = {}

    async def create(self, run_id, question, top_k, conversation_id=None):
        now = datetime.now(UTC)
        if conversation_id and conversation_id not in self.messages:
            raise ConversationNotFoundError()
        conversation_id = conversation_id or run_id
        run = SimpleNamespace(
            run_id=run_id,
            conversation_id=conversation_id,
            question=question,
            top_k=top_k,
            status="queued",
            current_node="queued",
            intent=None,
            result=None,
            events=[make_event(1, "run.created", "queued", "任务已创建", "queued")],
            error_code=None,
            error_message=None,
            created_at=now,
            started_at=None,
            completed_at=None,
        )
        self.runs[run_id] = run
        self.messages.setdefault(conversation_id, []).append(
            {"run_id": run_id, "role": "user", "content": question}
        )
        return run

    async def get_context(self, conversation_id, *, exclude_run_id):
        return [
            {"role": message["role"], "content": message["content"]}
            for message in self.messages.get(conversation_id, [])
            if message["run_id"] != exclude_run_id
            and self.runs[message["run_id"]].status == "completed"
        ]

    async def get(self, run_id):
        return self.runs.get(run_id)

    async def mark_running(self, run_id):
        run = self.runs[run_id]
        run.status = "running"
        run.current_node = "workflow"
        run.started_at = datetime.now(UTC)
        run.events.append(
            make_event(2, "workflow.started", "workflow", "工作流开始", "running")
        )

    async def record_node(self, run_id, *, sequence, node, intent):
        run = self.runs[run_id]
        run.current_node = node
        run.intent = intent
        run.events.append(
            make_event(sequence, "node.completed", node, "节点完成", "running")
        )

    async def complete(self, run_id, *, sequence, result, intent):
        run = self.runs[run_id]
        run.status = "completed"
        run.current_node = "completed"
        run.intent = intent
        run.result = result
        run.completed_at = datetime.now(UTC)
        run.events.append(
            make_event(
                sequence, "run.completed", "completed", "分析完成", "completed"
            )
        )
        self.messages[run.conversation_id].append(
            {"run_id": run_id, "role": "assistant", "content": result["answer"]}
        )

    async def fail(
        self, run_id, *, sequence, error_code, error_message
    ):
        run = self.runs[run_id]
        run.status = "failed"
        run.current_node = "failed"
        run.error_code = error_code
        run.error_message = error_message
        run.completed_at = datetime.now(UTC)
        run.events.append(
            make_event(sequence, "run.failed", "failed", error_message, "failed")
        )
        self.messages[run.conversation_id].append(
            {"run_id": run_id, "role": "assistant", "content": error_message}
        )


@pytest.fixture(autouse=True)
def clear_dependency_overrides():
    app.dependency_overrides.clear()
    store = MemoryRunStore()
    app.dependency_overrides[get_agent_run_store] = lambda: store
    yield store
    app.dependency_overrides.clear()


class FakeGraph:
    async def ainvoke(self, state):
        assert state["question"] == "退款率的口径是什么？"
        return {
            **state,
            "intent": "knowledge_qa",
            "answer": "退款率等于退款成功金额除以支付商品金额。[1]",
            "citations": [
                {
                    "index": 1,
                    "chunk_id": "a" * 64,
                    "document_id": "metric_refund_rate",
                    "title": "退款率指标定义",
                    "source": "指标手册",
                    "version": "1.0",
                    "updated_at": "2026-07-16",
                    "metric_name": "refund_rate",
                    "retrieval_score": 0.9,
                    "rerank_score": 0.99,
                }
            ],
            "model": "deepseek-v4-pro",
            "usage": {
                "prompt_tokens": 100,
                "completion_tokens": 20,
                "total_tokens": 120,
            },
        }


def test_knowledge_question_chain():
    app.dependency_overrides[get_agent_graph] = FakeGraph
    request_id = "test-knowledge-request"
    response = client.post(
        "/api/v1/runs",
        json={"question": "退款率的口径是什么？"},
        headers={"X-Request-ID": request_id},
    )

    assert response.status_code == 202

    created = response.json()
    assert created["status"] == "queued"
    assert created["conversation_id"] == created["run_id"]
    result = client.get(created["result_url"]).json()
    assert result["status"] == "completed"
    assert result["intent"] == "knowledge_qa"
    assert result["run_id"]
    assert result["conversation_id"] == created["conversation_id"]
    assert result["citations"][0]["document_id"] == "metric_refund_rate"
    assert result["model"] == "deepseek-v4-pro"
    assert response.headers["X-Request-ID"] == request_id
    assert response.headers["Content-Type"] == "application/json; charset=utf-8"


class FakeDataGraph:
    async def ainvoke(self, state):
        return {
            **state,
            "intent": "data_analysis",
            "answer": "支付 GMV 为 22,682.00 元。",
            "citations": [],
            "model": "deepseek-v4-pro",
            "usage": {"total_tokens": 100},
            "analysis": {
                "value": 22682.0,
                "unit": "元",
                "sql": "SELECT 22682 AS gmv LIMIT 1000",
                "evidence": {
                    "metric": {
                        "code": "gmv",
                        "name": "支付 GMV",
                        "description": "支付成功金额合计",
                        "formula": "SUM(payments.paid_amount)",
                        "unit": "元",
                        "version": "1.0",
                    },
                    "columns": ["gmv"],
                    "rows": [{"gmv": 22682.0}],
                    "row_count": 1,
                    "explain": [{"table": "payments", "type": "range"}],
                    "tables": ["orders", "payments"],
                    "execution_user": "insight_reader",
                    "timeout_ms": 5000,
                    "assumptions": ["上周按完整自然周计算"],
                },
            },
        }


def test_data_analysis_response_contains_value_sql_and_evidence():
    app.dependency_overrides[get_agent_graph] = FakeDataGraph
    response = client.post(
        "/api/v1/runs",
        json={"question": "上周华东区 GMV 是多少？"},
    )

    assert response.status_code == 202
    result = client.get(response.json()["result_url"]).json()
    assert result["intent"] == "data_analysis"
    assert result["analysis"]["value"] == 22682.0
    assert result["analysis"]["sql"].endswith("LIMIT 1000")
    assert result["analysis"]["evidence"]["execution_user"] == "insight_reader"
    assert result["analysis"]["evidence"]["rows"] == [{"gmv": 22682.0}]


class FakeForecastGraph:
    async def ainvoke(self, state):
        assert state["forecast_days"] == 2
        assert state["forecast_start_date"] is None
        return {
            **state,
            "intent": "sales_forecast",
            "answer": "本地 PyTorch 模型预测未来两天销量为 21 件。",
            "citations": [],
            "model": "torch-global-test",
            "usage": {"total_tokens": 300},
            "forecast": {
                "subject_type": "category",
                "subject_name": "耳机",
                "forecast_start_date": "2026-07-13",
                "forecast_end_date": "2026-07-14",
                "horizon_days": 2,
                "data_through": "2026-07-12",
                "points": [
                    {
                        "date": "2026-07-13",
                        "predicted_quantity": 10,
                        "lower_bound": 8,
                        "upper_bound": 12,
                    },
                    {
                        "date": "2026-07-14",
                        "predicted_quantity": 11,
                        "lower_bound": 9,
                        "upper_bound": 13,
                    },
                ],
                "total_predicted_quantity": 21,
                "model_version": "torch-global-test",
                "generation_method": "torch_global",
                "prediction_device": "cuda:test-gpu",
                "explanation_model": "deepseek-test",
                "history_start_date": "2026-04-14",
                "history_days": 90,
                "historical_daily_average": 9.5,
                "reasoning_summary": "近期销量平稳。",
                "replenishment": {
                    "current_stock": 10,
                    "forecast_demand": 21,
                    "safety_stock": 32,
                    "target_stock": 53,
                    "recommended_quantity": 43,
                    "safety_stock_days": 3,
                },
                "assumptions": ["未来经营条件稳定"],
            },
        }


def test_forecast_api_accepts_user_selected_horizon():
    app.dependency_overrides[get_agent_graph] = FakeForecastGraph
    response = client.post(
        "/api/v1/runs",
        json={"question": "预测耳机销量", "forecast_days": 2},
    )

    assert response.status_code == 202
    result = client.get(response.json()["result_url"]).json()
    assert result["intent"] == "sales_forecast"
    assert result["forecast"]["horizon_days"] == 2
    assert result["usage"]["total_tokens"] == 300


def test_run_events_are_replayable_and_finish_at_terminal_event():
    app.dependency_overrides[get_agent_graph] = FakeGraph
    created = client.post(
        "/api/v1/runs",
        json={"question": "退款率的口径是什么？"},
    ).json()

    response = client.get(created["events_url"])

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/event-stream")
    assert "event: run.created" in response.text
    assert "event: run.completed" in response.text

    resumed = client.get(created["events_url"], headers={"Last-Event-ID": "3"})
    assert "id: 1" not in resumed.text
    assert "event: run.completed" in resumed.text


class MultiTurnGraph:
    def __init__(self):
        self.states = []

    async def ainvoke(self, state):
        self.states.append(state)
        return {
            **state,
            "intent": "data_analysis",
            "answer": "本轮分析完成。",
            "citations": [],
            "model": "deepseek-test",
            "usage": {"total_tokens": 10},
        }


def test_follow_up_reuses_conversation_and_loads_completed_history():
    graph = MultiTurnGraph()
    app.dependency_overrides[get_agent_graph] = lambda: graph

    first = client.post(
        "/api/v1/runs",
        json={"question": "上周华东区 GMV 是多少？"},
    ).json()
    second = client.post(
        "/api/v1/runs",
        json={
            "conversation_id": first["conversation_id"],
            "question": "那华南呢？",
        },
    ).json()

    assert second["conversation_id"] == first["conversation_id"]
    assert second["run_id"] != first["run_id"]
    assert graph.states[0]["history_messages"] == []
    assert graph.states[1]["history_messages"] == [
        {"role": "user", "content": "上周华东区 GMV 是多少？"},
        {"role": "assistant", "content": "本轮分析完成。"},
    ]


def test_follow_up_for_missing_conversation_returns_stable_404():
    app.dependency_overrides[get_agent_graph] = FakeGraph

    response = client.post(
        "/api/v1/runs",
        json={"conversation_id": "missing", "question": "那华南呢？"},
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "conversation_not_found"


def test_run_not_found_has_stable_error_response():
    response = client.get("/api/v1/runs/missing")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "run_not_found"


def test_root_serves_native_analysis_page():
    response = client.get("/")

    assert response.status_code == 200
    assert "InsightAgent" in response.text
    assert "question-form" in response.text


class FakeKnowledgeService:
    async def answer(self, question, top_k=None):
        assert question == "支付 GMV 是什么？"
        assert top_k == 2
        return KnowledgeAnswer(
            answer="支付 GMV 是支付成功订单的商品实付金额合计。[1]",
            citations=[],
            model="deepseek-v4-pro",
            usage=None,
        )


def test_dedicated_knowledge_answer_endpoint():
    app.dependency_overrides[get_knowledge_qa_service] = FakeKnowledgeService
    response = client.post(
        "/api/v1/knowledge/answers",
        json={"question": "支付 GMV 是什么？", "top_k": 2},
    )

    assert response.status_code == 200
    assert response.json()["intent"] == "knowledge_qa"
    assert response.json()["answer"].endswith("[1]")


class FailingKnowledgeService:
    async def answer(self, question, top_k=None):
        raise RetrievalError("private Milvus details")


def test_dependency_errors_have_stable_public_response():
    app.dependency_overrides[get_knowledge_qa_service] = FailingKnowledgeService
    response = client.post(
        "/api/v1/knowledge/answers",
        json={"question": "退款率是什么？"},
        headers={"X-Request-ID": "error-request"},
    )

    assert response.status_code == 503
    assert response.json()["error"]["code"] == "retrieval_unavailable"
    assert "private Milvus details" not in response.text
    assert response.headers["X-Request-ID"] == "error-request"


def test_application_lifespan_preloads_models(monkeypatch):
    import app.main as main_module

    calls = []
    monkeypatch.setattr(
        main_module,
        "get_knowledge_qa_service",
        lambda: calls.append("preloaded"),
    )
    monkeypatch.setattr(main_module.settings, "model_preload_enabled", True)

    with TestClient(main_module.app):
        pass

    assert calls == ["preloaded"]


def test_readiness_includes_mysql(monkeypatch):
    import app.main as main_module

    async def mysql_ready(settings):
        return True

    monkeypatch.setattr(
        main_module,
        "check_readiness",
        lambda: ({"deepseek": True, "milvus": True, "models": True}, 25),
    )
    monkeypatch.setattr(main_module, "check_mysql_connection", mysql_ready)

    response = client.get("/api/v1/health/ready")

    assert response.status_code == 200
    assert response.json()["checks"]["mysql"] is True
    assert response.json()["knowledge_chunks"] == 25
