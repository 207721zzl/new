"""注册、登录、注销、当前用户和密码修改 HTTP 接口。"""

from fastapi import APIRouter, Depends, Request, Response, status

from app.auth.dependencies import (
    get_auth_service,
    get_current_auth_context,
    request_uses_https,
    require_csrf,
    require_same_origin,
)
from app.auth.schemas import (
    AuthClientConfig,
    ChangePasswordRequest,
    CurrentUser,
    LoginRequest,
    LoginResult,
    RegisterRequest,
    RegistrationResult,
)
from app.auth.security import MIN_PASSWORD_LENGTH
from app.auth.service import AuthContext, AuthService
from app.config import Settings, get_settings


router = APIRouter(prefix="/api/v1/auth", tags=["authentication"])


def _cookie_max_age(settings: Settings) -> int:
    return settings.auth_session_absolute_hours * 60 * 60


def _set_auth_cookies(
    response: Response,
    request: Request,
    *,
    session_token: str,
    csrf_token: str,
    settings: Settings,
) -> None:
    secure = request_uses_https(request, settings)
    cookie_options = {
        "max_age": _cookie_max_age(settings),
        "secure": secure,
        "samesite": settings.auth_cookie_samesite,
        "path": "/",
    }
    response.set_cookie(
        settings.auth_session_cookie_name,
        session_token,
        httponly=True,
        **cookie_options,
    )
    response.set_cookie(
        settings.auth_csrf_cookie_name,
        csrf_token,
        httponly=False,
        **cookie_options,
    )
    response.headers["Cache-Control"] = "no-store"


def _clear_auth_cookies(
    response: Response,
    request: Request,
    settings: Settings,
) -> None:
    secure = request_uses_https(request, settings)
    for cookie_name in (
        settings.auth_session_cookie_name,
        settings.auth_csrf_cookie_name,
    ):
        response.delete_cookie(
            cookie_name,
            path="/",
            secure=secure,
            httponly=cookie_name == settings.auth_session_cookie_name,
            samesite=settings.auth_cookie_samesite,
        )
    response.headers["Cache-Control"] = "no-store"


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client is not None else None


@router.get("/config", response_model=AuthClientConfig)
async def get_auth_client_config(
    response: Response,
    settings: Settings = Depends(get_settings),
) -> AuthClientConfig:
    """向静态前端公开非敏感的认证交互配置。"""
    response.headers["Cache-Control"] = "no-store"
    return AuthClientConfig(
        registration_enabled=settings.auth_registration_enabled,
        csrf_cookie_name=settings.auth_csrf_cookie_name,
        password_min_length=MIN_PASSWORD_LENGTH,
    )


@router.post(
    "/register",
    response_model=RegistrationResult,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(require_same_origin)],
)
async def register(
    payload: RegisterRequest,
    service: AuthService = Depends(get_auth_service),
) -> RegistrationResult:
    user = await service.register(
        username=payload.username,
        display_name=payload.display_name,
        password=payload.password.get_secret_value(),
    )
    return RegistrationResult(
        user=CurrentUser.model_validate(user),
        message="注册申请已提交，请等待管理员启用账号。",
    )


@router.post(
    "/login",
    response_model=LoginResult,
    dependencies=[Depends(require_same_origin)],
)
async def login(
    payload: LoginRequest,
    request: Request,
    response: Response,
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> LoginResult:
    issued = await service.login(
        username=payload.username,
        password=payload.password.get_secret_value(),
        ip_address=_client_ip(request),
        user_agent=request.headers.get("user-agent"),
    )
    _set_auth_cookies(
        response,
        request,
        session_token=issued.session_token,
        csrf_token=issued.csrf_token,
        settings=settings,
    )
    return LoginResult(
        user=CurrentUser.model_validate(issued.user),
        expires_at=issued.auth_session.expires_at,
    )


@router.get("/me", response_model=CurrentUser)
async def get_current_user(
    response: Response,
    context: AuthContext = Depends(get_current_auth_context),
) -> CurrentUser:
    response.headers["Cache-Control"] = "no-store"
    return CurrentUser.model_validate(context.user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_csrf),
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> Response:
    await service.logout(context)
    _clear_auth_cookies(response, request, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response


@router.post("/change-password", status_code=status.HTTP_204_NO_CONTENT)
async def change_password(
    payload: ChangePasswordRequest,
    request: Request,
    response: Response,
    context: AuthContext = Depends(require_csrf),
    service: AuthService = Depends(get_auth_service),
    settings: Settings = Depends(get_settings),
) -> Response:
    await service.change_password(
        context,
        current_password=payload.current_password.get_secret_value(),
        new_password=payload.new_password.get_secret_value(),
    )
    _clear_auth_cookies(response, request, settings)
    response.status_code = status.HTTP_204_NO_CONTENT
    return response
