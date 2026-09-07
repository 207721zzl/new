import httpx
import asyncio
from weakref import WeakKeyDictionary
from packages.platform.config import settings
from app.errors import AppError, AuthenticationRequiredError, CsrfValidationError

_clients = WeakKeyDictionary()


def pooled_client():
    loop = asyncio.get_running_loop()
    if loop not in _clients:
        _clients[loop] = httpx.AsyncClient(timeout=settings().service_timeout_seconds, trust_env=False,
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20))
    return _clients[loop]


async def close_clients():
    client = _clients.pop(asyncio.get_running_loop(), None)
    if client:
        await client.aclose()


class DependencyUnavailable(AppError):
    status_code = 503
    code = "dependency_unavailable"
    public_message = "依赖服务暂时不可用，请稍后重试。"


class ServiceClient:
    def __init__(self, domain):
        self.domain = domain

    async def post(self, path, payload, headers=None):
        config = settings()
        try:
            response = await pooled_client().post(
                getattr(config, f"{self.domain}_url") + path, json=payload,
                headers={**(headers or {}), "X-Service-Token": config.token()},
            )
        except httpx.HTTPError as exc:
            raise DependencyUnavailable() from exc
        if response.status_code == 401:
            raise AuthenticationRequiredError()
        if response.status_code == 403:
            raise CsrfValidationError()
        if not response.is_success:
            raise DependencyUnavailable()
        return response.json()
