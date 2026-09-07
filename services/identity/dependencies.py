"""FastAPI 认证、同源与 CSRF 依赖。"""

from urllib.parse import urlsplit

from fastapi import Depends, Request
from sqlalchemy.ext.asyncio import AsyncSession

from services.identity.service import AuthContext, AuthService
from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE
from app.config import Settings, get_settings
from services.identity.db import get_admin_session
from app.errors import (
    AuthenticationRequiredError,
    CsrfValidationError,
    PasswordChangeRequiredError,
    PermissionDeniedError,
)


def get_auth_service(
    session: AsyncSession = Depends(get_admin_session),
    settings: Settings = Depends(get_settings),
) -> AuthService:
    return AuthService(session, settings)


def request_uses_https(request: Request, settings: Settings) -> bool:
    """支持直连 HTTPS 和受控反向代理传入的原始协议。"""
    if settings.auth_cookie_secure is not None:
        return settings.auth_cookie_secure
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded_proto.split(",", maxsplit=1)[0].strip() or request.url.scheme
    return scheme.casefold() == "https"


def _canonical_origin(value: str) -> str | None:
    parsed = urlsplit(value)
    if parsed.scheme.casefold() not in {"http", "https"} or not parsed.netloc:
        return None
    scheme = parsed.scheme.casefold()
    hostname = (parsed.hostname or "").casefold()
    try:
        port = parsed.port
    except ValueError:
        return None
    if port is None or (scheme == "http" and port == 80) or (
        scheme == "https" and port == 443
    ):
        return f"{scheme}://{hostname}"
    return f"{scheme}://{hostname}:{port}"


def _request_origin(request: Request) -> str:
    forwarded_proto = request.headers.get("x-forwarded-proto", "")
    scheme = forwarded_proto.split(",", maxsplit=1)[0].strip() or request.url.scheme
    forwarded_host = request.headers.get("x-forwarded-host", "")
    host = forwarded_host.split(",", maxsplit=1)[0].strip()
    if not host:
        host = request.headers.get("host", request.url.netloc)
    return f"{scheme}://{host}"


def require_same_origin(request: Request) -> None:
    """拒绝浏览器发起的跨站状态变更，同时保留非浏览器 API 调用。"""
    fetch_site = request.headers.get("sec-fetch-site", "").casefold()
    if fetch_site == "cross-site":
        raise CsrfValidationError()

    source = request.headers.get("origin") or request.headers.get("referer")
    if source is None:
        return
    source_origin = _canonical_origin(source)
    expected_origin = _canonical_origin(_request_origin(request))
    if source_origin is None or source_origin != expected_origin:
        raise CsrfValidationError()


async def get_current_auth_context(
    request: Request,
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    token = request.cookies.get(settings.auth_session_cookie_name)
    return await service.authenticate(token)


async def get_optional_auth_context(
    request: Request,
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> AuthContext | None:
    """页面路由使用的可选认证；无效会话按未登录处理并跳转登录页。"""
    token = request.cookies.get(settings.auth_session_cookie_name)
    if not token:
        return None
    try:
        return await service.authenticate(token)
    except AuthenticationRequiredError:
        return None


async def require_csrf(
    request: Request,
    context: AuthContext = Depends(get_current_auth_context),
    settings: Settings = Depends(get_settings),
) -> AuthContext:
    """对 Cookie 认证的状态变更执行同源和会话绑定 CSRF 校验。"""
    require_same_origin(request)
    cookie_token = request.cookies.get(settings.auth_csrf_cookie_name)
    header_token = request.headers.get("X-CSRF-Token")
    if not AuthService.csrf_matches(
        context,
        cookie_token=cookie_token,
        header_token=header_token,
    ):
        raise CsrfValidationError()
    return context


def _require_password_ready(context: AuthContext) -> AuthContext:
    if context.user.must_change_password:
        raise PasswordChangeRequiredError()
    return context


async def require_authenticated_user(
    context: AuthContext = Depends(get_current_auth_context),
) -> AuthContext:
    """要求有效会话，并阻止临时密码账号进入业务功能。"""
    return _require_password_ready(context)


async def require_authenticated_csrf(
    context: AuthContext = Depends(require_csrf),
) -> AuthContext:
    """为普通业务写请求组合登录、改密和 CSRF 校验。"""
    return _require_password_ready(context)


def _require_employee_role(context: AuthContext) -> AuthContext:
    context = _require_password_ready(context)
    if context.user.role != ROLE_EMPLOYEE:
        raise PermissionDeniedError()
    return context


async def require_employee(
    context: AuthContext = Depends(get_current_auth_context),
) -> AuthContext:
    """只允许普通员工读取问答、运行与个人会话资源。"""
    return _require_employee_role(context)


async def require_employee_csrf(
    context: AuthContext = Depends(require_csrf),
) -> AuthContext:
    """只允许普通员工提交通过 CSRF 校验的问答写请求。"""
    return _require_employee_role(context)


def _require_admin_role(context: AuthContext) -> AuthContext:
    context = _require_password_ready(context)
    if context.user.role != ROLE_ADMIN:
        raise PermissionDeniedError()
    return context


async def require_admin(
    context: AuthContext = Depends(get_current_auth_context),
) -> AuthContext:
    """只允许密码状态正常的管理员读取管理资源。"""
    return _require_admin_role(context)


async def require_admin_csrf(
    context: AuthContext = Depends(require_csrf),
) -> AuthContext:
    """只允许通过 CSRF 校验的管理员执行管理写操作。"""
    return _require_admin_role(context)
