"""Вход в веб-панель: пароль администратора (из .env) или одноразовая ссылка из Telegram-бота (/web)."""
import secrets

import bcrypt
from fastapi import APIRouter, Depends, Form, HTTPException, Query, Response, status
from fastapi.responses import RedirectResponse
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.config import settings
from app.core.database import get_async_session
from app.core.security import (
    ACCESS_TOKEN_EXPIRE_MINUTES,
    TOKEN_TYPE_LOGIN,
    create_access_token,
    decode_token_user_id,
)
from app.models import ROLE_ADMIN, User

router = APIRouter(prefix="/api", tags=["Auth"])

BCRYPT_PREFIXES = ("$2a$", "$2b$", "$2y$")
BCRYPT_MAX_BYTES = 72  # bcrypt учитывает только первые 72 байта; bcrypt>=5 на длинном вводе бросает ValueError


def _bcrypt_bytes(password: str) -> bytes:
    return password.encode("utf-8")[:BCRYPT_MAX_BYTES]


def verify_password(plain_password: str, expected_password: str) -> bool:
    """
    Проверяет пароль администратора. Поддерживает как bcrypt-хеш в .env,
    так и обычный текст (с защитой от timing-атак через compare_digest).
    """
    if expected_password.startswith(BCRYPT_PREFIXES):
        try:
            return bcrypt.checkpw(_bcrypt_bytes(plain_password), expected_password.encode("utf-8"))
        except ValueError:
            # Повреждённый хеш в .env — считаем пароль неверным, а не роняем запрос с 500
            return False
    return secrets.compare_digest(plain_password.encode("utf-8"), expected_password.encode("utf-8"))


def get_password_hash(password: str) -> str:
    """bcrypt-хеш для ADMIN_PASSWORD: python -c "from app.web.auth import get_password_hash as h; print(h('пароль'))" """
    return bcrypt.hashpw(_bcrypt_bytes(password), bcrypt.gensalt()).decode("utf-8")


def _set_session_cookie(response: Response, user_id: int) -> str:
    token = create_access_token(user_id)
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return token


async def _get_or_create_admin_user(session: AsyncSession) -> User:
    """Профиль администратора, под которым входит владелец по паролю из .env."""
    user = None
    if settings.ADMIN_TELEGRAM_ID:
        user = (
            await session.execute(select(User).where(User.telegram_id == settings.ADMIN_TELEGRAM_ID))
        ).scalar_one_or_none()
    if user is None:
        user = (
            await session.execute(select(User).where(User.role == ROLE_ADMIN).order_by(User.id.asc()))
        ).scalars().first()
    if user is None:
        user = User(
            telegram_id=settings.ADMIN_TELEGRAM_ID or 1,
            name=settings.ADMIN_NAME,
            role=ROLE_ADMIN,
        )
        session.add(user)
        await session.commit()
        await session.refresh(user)
    elif user.role != ROLE_ADMIN and user.telegram_id == settings.ADMIN_TELEGRAM_ID:
        # Владелец из .env всегда администратор
        user.role = ROLE_ADMIN
        await session.commit()
    return user


@router.post("/login")
async def login_for_access_token(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
    session: AsyncSession = Depends(get_async_session),
):
    """Вход администратора по логину и паролю из .env (HTTP-only cookie с JWT)."""
    # compare_digest на str падает с TypeError, если в строке не-ASCII (например, кириллица) — сравниваем байты
    is_valid_user = secrets.compare_digest(username.encode("utf-8"), settings.ADMIN_USERNAME.encode("utf-8"))
    is_valid_pass = verify_password(password, settings.ADMIN_PASSWORD)

    if not (is_valid_user and is_valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль.",
        )

    admin = await _get_or_create_admin_user(session)
    token = _set_session_cookie(response, admin.id)
    return {"status": "ok", "access_token": token, "token_type": "bearer", "role": admin.role}


@router.get("/login/telegram")
async def login_via_telegram_link(
    token: str = Query(..., description="Короткоживущий токен из команды /web в боте"),
    session: AsyncSession = Depends(get_async_session),
):
    """Вход по ссылке из бота: подходит любому пользователю из белого списка (в т.ч. роли user)."""
    user_id = decode_token_user_id(token, TOKEN_TYPE_LOGIN)
    user = await session.get(User, user_id)
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Пользователь не найден.")
    redirect = RedirectResponse(url="/", status_code=status.HTTP_303_SEE_OTHER)
    _set_session_cookie(redirect, user.id)
    return redirect


@router.post("/logout")
async def logout(response: Response):
    """Удаляет JWT cookie при выходе."""
    response.delete_cookie(key="access_token")
    return {"status": "ok", "message": "Выход выполнен"}
