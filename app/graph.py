"""InsightAgent LangGraph 意图路由、RAG 与 Text2SQL 状态编排入口。"""

from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import RetryPolicy

from app.analysis.models import (
    AnalysisType,
    DataAnalysisAttemptResult,
    Text2SQLCompletion,
    Text2SQLRepairContext,
)
from app.analysis.service import get_data_analysis_service
from app.analysis.root_cause import get_root_cause_analysis_service
from app.config import Settings, get_settings
from app.errors import (
    ForecastGenerationError,
    GenerationError,
    IntentClassificationError,
    LocalModelError,
    RetrievalError,
    SQLExecutionError,
    Text2SQLGenerationError,
    UnsafeSQLError,
)
from app.forecast.service import get_sales_forecast_service
from app.intent.deepseek import get_intent_classifier
from app.logging_config import get_logger
from app.rag.models import QueryRewriteOutcome, RankedKnowledge
from app.rag.service import get_knowledge_qa_service
from app.schemas import Intent


logger = get_logger("graph")


def retry_repairable_analysis_error(exc: Exception) -> bool:
    """只重试可能通过再次计算恢复的归因分析错误。"""
    if isinstance(exc, Text2SQLGenerationError):
        return exc.repairable
    return isinstance(exc, SQLExecutionError)


def node_retry_policy(
    settings: Settings,
    retry_on,
) -> RetryPolicy:
    """创建最多三次总尝试的 LangGraph 节点重试策略。"""
    return RetryPolicy(
        initial_interval=0.2,
        backoff_factor=2.0,
        max_interval=2.0,
        max_attempts=settings.agent_node_max_attempts,
        jitter=True,
        retry_on=retry_on,
    )


class AgentState(TypedDict, total=False):
    """在 LangGraph 节点之间传递的运行状态。"""

    run_id: str
    conversation_id: str
    question: str
    history_messages: list[dict[str, str]]
    contextualized_question: str
    retrieval_query: str
    query_rewrite: QueryRewriteOutcome
    intent: Intent
    routing: dict
    answer: str
    top_k: int | None
    citations: list[dict]
    model: str | None
    usage: dict[str, int] | None
    analysis: dict | None
    forecast: dict | None
    forecast_days: int | None
    forecast_start_date: object | None
    forecast_end_date: object | None
    ranked_knowledge: list[RankedKnowledge]
    analysis_type: AnalysisType
    analysis_metrics: list
    analysis_completion: Text2SQLCompletion | None
    analysis_attempt_result: DataAnalysisAttemptResult | None
    analysis_attempt: int
    analysis_max_attempts: int
    analysis_status: Literal["ready", "generated", "executed", "failed"]
    analysis_failure_type: Literal["contract_error", "unsafe_sql", "execution_error"]
    analysis_failure_message: str
    analysis_failure_repairable: bool
    analysis_repair_context: Text2SQLRepairContext | None
    analysis_repair_history: list[Text2SQLRepairContext]
    analysis_usage: dict[str, int] | None


def effective_question(state: AgentState) -> str:
    """返回供路由和业务节点使用的完整问题。"""
    return state.get("contextualized_question") or state["question"]


async def classify_intent(
    question: str,
    history_messages: list[dict[str, str]] | None = None,
) -> Intent:
    """通过 DeepSeek 返回单个意图，供评测与外部调用复用。"""
    completion = await get_intent_classifier().classify(question, history_messages)
    return completion.result.intent


async def route_intent(state: AgentState) -> dict:
    """调用 DeepSeek 同时完成意图识别和多轮问题改写。"""
    logger.info("node started node=route_intent")
    completion = await get_intent_classifier().classify(
        state["question"],
        state.get("history_messages") or [],
    )
    decision = completion.result
    routing = {
        "intent": decision.intent,
        "confidence": decision.confidence,
        "rewritten_question": decision.rewritten_question,
        "reason": decision.reason,
        "model": completion.model,
        "usage": completion.usage,
    }
    logger.info(
        "node completed node=route_intent intent=%s confidence=%.3f",
        decision.intent,
        decision.confidence,
    )
    return {
        "intent": decision.intent,
        "contextualized_question": decision.rewritten_question,
        "routing": routing,
    }


async def retrieve_knowledge(state: AgentState) -> dict:
    """RAG 第一步：检索、重排、过滤并恢复父块上下文。"""
    logger.info("node started node=retrieve_knowledge")
    ranked = await get_knowledge_qa_service().retrieve(
        state.get("retrieval_query") or effective_question(state),
        top_k=state.get("top_k"),
    )
    logger.info("node completed node=retrieve_knowledge contexts=%s", len(ranked))
    return {"ranked_knowledge": ranked}


async def rewrite_knowledge_query(state: AgentState) -> dict:
    """把上下文化问题改写为检索专用查询，并记录可观测元数据。"""
    logger.info("node started node=rewrite_knowledge_query")
    outcome = await get_knowledge_qa_service().rewrite_query(effective_question(state))
    rewrite_metadata = {
        "original_query": outcome.original_query,
        "rewritten_query": outcome.rewritten_query,
        "applied": outcome.applied,
        "fallback": outcome.fallback,
        "reason": outcome.reason,
        "model": outcome.model,
        "usage": outcome.usage,
    }
    routing = {
        **state.get("routing", {}),
        "retrieval_query": outcome.rewritten_query,
        "query_rewrite": rewrite_metadata,
    }
    logger.info(
        "node completed node=rewrite_knowledge_query applied=%s fallback=%s",
        outcome.applied,
        outcome.fallback,
    )
    return {
        "retrieval_query": outcome.rewritten_query,
        "query_rewrite": outcome,
        "routing": routing,
    }


def select_knowledge_path(state: AgentState) -> str:
    """只有证据通过检索和重排门槛时才进入生成节点。"""
    return "generate" if state.get("ranked_knowledge") else "insufficient"


async def generate_knowledge_answer(state: AgentState) -> dict:
    """RAG 第二步：只基于已恢复的父块上下文生成答案。"""
    logger.info("node started node=generate_knowledge_answer")
    result = await get_knowledge_qa_service().generate_answer(
        effective_question(state),
        state["ranked_knowledge"],
    )
    logger.info(
        "node completed node=generate_knowledge_answer citations=%s",
        len(result.citations),
    )
    return {
        "answer": result.answer,
        "citations": result.citations,
        "model": result.model,
        "usage": result.usage,
    }


async def answer_without_knowledge(state: AgentState) -> dict:
    """RAG 证据不足时不调用生成模型，直接稳定拒答。"""
    del state
    result = get_knowledge_qa_service().insufficient_answer()
    return {
        "answer": result.answer,
        "citations": result.citations,
        "model": result.model,
        "usage": result.usage,
    }


async def answer_out_of_scope(state: AgentState) -> dict:
    """处理经营分析能力范围之外的问题。"""
    del state
    return {
        "answer": "当前问题不在经营分析系统的能力范围内。",
        "citations": [],
        "model": None,
        "usage": None,
    }


async def prepare_data_analysis(state: AgentState) -> dict:
    """Text2SQL 第一步：加载指标并初始化可循环的图状态。"""
    logger.info("node started node=prepare_data_analysis")
    service = get_data_analysis_service()
    analysis_type = (
        "comparison" if state["intent"] == "comparison_analysis" else "metric"
    )
    metrics = await service.load_metrics()
    max_attempts = min(
        service.settings.agent_node_max_attempts,
        service.settings.text2sql_max_repairs + 1,
    )
    logger.info(
        "node completed node=prepare_data_analysis metrics=%s max_attempts=%s",
        len(metrics),
        max_attempts,
    )
    return {
        "analysis_type": analysis_type,
        "analysis_metrics": metrics,
        "analysis_completion": None,
        "analysis_attempt_result": None,
        "analysis_attempt": 0,
        "analysis_max_attempts": max_attempts,
        "analysis_status": "ready",
        "analysis_repair_context": None,
        "analysis_repair_history": [],
        "analysis_usage": None,
    }


def analysis_failure_patch(exc: Exception) -> dict:
    """把异常转换为可由条件边判断的稳定、脱敏状态。"""
    if isinstance(exc, UnsafeSQLError):
        failure_type = "unsafe_sql"
        repairable = True
    elif isinstance(exc, SQLExecutionError):
        failure_type = "execution_error"
        repairable = True
    else:
        failure_type = "contract_error"
        repairable = not isinstance(exc, Text2SQLGenerationError) or exc.repairable
    return {
        "analysis_status": "failed",
        "analysis_failure_type": failure_type,
        "analysis_failure_message": str(exc),
        "analysis_failure_repairable": repairable,
    }


async def generate_data_sql(state: AgentState) -> dict:
    """Text2SQL 第二步：执行一次生成，并把失败交给图条件边。"""
    logger.info(
        "node started node=generate_data_sql attempt=%s",
        state.get("analysis_attempt", 0) + 1,
    )
    service = get_data_analysis_service()
    attempt = state.get("analysis_attempt", 0) + 1
    try:
        completion = await service.generate_sql(
            effective_question(state),
            state["analysis_metrics"],
            analysis_type=state["analysis_type"],
            repair_context=state.get("analysis_repair_context"),
        )
    except Text2SQLGenerationError as exc:
        return {"analysis_attempt": attempt, **analysis_failure_patch(exc)}
    usage = service.merge_usage(state.get("analysis_usage"), completion.usage)
    logger.info("node completed node=generate_data_sql attempt=%s", attempt)
    return {
        "analysis_completion": completion,
        "analysis_attempt": attempt,
        "analysis_status": "generated",
        "analysis_usage": usage,
    }


async def validate_execute_data_sql(state: AgentState) -> dict:
    """Text2SQL 第三步：进行语义、安全校验并只读执行。"""
    logger.info(
        "node started node=validate_execute_data_sql attempt=%s",
        state["analysis_attempt"],
    )
    service = get_data_analysis_service()
    try:
        result = await service.validate_and_execute(
            effective_question(state),
            state["analysis_metrics"],
            state["analysis_completion"],
            analysis_type=state["analysis_type"],
        )
    except (Text2SQLGenerationError, UnsafeSQLError, SQLExecutionError) as exc:
        return analysis_failure_patch(exc)
    logger.info("node completed node=validate_execute_data_sql")
    return {
        "analysis_attempt_result": result,
        "analysis_status": "executed",
    }


def data_analysis_failure_path(state: AgentState) -> str:
    """失败后根据可修复性和剩余尝试次数决定修复或终止。"""
    if (
        state.get("analysis_failure_repairable", False)
        and state["analysis_attempt"] < state["analysis_max_attempts"]
    ):
        return "repair"
    return "failed"


def select_after_sql_generation(state: AgentState) -> str:
    """SQL 生成成功后进入校验执行，否则进入受控修复或失败。"""
    if state["analysis_status"] == "generated":
        return "execute"
    return data_analysis_failure_path(state)


def select_after_sql_execution(state: AgentState) -> str:
    """SQL 校验执行成功后汇总结果，否则进入受控修复或失败。"""
    if state["analysis_status"] == "executed":
        return "finalize"
    return data_analysis_failure_path(state)


async def prepare_data_repair(state: AgentState) -> dict:
    """把上一次失败转换成下一次生成可见的稳定修复反馈。"""
    service = get_data_analysis_service()
    completion = state.get("analysis_completion")
    previous_sql = completion.result.sql if completion is not None else None
    repair_context = service.build_repair_context_for_failure(
        attempt=len(state.get("analysis_repair_history", [])) + 1,
        failure_type=state["analysis_failure_type"],
        previous_sql=previous_sql,
    )
    history = [*state.get("analysis_repair_history", []), repair_context]
    logger.info(
        "node completed node=prepare_data_repair repair=%s failure_type=%s",
        repair_context.attempt,
        repair_context.failure_type,
    )
    return {
        "analysis_repair_context": repair_context,
        "analysis_repair_history": history,
        "analysis_completion": None,
        "analysis_status": "ready",
    }


async def finalize_data_analysis(state: AgentState) -> dict:
    """Text2SQL 最后一步：从成功图状态构造确定性答案和证据。"""
    result = get_data_analysis_service().finalize_answer(
        state["analysis_attempt_result"],
        repair_history=state.get("analysis_repair_history", []),
        aggregate_usage=state.get("analysis_usage"),
    )
    logger.info("node completed node=finalize_data_analysis")
    return {
        "answer": result.answer,
        "analysis": result.analysis.model_dump(mode="python"),
        "citations": [],
        "model": result.model,
        "usage": result.usage,
    }


async def fail_data_analysis(state: AgentState) -> dict:
    """重试耗尽后恢复稳定异常类型，交给统一运行错误处理。"""
    failure_type = state.get("analysis_failure_type", "contract_error")
    message = state.get("analysis_failure_message") or "Text2SQL workflow failed"
    if failure_type == "unsafe_sql":
        raise UnsafeSQLError(message)
    if failure_type == "execution_error":
        raise SQLExecutionError(message)
    raise Text2SQLGenerationError(
        message,
        repairable=state.get("analysis_failure_repairable", False),
    )


async def analyze_data(state: AgentState) -> dict:
    """保留直接节点调用兼容性；主图使用拆分后的 Text2SQL 节点。"""
    analysis_type = (
        "comparison" if state["intent"] == "comparison_analysis" else "metric"
    )
    result = await get_data_analysis_service().answer(
        effective_question(state),
        analysis_type=analysis_type,
    )
    return {
        "answer": result.answer,
        "analysis": result.analysis.model_dump(mode="python"),
        "citations": [],
        "model": result.model,
        "usage": result.usage,
    }


async def analyze_root_cause(state: AgentState) -> dict:
    """原因分析分支：执行双周期基线、多维贡献拆解和异常识别。"""
    logger.info("node started node=analyze_root_cause")
    result = await get_root_cause_analysis_service().answer(effective_question(state))
    logger.info("node completed node=analyze_root_cause")
    return {
        "answer": result.answer,
        "analysis": result.analysis.model_dump(mode="python"),
        "citations": [],
        "model": result.model,
        "usage": result.usage,
    }


async def forecast_sales(state: AgentState) -> dict:
    """销量预测分支：本地 PyTorch 生成数值，DeepSeek 只做解释。"""
    logger.info("node started node=forecast_sales")
    result = await get_sales_forecast_service().answer(
        effective_question(state),
        forecast_days=state.get("forecast_days"),
        forecast_start_date=state.get("forecast_start_date"),
        forecast_end_date=state.get("forecast_end_date"),
    )
    logger.info("node completed node=forecast_sales")
    return {
        "answer": result.answer,
        "forecast": result.forecast.model_dump(mode="python"),
        "citations": [],
        "model": result.model,
        "usage": result.usage,
    }


def select_intent_branch(state: AgentState) -> str:
    """把六类意图路由到相应的多步骤工作流。"""
    if state["intent"] == "knowledge_qa":
        return "knowledge"
    if state["intent"] == "root_cause_analysis":
        return "root_cause"
    if state["intent"] == "sales_forecast":
        return "forecast"
    if state["intent"] in {"data_analysis", "comparison_analysis"}:
        return "data_analysis"
    return "out_of_scope"


def build_graph(settings: Settings | None = None):
    """构建包含受控节点重试和独立业务分支的 Agent 工作流。"""
    settings = settings or get_settings()
    builder = StateGraph(AgentState)

    # RetryPolicy 的 max_attempts 包含首次调用，因此最大值 3 表示最多调用三次，
    # 而不是首次调用后再额外重试三次。
    builder.add_node(
        "route_intent",
        route_intent,
        retry_policy=node_retry_policy(settings, IntentClassificationError),
    )
    builder.add_node(
        "rewrite_knowledge_query",
        rewrite_knowledge_query,
    )
    builder.add_node(
        "retrieve_knowledge",
        retrieve_knowledge,
        retry_policy=node_retry_policy(
            settings,
            (RetrievalError, LocalModelError),
        ),
    )
    builder.add_node(
        "generate_knowledge_answer",
        generate_knowledge_answer,
        retry_policy=node_retry_policy(settings, GenerationError),
    )
    builder.add_node("answer_without_knowledge", answer_without_knowledge)
    builder.add_node("answer_out_of_scope", answer_out_of_scope)
    builder.add_node("prepare_data_analysis", prepare_data_analysis)
    builder.add_node("generate_data_sql", generate_data_sql)
    builder.add_node("validate_execute_data_sql", validate_execute_data_sql)
    builder.add_node("prepare_data_repair", prepare_data_repair)
    builder.add_node("finalize_data_analysis", finalize_data_analysis)
    builder.add_node("fail_data_analysis", fail_data_analysis)
    builder.add_node(
        "analyze_root_cause",
        analyze_root_cause,
        retry_policy=node_retry_policy(settings, retry_repairable_analysis_error),
    )
    builder.add_node(
        "forecast_sales",
        forecast_sales,
        retry_policy=node_retry_policy(settings, ForecastGenerationError),
    )

    builder.add_edge(START, "route_intent")
    builder.add_conditional_edges(
        "route_intent",
        select_intent_branch,
        {
            "knowledge": "rewrite_knowledge_query",
            "data_analysis": "prepare_data_analysis",
            "root_cause": "analyze_root_cause",
            "forecast": "forecast_sales",
            "out_of_scope": "answer_out_of_scope",
        },
    )
    builder.add_edge("rewrite_knowledge_query", "retrieve_knowledge")
    builder.add_conditional_edges(
        "retrieve_knowledge",
        select_knowledge_path,
        {
            "generate": "generate_knowledge_answer",
            "insufficient": "answer_without_knowledge",
        },
    )
    builder.add_edge("generate_knowledge_answer", END)
    builder.add_edge("answer_without_knowledge", END)
    builder.add_edge("answer_out_of_scope", END)
    builder.add_edge("prepare_data_analysis", "generate_data_sql")
    builder.add_conditional_edges(
        "generate_data_sql",
        select_after_sql_generation,
        {
            "execute": "validate_execute_data_sql",
            "repair": "prepare_data_repair",
            "failed": "fail_data_analysis",
        },
    )
    builder.add_conditional_edges(
        "validate_execute_data_sql",
        select_after_sql_execution,
        {
            "repair": "prepare_data_repair",
            "finalize": "finalize_data_analysis",
            "failed": "fail_data_analysis",
        },
    )
    builder.add_edge("prepare_data_repair", "generate_data_sql")
    builder.add_edge("finalize_data_analysis", END)
    builder.add_edge("analyze_root_cause", END)
    builder.add_edge("forecast_sales", END)

    return builder.compile()


agent_graph = build_graph()
