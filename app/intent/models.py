"""意图识别结构化输出契约。"""

from pydantic import BaseModel, ConfigDict, Field

from app.schemas import Intent


class IntentClassificationResult(BaseModel):
    """DeepSeek 对当前问题给出的可校验路由决策。"""

    model_config = ConfigDict(extra="forbid")

    intent: Intent
    confidence: float = Field(ge=0, le=1)
    rewritten_question: str = Field(min_length=1, max_length=4_000)
    reason: str = Field(min_length=1, max_length=500)


class IntentClassificationCompletion(BaseModel):
    """包含模型元数据的意图识别完成结果。"""

    result: IntentClassificationResult
    model: str
    usage: dict[str, int] | None = None
