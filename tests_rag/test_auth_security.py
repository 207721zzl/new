"""认证输入规范化和密码哈希回归测试。"""

import pytest

from app.auth.security import (
    MAX_PASSWORD_LENGTH,
    generate_security_token,
    hash_password,
    hash_security_token,
    normalize_username,
    password_needs_rehash,
    validate_display_name,
    validate_password,
    validate_username,
    verify_password,
    security_token_matches,
)


def test_username_is_nfkc_normalized_and_case_insensitive() -> None:
    assert normalize_username("  Ａdmin.User  ") == "admin.user"
    assert validate_username("Admin.User") == "admin.user"
    assert validate_username("  大臭蛋  ") == "大臭蛋"


@pytest.mark.parametrize(
    "username",
    ["ab", "has space", "slash/name", "-leading-hyphen", "用户🙂"],
)
def test_invalid_username_is_rejected(username: str) -> None:
    with pytest.raises(ValueError):
        validate_username(username)


def test_display_name_accepts_chinese_but_rejects_control_characters() -> None:
    assert validate_display_name("  系统管理员  ") == "系统管理员"
    with pytest.raises(ValueError):
        validate_display_name("管理员\n伪造行")


def test_password_hash_is_argon2id_salted_and_verifiable() -> None:
    password = "correct horse battery staple"
    first = hash_password(password)
    second = hash_password(password)

    assert first.startswith("$argon2id$")
    assert second.startswith("$argon2id$")
    assert first != second
    assert verify_password(password, first) is True
    assert verify_password("incorrect password", first) is False
    assert verify_password(password, "!disabled-system-account") is False
    assert password_needs_rehash(first) is False
    assert password_needs_rehash("not-a-valid-hash") is True


def test_password_length_limits_are_enforced() -> None:
    with pytest.raises(ValueError):
        validate_password("too short")
    with pytest.raises(ValueError):
        validate_password("x" * (MAX_PASSWORD_LENGTH + 1))


def test_security_tokens_are_random_and_only_their_digest_needs_storage() -> None:
    first = generate_security_token()
    second = generate_security_token()
    digest = hash_security_token(first)

    assert first != second
    assert len(digest) == 64
    assert first not in digest
    assert security_token_matches(first, digest) is True
    assert security_token_matches(second, digest) is False
