"""认证接口的请求和响应模型。"""

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, Field, SecretStr, field_validator

from app.auth.security import (
    MAX_PASSWORD_LENGTH,
    validate_display_name,
    validate_password,
    validate_username,
)


class RegisterRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    display_name: str = Field(min_length=1, max_length=128)
    password: SecretStr

    @field_validator("username")
    @classmethod
    def validate_login_name(cls, value: str) -> str:
        return validate_username(value)

    @field_validator("display_name")
    @classmethod
    def validate_name(cls, value: str) -> str:
        return validate_display_name(value)

    @field_validator("password")
    @classmethod
    def validate_new_password(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: SecretStr

    @field_validator("password")
    @classmethod
    def limit_password_input(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) > MAX_PASSWORD_LENGTH:
            raise ValueError("password is too long")
        return value


class ChangePasswordRequest(BaseModel):
    current_password: SecretStr
    new_password: SecretStr

    @field_validator("current_password")
    @classmethod
    def limit_current_password(cls, value: SecretStr) -> SecretStr:
        if len(value.get_secret_value()) > MAX_PASSWORD_LENGTH:
            raise ValueError("password is too long")
        return value

    @field_validator("new_password")
    @classmethod
    def validate_new_password(cls, value: SecretStr) -> SecretStr:
        validate_password(value.get_secret_value())
        return value


UserRole = Literal["admin", "employee"]
UserStatus = Literal["pending", "active", "disabled"]


class CurrentUser(BaseModel):
    user_id: str
    username: str
    display_name: str
    role: UserRole
    status: UserStatus
    must_change_password: bool

    model_config = {"from_attributes": True}


class RegistrationResult(BaseModel):
    user: CurrentUser
    message: str


class LoginResult(BaseModel):
    user: CurrentUser
    expires_at: datetime


class AuthClientConfig(BaseModel):
    registration_enabled: bool
    csrf_cookie_name: str
    password_min_length: int
