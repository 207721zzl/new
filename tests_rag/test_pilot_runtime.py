from datetime import UTC, datetime, timedelta

from app.pilot.runtime import PilotRuntimeMetrics
from app.pilot.service import _workload_summary
from scripts.pilot_load_test import percentile


def test_runtime_metrics_collect_only_counts_and_latency() -> None:
    metrics = PilotRuntimeMetrics(sample_size=3)
    metrics.record(status_code=200, duration_ms=10)
    metrics.record(status_code=404, duration_ms=20)
    metrics.record(status_code=503, duration_ms=30)

    snapshot = metrics.snapshot()
    assert snapshot["requests_total"] == 3
    assert snapshot["responses_4xx"] == 1
    assert snapshot["responses_5xx"] == 1
    assert snapshot["average_latency_ms"] == 20
    assert snapshot["p95_latency_ms"] == 30
    assert "path" not in snapshot


def test_workload_summary_flags_stale_and_calculates_success_rate() -> None:
    now = datetime.now(UTC).replace(tzinfo=None)
    rows = [
        ("completed", now - timedelta(seconds=2), now, now),
        ("failed", now - timedelta(seconds=1), now, now),
        ("running", now - timedelta(hours=1), None, now - timedelta(hours=1)),
    ]

    result = _workload_summary(rows, stale_before=now - timedelta(minutes=30))

    assert result.total == 3
    assert result.completed == 1
    assert result.failed == 1
    assert result.stale == 1
    assert result.success_rate == 0.5
    assert result.p95_duration_ms == 2000


def test_load_test_percentile_uses_nearest_rank() -> None:
    assert percentile([10, 20, 30, 40], 0.95) == 40
    assert percentile([], 0.95) is None
