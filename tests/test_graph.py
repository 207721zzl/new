"""LangGraph 意图路由与知识问答分支测试。"""

import asyncio
from types import SimpleNamespace

import pytest

from app import graph
from app.analysis.models import (
    ComparisonAnalysis,
    DataAnalysisAnswer,
    DataAnalysisResult,
    MetricEvidence,
    QueryEvidence,
    Text2SQLRepairContext,
)
from app.intent.models import (
    IntentClassificationCompletion,
    IntentClassificationResult,
)
from app.errors import ConfigurationError, RetrievalError, UnsafeSQLError
from app.rag.models import KnowledgeAnswer, QueryRewriteOutcome


class PassthroughQueryRewrite:
    async def rewrite_query(self, question):
        return QueryRewriteOutcome(
            original_query=question,
            rewritten_query=question,
            applied=False,
            fallback=False,
            reason="测试透传",
        )


class FakeIntentClassifier:
    def __init__(self, intent, *, rewritten_question=None):
        self.intent = intent
        self.rewritten_question = rewritten_question

    async def classify(self, question, history_messages=None):
        rewritten = self.rewritten_question or question
        return IntentClassificationCompletion(
            result=IntentClassificationResult(
                intent=self.intent,
                confidence=0.99,
                rewritten_question=rewritten,
                reason="测试分类结果",
            ),
            model="deepseek-intent-test",
            usage={"total_tokens": 20},
        )


def patch_intent(monkeypatch, intent, *, rewritten_question=None):
    classifier = FakeIntentClassifier(
        intent,
        rewritten_question=rewritten_question,
    )
    monkeypatch.setattr(graph, "get_intent_classifier", lambda: classifier)


class FakeKnowledgeService(PassthroughQueryRewrite):
    async def retrieve(self, question, top_k=None):
        assert question == "退款率的口径是什么？"
        assert top_k == 2
        return ["ranked-context"]

    async def generate_answer(self, question, ranked):
        assert question == "退款率的口径是什么？"
        assert ranked == ["ranked-context"]
        return KnowledgeAnswer(
            answer="退款率按退款金额率计算。[1]",
            citations=[{"index": 1}],
            model="deepseek-v4-pro",
            usage={"total_tokens": 10},
        )


def test_graph_routes_knowledge_intent_into_rag_service(monkeypatch):
    patch_intent(monkeypatch, "knowledge_qa")
    monkeypatch.setattr(
        graph,
        "get_knowledge_qa_service",
        lambda: FakeKnowledgeService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "test-run",
                "question": "退款率的口径是什么？",
                "top_k": 2,
            }
        )
    )

    assert result["intent"] == "knowledge_qa"
    assert result["answer"].endswith("[1]")
    assert result["citations"] == [{"index": 1}]
    assert result["model"] == "deepseek-v4-pro"


def test_rag_graph_skips_generation_when_retrieval_has_no_evidence(monkeypatch):
    patch_intent(monkeypatch, "knowledge_qa")

    class EmptyKnowledgeService(PassthroughQueryRewrite):
        generated = False

        async def retrieve(self, question, top_k=None):
            return []

        async def generate_answer(self, question, ranked):
            self.generated = True
            raise AssertionError("generation must not run without evidence")

        @staticmethod
        def insufficient_answer():
            return KnowledgeAnswer(
                answer="现有知识库资料不足以回答该问题。",
                citations=[],
                model=None,
                usage=None,
            )

    service = EmptyKnowledgeService()
    monkeypatch.setattr(graph, "get_knowledge_qa_service", lambda: service)

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {"run_id": "rag-insufficient", "question": "不存在的知识"}
        )
    )

    assert result["answer"] == "现有知识库资料不足以回答该问题。"
    assert result["citations"] == []
    assert service.generated is False


class FlakyKnowledgeService(PassthroughQueryRewrite):
    def __init__(self, *, failures: int, error_factory=None):
        self.failures = failures
        self.error_factory = error_factory or RetrievalError
        self.calls = 0

    async def retrieve(self, question, top_k=None):
        self.calls += 1
        if self.calls <= self.failures:
            raise self.error_factory("temporary test failure")
        return ["ranked-context"]

    async def generate_answer(self, question, ranked):
        return KnowledgeAnswer(
            answer="重试后成功。[1]",
            citations=[{"index": 1}],
            model="deepseek-retry-test",
            usage=None,
        )


def test_langgraph_retries_retryable_rag_node_at_most_three_attempts(monkeypatch):
    patch_intent(monkeypatch, "knowledge_qa")
    service = FlakyKnowledgeService(failures=2)
    monkeypatch.setattr(graph, "get_knowledge_qa_service", lambda: service)

    result = asyncio.run(
        graph.agent_graph.ainvoke({"run_id": "retry-success", "question": "测试重试"})
    )

    assert service.calls == 3
    assert result["answer"] == "重试后成功。[1]"


def test_langgraph_stops_after_three_retryable_attempts(monkeypatch):
    patch_intent(monkeypatch, "knowledge_qa")
    service = FlakyKnowledgeService(failures=99)
    monkeypatch.setattr(graph, "get_knowledge_qa_service", lambda: service)

    with pytest.raises(RetrievalError):
        asyncio.run(
            graph.agent_graph.ainvoke(
                {"run_id": "retry-exhausted", "question": "测试重试上限"}
            )
        )

    assert service.calls == 3


def test_langgraph_does_not_retry_non_retryable_configuration_error(monkeypatch):
    patch_intent(monkeypatch, "knowledge_qa")
    service = FlakyKnowledgeService(
        failures=99,
        error_factory=ConfigurationError,
    )
    monkeypatch.setattr(graph, "get_knowledge_qa_service", lambda: service)

    with pytest.raises(ConfigurationError):
        asyncio.run(
            graph.agent_graph.ainvoke(
                {"run_id": "no-retry", "question": "测试不可重试错误"}
            )
        )

    assert service.calls == 1


def test_deepseek_route_rewrites_follow_up_and_returns_routing_metadata(monkeypatch):
    patch_intent(
        monkeypatch,
        "data_analysis",
        rewritten_question="查询上周华南区 GMV",
    )
    state = {
        "question": "那华南呢？",
        "history_messages": [
            {"role": "user", "content": "上周华东区 GMV 是多少？"},
        ],
    }
    result = asyncio.run(graph.route_intent(state))

    assert result["intent"] == "data_analysis"
    assert result["contextualized_question"] == "查询上周华南区 GMV"
    assert result["routing"]["model"] == "deepseek-intent-test"
    assert result["routing"]["confidence"] == 0.99


def test_rag_query_rewrite_is_used_only_for_retrieval(monkeypatch):
    patch_intent(
        monkeypatch,
        "knowledge_qa",
        rewritten_question="退款率怎么算？",
    )

    class RewritingKnowledgeService:
        async def rewrite_query(self, question):
            assert question == "退款率怎么算？"
            return QueryRewriteOutcome(
                original_query=question,
                rewritten_query="退款率（退款金额率）的指标定义、计算公式与统计口径",
                applied=True,
                fallback=False,
                reason="补充检索同义表达",
                model="deepseek-rewrite-test",
                usage={"total_tokens": 12},
            )

        async def retrieve(self, question, top_k=None):
            assert question == "退款率（退款金额率）的指标定义、计算公式与统计口径"
            return ["ranked-context"]

        async def generate_answer(self, question, ranked):
            assert question == "退款率怎么算？"
            return KnowledgeAnswer(
                answer="退款率按退款金额率计算。[1]",
                citations=[{"index": 1}],
                model="deepseek-answer-test",
                usage=None,
            )

    monkeypatch.setattr(
        graph,
        "get_knowledge_qa_service",
        lambda: RewritingKnowledgeService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {"run_id": "rewrite-rag", "question": "退款率怎么算？"}
        )
    )

    assert result["answer"].endswith("[1]")
    assert result["retrieval_query"].startswith("退款率（退款金额率）")
    assert result["routing"]["query_rewrite"]["applied"] is True
    assert result["routing"]["query_rewrite"]["usage"] == {"total_tokens": 12}


class FakeDataAnalysisService:
    settings = SimpleNamespace(
        agent_node_max_attempts=3,
        text2sql_max_repairs=2,
    )

    async def load_metrics(self):
        return ["gmv-metric"]

    async def generate_sql(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        assert question == "上周华东区 GMV 是多少？"
        assert analysis_type == "metric"
        assert metrics == ["gmv-metric"]
        assert repair_context is None
        return SimpleNamespace(
            result=SimpleNamespace(sql="SELECT 57970 AS gmv LIMIT 1000"),
            usage={"total_tokens": 100},
        )

    @staticmethod
    def merge_usage(total, current):
        return current or total

    async def validate_and_execute(
        self,
        question,
        metrics,
        completion,
        *,
        analysis_type="metric",
    ):
        assert question == "上周华东区 GMV 是多少？"
        assert metrics == ["gmv-metric"]
        assert completion.result.sql.startswith("SELECT")
        assert analysis_type == "metric"
        return "executed"

    def finalize_answer(
        self,
        attempt,
        *,
        repair_history,
        aggregate_usage,
    ):
        assert attempt == "executed"
        assert aggregate_usage == {"total_tokens": 100}
        evidence = QueryEvidence(
            metric=MetricEvidence(
                code="gmv",
                name="支付 GMV",
                description="支付成功金额合计",
                formula="SUM(payments.paid_amount)",
                unit="元",
                version="1.0",
            ),
            columns=["gmv"],
            rows=[{"gmv": 22682.0}],
            row_count=1,
            explain=[],
            tables=["orders", "payments"],
            execution_user="insight_reader",
            timeout_ms=5000,
        )
        return DataAnalysisAnswer(
            answer="支付 GMV 为 22,682.00 元。",
            analysis=DataAnalysisResult(
                value=22682.0,
                unit="元",
                sql="SELECT 57970 AS gmv LIMIT 1000",
                evidence=evidence,
                repair_count=len(repair_history),
            ),
            model="deepseek-v4-pro",
            usage={"total_tokens": 100},
        )


def test_graph_routes_data_intent_into_text2sql_branch(monkeypatch):
    patch_intent(monkeypatch, "data_analysis")
    monkeypatch.setattr(
        graph,
        "get_data_analysis_service",
        lambda: FakeDataAnalysisService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "test-data-run",
                "question": "上周华东区 GMV 是多少？",
            }
        )
    )

    assert result["intent"] == "data_analysis"
    assert result["analysis"]["value"] == 22682.0
    assert result["analysis"]["evidence"]["execution_user"] == "insight_reader"
    assert result["model"] == "deepseek-v4-pro"


class RepairingDataAnalysisService(FakeDataAnalysisService):
    def __init__(self):
        self.generation_attempts = 0
        self.repair_attempts = []
        self.aggregate_usage = None

    async def generate_sql(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        self.generation_attempts += 1
        self.repair_attempts.append(
            repair_context.attempt if repair_context is not None else None
        )
        return SimpleNamespace(
            result=SimpleNamespace(
                sql=f"SELECT {self.generation_attempts} AS gmv LIMIT 1000"
            ),
            usage={"total_tokens": 10},
        )

    @staticmethod
    def merge_usage(total, current):
        return {
            "total_tokens": (total or {}).get("total_tokens", 0)
            + (current or {}).get("total_tokens", 0)
        }

    async def validate_and_execute(
        self,
        question,
        metrics,
        completion,
        *,
        analysis_type="metric",
    ):
        if self.generation_attempts < 3:
            raise UnsafeSQLError("test SQL needs repair")
        return "executed"

    @staticmethod
    def build_repair_context_for_failure(
        *,
        attempt,
        failure_type,
        previous_sql,
    ):
        return Text2SQLRepairContext(
            attempt=attempt,
            failure_type=failure_type,
            instruction="repair test SQL",
            previous_sql=previous_sql,
        )

    def finalize_answer(
        self,
        attempt,
        *,
        repair_history,
        aggregate_usage,
    ):
        self.aggregate_usage = aggregate_usage
        return super().finalize_answer(
            attempt,
            repair_history=repair_history,
            aggregate_usage={"total_tokens": 100},
        )


def test_text2sql_failures_loop_through_langgraph_and_stop_at_three(monkeypatch):
    patch_intent(monkeypatch, "data_analysis")
    service = RepairingDataAnalysisService()
    monkeypatch.setattr(graph, "get_data_analysis_service", lambda: service)

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "data-repair-loop",
                "question": "上周华东区 GMV 是多少？",
            }
        )
    )

    assert service.generation_attempts == 3
    assert service.repair_attempts == [None, 1, 2]
    assert service.aggregate_usage == {"total_tokens": 30}
    assert result["analysis"]["repair_count"] == 2


def test_text2sql_graph_raises_after_third_failed_generation(monkeypatch):
    patch_intent(monkeypatch, "data_analysis")

    class ExhaustedDataAnalysisService(RepairingDataAnalysisService):
        async def validate_and_execute(
            self,
            question,
            metrics,
            completion,
            *,
            analysis_type="metric",
        ):
            raise UnsafeSQLError("still unsafe after repair")

    service = ExhaustedDataAnalysisService()
    monkeypatch.setattr(graph, "get_data_analysis_service", lambda: service)

    with pytest.raises(UnsafeSQLError):
        asyncio.run(
            graph.agent_graph.ainvoke(
                {
                    "run_id": "data-repair-exhausted",
                    "question": "上周华东区 GMV 是多少？",
                }
            )
        )

    assert service.generation_attempts == 3
    assert service.repair_attempts == [None, 1, 2]


class FakeComparisonAnalysisService:
    settings = FakeDataAnalysisService.settings

    async def load_metrics(self):
        return ["gmv-metric"]

    async def generate_sql(
        self,
        question,
        metrics,
        *,
        analysis_type="metric",
        repair_context=None,
    ):
        assert "相比" in question
        assert analysis_type == "comparison"
        assert metrics == ["gmv-metric"]
        assert repair_context is None
        return SimpleNamespace(
            result=SimpleNamespace(sql="SELECT 22682 AS current_value LIMIT 1000"),
            usage={"total_tokens": 120},
        )

    merge_usage = staticmethod(FakeDataAnalysisService.merge_usage)

    async def validate_and_execute(
        self,
        question,
        metrics,
        completion,
        *,
        analysis_type="metric",
    ):
        assert "相比" in question
        assert metrics == ["gmv-metric"]
        assert completion.result.sql.startswith("SELECT")
        assert analysis_type == "comparison"
        return "executed-comparison"

    def finalize_answer(
        self,
        attempt,
        *,
        repair_history,
        aggregate_usage,
    ):
        assert attempt == "executed-comparison"
        assert repair_history == []
        assert aggregate_usage == {"total_tokens": 120}
        evidence = QueryEvidence(
            metric=MetricEvidence(
                code="gmv",
                name="支付 GMV",
                description="支付成功金额合计",
                formula="SUM(payments.paid_amount)",
                unit="元",
                version="1.0",
            ),
            columns=["current_value", "previous_value"],
            rows=[{"current_value": 22682.0, "previous_value": 18000.0}],
            row_count=1,
            explain=[],
            tables=["orders", "payments"],
            execution_user="insight_reader",
            timeout_ms=5000,
        )
        return DataAnalysisAnswer(
            answer="支付 GMV 当前周期较对比周期增长 26.01%。",
            analysis=DataAnalysisResult(
                value=22682.0,
                unit="元",
                sql="SELECT 22682 AS current_value LIMIT 1000",
                evidence=evidence,
                comparison=ComparisonAnalysis(
                    current_value=22682.0,
                    previous_value=18000.0,
                    change=4682.0,
                    change_rate=26.0111,
                    direction="increase",
                    current_period="2026-07-06 至 2026-07-13",
                    previous_period="2026-06-29 至 2026-07-06",
                ),
            ),
            model="deepseek-v4-pro",
            usage={"total_tokens": 120},
        )


def test_graph_routes_comparison_intent_into_comparison_analysis(monkeypatch):
    patch_intent(monkeypatch, "comparison_analysis")
    monkeypatch.setattr(
        graph,
        "get_data_analysis_service",
        lambda: FakeComparisonAnalysisService(),
    )

    result = asyncio.run(
        graph.agent_graph.ainvoke(
            {
                "run_id": "test-comparison-run",
                "question": "上周华东区 GMV 与前一周相比变化如何？",
            }
        )
    )

    assert result["intent"] == "comparison_analysis"
    assert result["analysis"]["comparison"]["direction"] == "increase"
    assert result["analysis"]["comparison"]["change_rate"] == 26.0111
