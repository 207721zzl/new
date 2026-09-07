import pytest
from starlette.requests import Request

from app.main import _pilot_rate_policy, settings
from app.pilot.rate_limit import PilotRateLimiter


def test_rate_limiter_returns_retry_after_without_storing_extra_request() -> None:
    limiter = PilotRateLimiter()

    assert limiter.check(
        scope="login",
        client_key="ip:test",
        limit=2,
        window_seconds=60,
        now=100,
    ) is None
    assert limiter.check(
        scope="login",
        client_key="ip:test",
        limit=2,
        window_seconds=60,
        now=101,
    ) is None
    assert limiter.check(
        scope="login",
        client_key="ip:test",
        limit=2,
        window_seconds=60,
        now=102,
    ) == 58
    assert limiter.check(
        scope="login",
        client_key="ip:test",
        limit=2,
        window_seconds=60,
        now=161,
    ) is None


@pytest.mark.parametrize(
    "path",
    [
        "/api/v1/runs",
        "/api/v1/answers",
        "/api/v1/knowledge/answers",
        "/api/v1/retrieval/search",
        "/api/v1/feedback",
    ],
)
def test_employee_query_routes_share_query_rate_limit(
    path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(settings, "pilot_rate_limit_enabled", True)
    request = Request(
        {
            "type": "http",
            "http_version": "1.1",
            "method": "POST",
            "scheme": "http",
            "path": path,
            "raw_path": path.encode(),
            "query_string": b"",
            "headers": [],
            "client": ("127.0.0.1", 1234),
            "server": ("127.0.0.1", 8000),
        }
    )

    policy = _pilot_rate_policy(request)

    assert policy is not None
    assert policy[0] == "query"
