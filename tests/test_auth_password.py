"""Проверка пароля администратора: bcrypt-хеш и открытый текст, в т.ч. кириллица и длинные пароли."""
from app.web.auth import get_password_hash, verify_password


def test_plain_text_password():
    assert verify_password("secret", "secret")
    assert not verify_password("secret2", "secret")


def test_plain_text_non_ascii_does_not_crash():
    assert verify_password("пароль", "пароль")
    assert not verify_password("пароль", "secret")


def test_bcrypt_hash_roundtrip():
    hashed = get_password_hash("Сложный пароль 123")
    assert hashed.startswith("$2b$")
    assert verify_password("Сложный пароль 123", hashed)
    assert not verify_password("другой", hashed)


def test_bcrypt_long_password_does_not_crash():
    long_pw = "x" * 100
    hashed = get_password_hash(long_pw)
    assert verify_password(long_pw, hashed)


def test_broken_hash_is_rejected_not_500():
    assert not verify_password("x", "$2b$broken")
