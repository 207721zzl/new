"""本地模型运行状态的生命周期测试。"""

from app.rag.runtime import ModelRuntimeStatus


def test_model_runtime_status_tracks_loading_ready_and_failure():
    status = ModelRuntimeStatus()

    assert status.snapshot().state == "not_loaded"
    status.mark_loading()
    assert status.snapshot().state == "loading"
    status.mark_ready()
    assert status.snapshot().ready is True

    status.mark_failed(RuntimeError("private failure"))
    snapshot = status.snapshot()
    assert snapshot.state == "failed"
    assert snapshot.ready is False
    assert snapshot.last_error_type == "RuntimeError"
