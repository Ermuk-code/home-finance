import secrets
from datetime import datetime, timedelta, timezone
from typing import Optional

from fastapi import APIRouter, Cookie, Depends, Form, HTTPException, Request, Response, status
from jose import JWTError, jwt
from passlib.context import CryptContext

from config import settings

router = APIRouter(prefix="/api", tags=["Auth"])

pwd_context = CryptContext(schemes=["bcrypt"], deprecated="auto")
ACCESS_TOKEN_EXPIRE_MINUTES = 60 * 24 * 7  # 7 дней


def verify_password(plain_password: str, expected_password: str) -> bool:
    """
    Проверяет пароль администратора. Поддерживает как bcrypt-хеш в .env,
    так и обычный текст (с защитой от timing-атак через compare_digest).
    """
    if expected_password.startswith("$2b$") or expected_password.startswith("$2a$"):
        return pwd_context.verify(plain_password, expected_password)
    return secrets.compare_digest(plain_password, expected_password)


def get_password_hash(password: str) -> str:
    return pwd_context.hash(password)


def create_access_token(data: dict, expires_delta: Optional[timedelta] = None) -> str:
    to_encode = data.copy()
    expire = datetime.now(timezone.utc) + (
        expires_delta or timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    to_encode.update({"exp": expire})
    return jwt.encode(to_encode, settings.JWT_SECRET_KEY, algorithm=settings.JWT_ALGORITHM)


async def get_current_admin(
    request: Request,
    access_token: Optional[str] = Cookie(default=None),
) -> str:
    """
    Dependency для защиты эндпоинтов: извлекает JWT из HTTP-only cookie
    или заголовка Authorization и сверяет sub с ADMIN_USERNAME.
    """
    token = access_token

    if not token:
        auth_header = request.headers.get("Authorization")
        if auth_header and auth_header.startswith("Bearer "):
            token = auth_header.split(" ", 1)[1]

    if not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Не авторизован. Выполните вход через /api/login.",
        )

    try:
        payload = jwt.decode(
            token, settings.JWT_SECRET_KEY, algorithms=[settings.JWT_ALGORITHM]
        )
        username: Optional[str] = payload.get("sub")
        if username is None or not secrets.compare_digest(username, settings.ADMIN_USERNAME):
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Недействительный токен доступа.",
            )
        return username
    except JWTError as e:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Срок действия токена истек или токен поврежден.",
        ) from e


@router.post("/login")
async def login_for_access_token(
    response: Response,
    username: str = Form(...),
    password: str = Form(...),
):
    """Авторизация по логину и паролю с установкой защищенной HTTP-only cookie."""
    is_valid_user = secrets.compare_digest(username, settings.ADMIN_USERNAME)
    is_valid_pass = verify_password(password, settings.ADMIN_PASSWORD)

    if not (is_valid_user and is_valid_pass):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Неверный логин или пароль.",
        )

    token = create_access_token(data={"sub": settings.ADMIN_USERNAME})
    response.set_cookie(
        key="access_token",
        value=token,
        httponly=True,
        max_age=ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        samesite="lax",
    )
    return {"status": "ok", "access_token": token, "token_type": "bearer"}


@router.post("/logout")
async def logout(response: Response):
    """Удаляет JWT cookie при выходе."""
    response.delete_cookie(key="access_token")
    return {"status": "ok", "message": "Выход выполнен"}