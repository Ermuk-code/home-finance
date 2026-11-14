"""RBAC и изоляция данных: чистая логика прав (без БД)."""
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import select

from app.core.security import (
    TOKEN_TYPE_ACCESS,
    TOKEN_TYPE_LOGIN,
    build_scope,
    create_access_token,
    create_login_token,
    decode_token_user_id,
)
from app.models import Transaction

ADMIN = SimpleNamespace(id=1, role="admin")
GIRL = SimpleNamespace(id=2, role="user")


def _sql(stmt) -> str:
    return str(stmt.compile(compile_kwargs={"literal_binds": True}))


def test_user_sees_only_own_rows():
    scope = build_scope(GIRL)
    assert scope.owner_id == 2 and scope.filter_id == 2
    sql = _sql(scope.apply(select(Transaction), Transaction))
    assert "transactions.user_id = 2" in sql


def test_user_cannot_request_other_profile():
    with pytest.raises(HTTPException) as exc:
        build_scope(GIRL, target_user_id=1)
    assert exc.value.status_code == 403


def test_user_cannot_request_all_users():
    with pytest.raises(HTTPException) as exc:
        build_scope(GIRL, all_users=True)
    assert exc.value.status_code == 403


def test_user_may_name_themselves_as_target():
    assert build_scope(GIRL, target_user_id=2).filter_id == 2


def test_admin_defaults_to_own_data():
    scope = build_scope(ADMIN)
    assert scope.owner_id == 1 and scope.filter_id == 1


def test_admin_can_target_other_user():
    scope = build_scope(ADMIN, target_user_id=2)
    assert scope.owner_id == 2 and scope.filter_id == 2
    assert "transactions.user_id = 2" in _sql(scope.apply(select(Transaction), Transaction))


def test_admin_all_users_has_no_user_filter():
    scope = build_scope(ADMIN, all_users=True)
    assert scope.filter_id is None
    assert "WHERE" not in _sql(scope.apply(select(Transaction), Transaction))


def test_access_token_roundtrip():
    assert decode_token_user_id(create_access_token(7), TOKEN_TYPE_ACCESS) == 7


def test_login_link_token_is_not_a_session_token():
    token = create_login_token(7)
    assert decode_token_user_id(token, TOKEN_TYPE_LOGIN) == 7
    with pytest.raises(HTTPException) as exc:
        decode_token_user_id(token, TOKEN_TYPE_ACCESS)
    assert exc.value.status_code == 401


def test_garbage_token_is_rejected():
    with pytest.raises(HTTPException) as exc:
        decode_token_user_id("not-a-jwt", TOKEN_TYPE_ACCESS)
    assert exc.value.status_code == 401
