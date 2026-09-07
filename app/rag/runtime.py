"""线程安全的本地模型生命周期状态。"""

from dataclasses import dataclass
from threading import Lock


@dataclass(frozen=True, slots=True)
class ModelRuntimeSnapshot:
    """某一时刻不可变的模型加载状态快照。"""

    state: str
    ready: bool
    last_error_type: str | None


class ModelRuntimeStatus:
    """记录模型未加载、加载中、就绪或失败状态。"""

    def __init__(self) -> None:
        """初始化为尚未加载状态。"""
        self._lock = Lock()
        self._state = "not_loaded"
        self._last_error_type: str | None = None

    def mark_loading(self) -> None:
        """标记模型正在加载并清除旧错误。"""
        with self._lock:
            self._state = "loading"
            self._last_error_type = None

    def mark_ready(self) -> None:
        """标记模型已可接受推理请求。"""
        with self._lock:
            self._state = "ready"
            self._last_error_type = None

    def mark_failed(self, exc: Exception) -> None:
        """记录加载失败及异常类型。"""
        with self._lock:
            self._state = "failed"
            self._last_error_type = type(exc).__name__

    def snapshot(self) -> ModelRuntimeSnapshot:
        """在线程锁保护下返回一致的状态快照。"""
        with self._lock:
            return ModelRuntimeSnapshot(
                state=self._state,
                ready=self._state == "ready",
                last_error_type=self._last_error_type,
            )

    def reset(self) -> None:
        """重置状态，主要供测试或显式重载流程使用。"""
        with self._lock:
            self._state = "not_loaded"
            self._last_error_type = None


model_runtime_status = ModelRuntimeStatus()
