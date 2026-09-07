from fastapi import Depends, Request
from pydantic import BaseModel
from packages.platform.application import create_app
from services.identity.router import router
from services.identity.admin_router import router as admin_router
from services.identity.dependencies import get_auth_service, require_same_origin
from services.identity.operations import router as operations_router
from services.identity.service import AuthService
from app.auth.schemas import CurrentUser
from app.config import get_settings
from app.errors import CsrfValidationError

app = create_app("identity")
app.include_router(router)
app.include_router(admin_router)
app.include_router(operations_router)


class AuthenticationRequest(BaseModel):
    csrf: bool = False


@app.post("/internal/v1/authenticate")
async def authenticate(payload: AuthenticationRequest, request: Request,
                       service: AuthService = Depends(get_auth_service)):
    config = get_settings()
    context = await service.authenticate(request.cookies.get(config.auth_session_cookie_name))
    if payload.csrf:
        require_same_origin(request)
        if not service.csrf_matches(context,
                                    cookie_token=request.cookies.get(config.auth_csrf_cookie_name),
                                    header_token=request.headers.get("x-csrf-token")):
            raise CsrfValidationError()
    return CurrentUser.model_validate(context.user)
