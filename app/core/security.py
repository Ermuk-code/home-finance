"""Аутентификация и ролевая модель (RBAC).

Зависимости FastAPI:

* ``get_current_user`` — пользователь по JWT из cookie/заголовка Authorization (веб-интерфейс);
* ``is_admin`` — то же, но пускает только ``role == 'admin'`` (иначе 403);
* ``get_scope`` — «область данных» запроса: обычный пользователь всегда видит только свои записи,
  админ — свои, чужие (``target_user_id``) или все сразу (``all_users=true``).

Для бота: ``get_telegram_user`` находит пользователя по ``telegram_id`` (None — доступ запрещён).

Изоляция данных делается на уровне SQL: ``DataScope.apply(stmt, Model)`` добавляет
``WHERE model.user_id = :id`` к любому запросу, поэтому чужие строки не попадают в выборку вообще.
"""
import secrets
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from fastapi import Cookie, Depends, HTTPException, Query, Request, status
from jose import JWTError, jwt
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_session
from app.models import ROLE_ADMIN, User

ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней
LOGIN_LINK_EXPIRE_MINUTES = 5  # одноразовая ссылка входа из бота

TOKEN_TYPE_ACCESS = "access"
TOKEN_TYPE_LOGIN = "login"


# ---------------------------------------------------------------------------
# JWT
# ---------------------------------------------------------------------------
def create_token(user_id: int, token_type: str, expires_delta: timedelta) -> str:
    payload = {
        "sub": str(user_id),  # python-jose требует строку
        "typ": token_type,
        "exp": datetime.now(timezone.utc) + expires_delta,
    }
    return jwt.encode(payload, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


def create_access_token(user_id: int) -> str:
    return create_token(user_id, TOKEN_TYPE_ACCESS, timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES))


def create_login_token(user_id: int) -> str:
    return create_token(user_id, TOKEN_TYPE_LOGIN, timedelta(minutes=LOGIN_LINK_EXPIRE_MINUTES))


def decode_token_user_id(token: str, expected_type: str) -> int:
    """Возвращает user_id из токена нужного типа. Любая проблема — HTTP 401."""
    try:
        payload = jwt.decode(token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM])
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Срок действия токена истек или токен поврежден.",
        ) from e

    token_type = payload.get("typ")
    sub = payload.get("sub")
    if not isinstance(token_type, str) or not secrets.compare_digest(token_type, expected_type):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный токен доступа.")
    try:
        return int(sub)
    except (TypeError, ValueError) as e:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Недействительный токен доступа.") from e


def _extract_token(request: Request, access_token: Optional[str]) -> Optional[str]:
    if access_token:
        return access_token
    auth_header = request.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        return auth_header.split(" ", 1)[1]
    return None


# ---------------------------------------------------------------------------
# Пользователь
# ---------------------------------------------------------------------------
async def get_current_user(
    request: Request,
    access_token: Optional[str] = Cookie(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> User:
    """Пользователь по токену из веб-интерфейса (HTTP-only cookie или Bearer-заголовок)."""
    token = _extract_token(request, access_token)
    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован. Выполните вход через /login.",
        )
    user_id = decode_token_user_id(token, TOKEN_TYPE_ACCESS)
    user = await session.get(User, user_id)
    if user is None:
        # Пользователя удалили из белого списка — токен больше недействителен
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь не найден.")
    return user


async def get_optional_user(
    request: Request,
    access_token: Optional[str] = Cookie(default=None),
    session: AsyncSession = Depends(get_async_session),
) -> Optional[User]:
    """Для HTML-страниц: None вместо 401, чтобы сделать редирект на /login."""
    try:
        return await get_current_user(request=request, access_token=access_token, session=session)
    except HTTPException:
        return None


async def is_admin(current_user: User = Depends(get_current_user)) -> User:
    """Dependency: пускает только администратора."""
    if current_user.role != ROLE_ADMIN:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Недостаточно прав: нужна роль admin.")
    return current_user


async def get_telegram_user(session: AsyncSession, telegram_id: int) -> Optional[User]:
    """Пользователь бота по telegram_id. None — не зарегистрирован, доступ нужно закрыть."""
    return (await session.execute(select(User).where(User.telegram_id == telegram_id))).scalar_one_or_none()


# ---------------------------------------------------------------------------
# Изоляция данных (Data Segregation)
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DataScope:
    """Чьи данные доступны в этом запросе.

    * ``owner_id`` — владелец новых записей и «текущий» профиль (сам пользователь или ``target_user_id``);
    * ``filter_id`` — по какому ``user_id`` фильтровать выборки; ``None`` — без фильтра (только админ, ``all_users``).
    """

    current_user: User
    owner_id: int
    filter_id: Optional[int]

    def apply(self, stmt: Any, model: Any) -> Any:
        """Добавляет ``WHERE model.user_id = ...`` (или ничего для админа в режиме «все»)."""
        if self.filter_id is None:
            return stmt
        return stmt.where(model.user_id == self.filter_id)


def build_scope(current_user: User, target_user_id: Optional[int] = None, all_users: bool = False) -> DataScope:
    """Чистая логика прав (без БД): кому что можно. Нарушение прав — HTTP 403."""
    is_admin_role = current_user.role == ROLE_ADMIN

    if all_users:
        if not is_admin_role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Данные всех пользователей доступны только администратору.")
        return DataScope(current_user=current_user, owner_id=target_user_id or current_user.id, filter_id=None)

    if target_user_id is not None and target_user_id != current_user.id:
        if not is_admin_role:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Нет доступа к данным другого пользователя.")
        return DataScope(current_user=current_user, owner_id=target_user_id, filter_id=target_user_id)

    return DataScope(current_user=current_user, owner_id=current_user.id, filter_id=current_user.id)


async def get_scope(
    target_user_id: Optional[int] = Query(
        default=None, description="Только для admin: id пользователя, чьи данные нужны (по умолчанию — свои)."
    ),
    all_users: bool = Query(default=False, description="Только для admin: данные всех пользователей без фильтра."),
    current_user: User = Depends(get_current_user),
    session: AsyncSession = Depends(get_async_session),
) -> DataScope:
    scope = build_scope(current_user, target_user_id, all_users)
    if target_user_id is not None and target_user_id != current_user.id:
        # Админ запросил чужой профиль — убеждаемся, что он существует
        if await session.get(User, target_user_id) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Пользователь не найден.")
    return scope


def scope_for_user(user: User) -> DataScope:
    """Область «только свои данные» — для бота и фоновых задач, где нет HTTP-запроса."""
    return DataScope(current_user=user, owner_id=user.id, filter_id=user.id)
