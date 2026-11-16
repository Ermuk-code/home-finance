from datetime import date, datetime
from decimal import Decimal
from typing import Optional, TYPE_CHECKING

from sqlalchemy import Boolean, CheckConstraint, Date, DateTime, ForeignKey, Index, Integer, Numeric, String, func
from sqlalchemy.orm import Mapped, mapped_column, relationship, synonym

from app.core.database import Base

if TYPE_CHECKING:
    from app.users.models import User


class WeightLog(Base):
    __tablename__ = "weight_logs"
    __table_args__ = (CheckConstraint("body_fat > 0 AND body_fat < 100", name="ck_weight_body_fat"),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
    weight_kg: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    weight = synonym('weight_kg')
    date = synonym('timestamp')
    body_fat: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 1), nullable=True)
    bmi: Mapped[Optional[Decimal]] = mapped_column(Numeric(4, 1), nullable=True)
    timestamp: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


# Совместимое имя для истории состава тела, без второй таблицы веса.
HealthMetric = WeightLog


class BodyMeasurement(Base):
    __tablename__ = "body_measurements"
    __table_args__ = (
        Index("uq_body_measurements_user_date", "user_id", "date", unique=True),
        CheckConstraint("height_cm BETWEEN 50 AND 260", name="ck_body_height"),
        CheckConstraint("waist_cm BETWEEN 30 AND 250", name="ck_body_waist"),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    date: Mapped[date] = mapped_column(Date, nullable=False)
    height_cm: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1), nullable=True)
    waist_cm: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 1), nullable=True)


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


class Workout(Base):
    __tablename__ = 'workouts'
    __table_args__ = (Index('ix_workouts_user_date', 'user_id', 'date'),)

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    user_id: Mapped[int] = mapped_column(ForeignKey('users.id', ondelete='CASCADE'), nullable=False)
    morning_notified_on: Mapped[Optional[date]] = mapped_column(Date, nullable=True)
    date: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    title: Mapped[str] = mapped_column(String(120), nullable=False)
    is_completed: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False, server_default='false')
    user: Mapped['User'] = relationship(back_populates='workouts', lazy='raise')
    exercises: Mapped[list['WorkoutExercise']] = relationship(
        back_populates='workout', cascade='all, delete-orphan', passive_deletes=True,
        order_by='WorkoutExercise.id', lazy='raise',
    )


class WorkoutExercise(Base):
    __tablename__ = 'workout_exercises'
    __table_args__ = (
        CheckConstraint('sets BETWEEN 1 AND 100', name='ck_workout_exercise_sets'),
        CheckConstraint('weight_kg BETWEEN 0 AND 999.99', name='ck_workout_exercise_weight'),
    )
    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    workout_id: Mapped[int] = mapped_column(ForeignKey('workouts.id', ondelete='CASCADE'), nullable=False, index=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    sets: Mapped[int] = mapped_column(Integer, nullable=False)
    reps: Mapped[str] = mapped_column(String(40), nullable=False)
    weight_kg: Mapped[Optional[Decimal]] = mapped_column(Numeric(5, 2), nullable=True)
    workout: Mapped['Workout'] = relationship(back_populates='exercises', lazy='raise')
