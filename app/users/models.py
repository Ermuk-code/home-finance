from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, CheckConstraint, DateTime, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.finance.models import Asset, Reminder, Transaction, Vacation

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


class User(Base):
    """Участник семьи. Через users.id все данные (финансы, здоровье) привязаны к конкретному человеку."""

    __tablename__ = "users"
    __table_args__ = (CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="User", server_default="User")
    # 'admin' — видит чужую статистику; 'user' — только свои данные
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_USER, server_default=ROLE_USER)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    # Свободные рубли на брокерском счёте по последнему брокерскому отчёту (None — отчёт не загружали)
    broker_cash_rub: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 2), nullable=True)

    # FatSecret (OAuth 1.0a): постоянные токены доступа к дневнику питания; None — аккаунт не привязан
    fs_access_token: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)
    fs_access_secret: Mapped[Optional[str]] = mapped_column(String(255), nullable=True)

    # Связи указаны строками: сами классы лежат в app.finance.models (регистрируются через app.models)
    transactions: Mapped[List["Transaction"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    assets: Mapped[List["Asset"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    reminders: Mapped[List["Reminder"]] = relationship(back_populates="user", cascade="all, delete-orphan")
    vacations: Mapped[List["Vacation"]] = relationship(back_populates="user", cascade="all, delete-orphan")

    @property
    def fs_linked(self) -> bool:
        return bool(self.fs_access_token and self.fs_access_secret)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN
