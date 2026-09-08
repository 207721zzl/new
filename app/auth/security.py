"""用户名、密码校验与 Argon2id 密码哈希。"""

import hashlib
import secrets
import unicodedata

from argon2 import PasswordHasher, Type, exceptions as argon2_exceptions


MIN_PASSWORD_LENGTH = 12
MAX_PASSWORD_LENGTH = 128
TOKEN_ENTROPY_BYTES = 32
USERNAME_SEPARATORS = frozenset("._-")

# argon2-cffi 23.1 将 InvalidHash 重命名为 InvalidHashError，并保留了别名；
# 兼容仍在使用旧名称的现有运行环境。
_INVALID_HASH_ERROR = getattr(
    argon2_exceptions,
    "InvalidHashError",
    argon2_exceptions.InvalidHash,
)

# 参数不依赖库默认值，便于未来通过 check_needs_rehash 平滑提升成本。
_PASSWORD_HASHER = PasswordHasher(
    time_cost=2,
    memory_cost=19_456,
    parallelism=1,
    hash_len=32,
    salt_len=16,
    type=Type.ID,
)


def normalize_username(username: str) -> str:
    """将登录名规范化为稳定、大小写不敏感的数据库键。"""
    return unicodedata.normalize("NFKC", username).strip().casefold()


def validate_username(username: str) -> str:
    """校验并返回规范化登录名。"""
    normalized = normalize_username(username)
    characters = list(normalized)
    starts_with_name_character = bool(characters) and unicodedata.category(
        characters[0]
    )[0] in {"L", "N"}
    contains_only_supported_characters = all(
        unicodedata.category(character)[0] in {"L", "N"}
        or character in USERNAME_SEPARATORS
        for character in characters
    )
    if not (
        3 <= len(characters) <= 64
        and starts_with_name_character
        and contains_only_supported_characters
    ):
        raise ValueError(
            "用户名需为 3–64 个字符，首位使用中文、字母或数字，"
            "其余可使用点、下划线和短横线。"
        )
    return normalized


def validate_password(password: str) -> None:
    """使用长度优先的密码规则，并限制极端输入带来的资源消耗。"""
    length = len(password)
    if length < MIN_PASSWORD_LENGTH or length > MAX_PASSWORD_LENGTH:
        raise ValueError(
            f"password must be between {MIN_PASSWORD_LENGTH} and "
            f"{MAX_PASSWORD_LENGTH} characters"
        )


def validate_display_name(display_name: str) -> str:
    """清理显示名称并拒绝控制字符。"""
    normalized = unicodedata.normalize("NFKC", display_name).strip()
    if not normalized or len(normalized) > 128:
        raise ValueError("display name must be between 1 and 128 characters")
    if any(unicodedata.category(character).startswith("C") for character in normalized):
        raise ValueError("display name must not contain control characters")
    return normalized


def hash_password(password: str) -> str:
    """校验明文后生成带随机盐的 Argon2id 哈希。"""
    validate_password(password)
    return _PASSWORD_HASHER.hash(password)


def verify_password(password: str, password_hash: str) -> bool:
    """安全验证密码；畸形或非 Argon2 哈希统一视为不匹配。"""
    try:
        return bool(_PASSWORD_HASHER.verify(password_hash, password))
    except (argon2_exceptions.VerificationError, _INVALID_HASH_ERROR):
        return False


def password_needs_rehash(password_hash: str) -> bool:
    """判断旧哈希是否需要在下次成功登录时升级参数。"""
    try:
        return _PASSWORD_HASHER.check_needs_rehash(password_hash)
    except _INVALID_HASH_ERROR:
        return True


def generate_security_token() -> str:
    """生成至少 256 bit 熵、适合 Cookie 和请求头传输的随机令牌。"""
    return secrets.token_urlsafe(TOKEN_ENTROPY_BYTES)


def hash_security_token(token: str) -> str:
    """使用固定长度摘要保存会话或 CSRF 令牌，数据库不落原文。"""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def security_token_matches(token: str, expected_hash: str) -> bool:
    """以常量时间比较令牌摘要。"""
    return secrets.compare_digest(hash_security_token(token), expected_hash)
