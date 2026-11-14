from datetime import date, datetime
from decimal import Decimal
from typing import TYPE_CHECKING, List, Optional

from sqlalchemy import Boolean, Date, DateTime, ForeignKey, Integer, Numeric, String, Text, UniqueConstraint
from sqlalchemy.dialects.postgresql import ARRAY
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.core.database import Base

if TYPE_CHECKING:
    from app.users.models import User


class Category(Base):
    __tablename__ = "categories"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    name: Mapped[str] = mapped_column(String(100), unique=True, nullable=False)
    type: Mapped[str] = mapped_column(String(20), nullable=False)  # 'income' или 'expense'
    keywords: Mapped[List[str]] = mapped_column(ARRAY(String), default=list, server_default="{}")
    is_mandatory: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false", nullable=False)

    transactions: Mapped[List["Transaction"]] = relationship(back_populates="category")
    limits: Mapped[List["CategoryLimit"]] = relationship(back_populates="category", cascade="all, delete-orphan")


class CategoryLimit(Base):
    """Персональный месячный лимит пользователя на категорию расходов.

    Сами категории — общий справочник, а бюджет у каждого свой.
    """
    __tablename__ = "category_limits"
    __table_args__ = (UniqueConstraint("user_id", "category_id", name="uq_category_limits_user_category"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id: Mapped[int] = mapped_column(ForeignKey("categories.id", ondelete="CASCADE"), nullable=False, index=True)
    monthly_limit: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)

    category: Mapped["Category"] = relationship(back_populates="limits")


class Transaction(Base):
    __tablename__ = "transactions"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    category_id: Mapped[Optional[int]] = mapped_column(ForeignKey("categories.id", ondelete="SET NULL"), nullable=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    bank_name: Mapped[str] = mapped_column(String(50), nullable=False)
    is_parsed_automatically: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)

    user: Mapped["User"] = relationship(back_populates="transactions")
    category: Mapped[Optional["Category"]] = relationship(back_populates="transactions")


class Asset(Base):
    __tablename__ = "assets"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    ticker: Mapped[str] = mapped_column(String(30), nullable=False, index=True)
    # 'stock' | 'bond' | 'fund'
    asset_type: Mapped[str] = mapped_column(String(20), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 4), nullable=False)
    # None — цена покупки неизвестна (в брокерском отчёте ТБанка её нет); P&L по такой бумаге не считается
    average_buy_price: Mapped[Optional[Decimal]] = mapped_column(Numeric(14, 4), nullable=True)
    isin: Mapped[Optional[str]] = mapped_column(String(12), nullable=True)
    name: Mapped[Optional[str]] = mapped_column(String(150), nullable=True)

    user: Mapped["User"] = relationship(back_populates="assets")


class Reminder(Base):
    __tablename__ = "reminders"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[Optional[str]] = mapped_column(Text, nullable=True)
    trigger_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    is_notified: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False, index=True)
    # Повтор: None — разовое; 'daily' | 'weekly' | 'monthly' | 'yearly'. Повторяющееся напоминание
    # после отправки не закрывается, а переносится на следующую дату (is_notified остаётся False).
    recurrence: Mapped[Optional[str]] = mapped_column(String(16), nullable=True)
    # Интервал повтора: каждые N дней/недель/месяцев/лет (1 = каждый)
    recurrence_every: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    # «Якорное» число месяца для monthly/yearly (чтобы 31-е не «съезжало» после короткого месяца)
    recurrence_day: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Связь с вкладом: напоминание «Закрытие вклада» удаляется вместе с ним
    deposit_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("deposits.id", ondelete="CASCADE"), nullable=True, index=True
    )

    user: Mapped["User"] = relationship(back_populates="reminders")
    deposit: Mapped[Optional["Deposit"]] = relationship(back_populates="reminders")


class Deposit(Base):
    __tablename__ = "deposits"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    bank_name: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    amount: Mapped[Decimal] = mapped_column(Numeric(14, 2), nullable=False)
    interest_rate: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    end_date: Mapped[Optional[datetime]] = mapped_column(DateTime(timezone=True), nullable=True)
    # 'deposit' — срочный вклад (без пополнения/снятия), 'savings' — накопительный счёт
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="deposit", server_default="deposit")
    # Для срочного вклада: дата открытия и срок в месяцах (end_date считается из них)
    opened_at: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    term_months: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)
    # Ежемесячная капитализация процентов (только для срочного вклада)
    capitalization: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default="false")

    reminders: Mapped[List["Reminder"]] = relationship(
        back_populates="deposit", cascade="all, delete-orphan", passive_deletes=True
    )


class Vacation(Base):
    __tablename__ = "vacations"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    start_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)
    end_date: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, index=True)

    user: Mapped["User"] = relationship(back_populates="vacations")


class IncomeRule(Base):
    """Правило разбора доходов: «если в описании есть <keyword> — это <source>».

    Нужно, чтобы отличать переводы от родителей от обычных переводов: в выписке видно только имя отправителя.
    """
    __tablename__ = "income_rules"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    keyword: Mapped[str] = mapped_column(String(120), nullable=False, index=True)
    # 'parents' | 'transfers' | 'salary' | 'scholarship' | 'own' | 'interest' | 'other'
    source: Mapped[str] = mapped_column(String(20), nullable=False)
