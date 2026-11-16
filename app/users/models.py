from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import BigInteger, CheckConstraint, Date, DateTime, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core.database import Base

if TYPE_CHECKING:
    from app.health.models import Workout
    from app.finance.models import Asset, Reminder, Transaction, Vacation

ROLE_ADMIN = "admin"
ROLE_USER = "user"
ROLES = (ROLE_ADMIN, ROLE_USER)


class User(Base):
    """Участник семьи. Через users.id все данные (финансы, здоровье) привязаны к конкретному человеку."""

    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('admin', 'user')", name="ck_users_role"),
        CheckConstraint("gender IN ('male', 'female')", name="ck_users_gender"),
        CheckConstraint("height_cm BETWEEN 50 AND 260", name="ck_users_height"),
        CheckConstraint("health_goal IN ('maintain', 'lose')", name="ck_users_health_goal"),
        CheckConstraint("deficit_percent BETWEEN 0 AND 20", name="ck_users_deficit"),
        CheckConstraint("goal_weight_kg BETWEEN 30 AND 300", name="ck_users_goal_weight"),
        CheckConstraint("activity_factor BETWEEN 1.2 AND 2.4", name="ck_users_activity_factor"),
        CheckConstraint("target_body_fat > 0 AND target_body_fat < 100", name="ck_users_target_body_fat"),
        CheckConstraint("avg_daily_deficit BETWEEN 0 AND 2000", name="ck_users_avg_daily_deficit"),
    )

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    telegram_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True, nullable=False)
    name: Mapped[str] = mapped_column(String(100), nullable=False, default="User", server_default="User")
    # 'admin' — видит чужую статистику; 'user' — только свои данные
    role: Mapped[str] = mapped_column(String(16), nullable=False, default=ROLE_USER, server_default=ROLE_USER)
    gender: Mapped[Optional[str]] = mapped_column(String(6), nullable=True)
    height_cm: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    height = synonym("height_cm")
    target_body_fat: Mapped[Decimal] = mapped_column(Numeric(4, 1), nullable=False, default=Decimal('12'), server_default='12')
    avg_daily_deficit: Mapped[int] = mapped_column(Integer, nullable=False, default=500, server_default='500')
    birth_date: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    health_goal: Mapped[str] = mapped_column(String(8), nullable=False, default="maintain", server_default="maintain")
    deficit_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=15, server_default="15")
    goal_weight_kg: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1), nullable=True)
    activity_factor: Mapped[Decimal] = mapped_column(Numeric(3, 2), nullable=False, default=Decimal("1.2"), server_default="1.2")
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
    workouts: Mapped[List['Workout']] = relationship(back_populates='user', cascade='all, delete-orphan', passive_deletes=True)

    @property
    def fs_linked(self) -> bool:
        return bool(self.fs_access_token and self.fs_access_secret)

    @property
    def is_admin(self) -> bool:
        return self.role == ROLE_ADMIN
