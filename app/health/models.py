from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from sqlalchemy import Date, DateTime, ForeignKey, Index, Integer, Numeric, func
from sqlalchemy.orm import Mapped, mapped_column

from app.core.database import Base


class WeightLog(Base):
    __tablename__ = "weight_logs"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    bmi: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 1), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class DailyNutrition(Base):
    __tablename__ = "daily_nutrition"
    __table_args__ = (Index("uq_daily_nutrition_user_date", "user_id", "date", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    calories: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    protein: Mapped[Decimal] = mapped_column(Numeric(6, 1), nullable=False, default=0, server_default="0")
    fat: Mapped[Decimal] = mapped_column(Numeric(6, 1), nullable=False, default=0, server_default="0")
    carbs: Mapped[Decimal] = mapped_column(Numeric(6, 1), nullable=False, default=0, server_default="0")


class ActivityLog(Base):
    __tablename__ = "activity_logs"
    __table_args__ = (Index("uq_activity_logs_user_date", "user_id", "date", unique=True),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    steps: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
    active_calories: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")
