from dataclasses import dataclass
from types import SimpleNamespace
from fastapi import Depends, Request
from app.auth.constants import ROLE_ADMIN, ROLE_EMPLOYEE
from app.errors import AuthenticationRequiredError, PasswordChangeRequiredError, PermissionDeniedError
from packages.platform.client import ServiceClient


@dataclass
class AuthContext:
    user: SimpleNamespace


async def authenticate(request: Request, csrf=False):
    forwarded = {key: value for key, value in request.headers.items() if key.lower() in {
        "cookie", "x-csrf-token", "origin", "referer", "sec-fetch-site", "x-forwarded-proto",
        "x-forwarded-host", "host", "x-request-id",
    }}
    data = await ServiceClient("identity").post(
        "/internal/v1/authenticate", {"csrf": csrf}, headers=forwarded,
    )
    return AuthContext(SimpleNamespace(**data))


async def get_current_auth_context(request: Request):
    return await authenticate(request)


async def get_optional_auth_context(request: Request):
    if not request.headers.get("cookie"):
        return None
    try:
        return await authenticate(request)
    except AuthenticationRequiredError:
        return None


async def require_csrf(request: Request):
    return await authenticate(request, csrf=True)


def check_role(context, role):
    if context.user.must_change_password:
        raise PasswordChangeRequiredError()
    if context.user.role != role:
        raise PermissionDeniedError()
    return context


async def require_employee(context=Depends(get_current_auth_context)):
    return check_role(context, ROLE_EMPLOYEE)


async def require_employee_csrf(context=Depends(require_csrf)):
    return check_role(context, ROLE_EMPLOYEE)


async def require_admin(context=Depends(get_current_auth_context)):
    return check_role(context, ROLE_ADMIN)


async def require_admin_csrf(context=Depends(require_csrf)):
    return check_role(context, ROLE_ADMIN)
