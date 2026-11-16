"""Pydantic-схемы домена health."""
from datetime import date, datetime
from decimal import Decimal
from typing import Optional

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator


class WeightCreateSchema(BaseModel):
    weight_kg: Decimal = Field(..., gt=20, le=400)
    bmi: Optional[Decimal] = Field(default=None, gt=5, le=100)
    body_fat: Optional[Decimal] = Field(default=None, gt=0, lt=100, decimal_places=1,
                                      validation_alias=AliasChoices('body_fat', 'body_fat_percent'))
    timestamp: Optional[datetime] = None  # по умолчанию — сейчас


class ActivityWebhookPayload(BaseModel):
    """Накопительные итоги за сегодня, не приращение с прошлого запроса."""

    profile_alias: str = Field(..., min_length=1, max_length=100)
    steps: int = Field(..., strict=True, ge=0, le=2147483647)
    active_calories: int = Field(default=0, strict=True, ge=0, le=2147483647)

    @field_validator("profile_alias")
    @classmethod
    def _strip_alias(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("profile_alias не может быть пустым")
        return value


class WeightWebhookPayload(WeightCreateSchema):
    """Тело вебхука Home Assistant: {"profile_alias": "admin", "weight_kg": 82.4}.

    profile_alias — роль (admin / user), имя из users.name, числовой id или telegram_id.
    Для обратной совместимости принимается и старое имя поля target_user.
    """

    profile_alias: str = Field(
        ...,
        min_length=1,
        max_length=100,
        validation_alias=AliasChoices("profile_alias", "target_user"),
    )

    @field_validator("profile_alias")
    @classmethod
    def _strip_alias(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("profile_alias не может быть пустым")
        return value


class WorkoutExerciseCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    sets: int = Field(strict=True, ge=1, le=100)
    reps: str = Field(min_length=1, max_length=40)
    weight_kg: Optional[Decimal] = Field(default=None, ge=0, le=Decimal('999.99'), decimal_places=2)

    @field_validator('name', 'reps', mode='before')
    @classmethod
    def strip_text(cls, value):
        return value.strip() if isinstance(value, str) else value


class WorkoutCreate(BaseModel):
    date: date
    title: str = Field(min_length=1, max_length=120)
    exercises: list[WorkoutExerciseCreate] = Field(min_length=1, max_length=20)

    @field_validator('title', mode='before')
    @classmethod
    def strip_title(cls, value):
        return value.strip() if isinstance(value, str) else value


class WorkoutExerciseRead(WorkoutExerciseCreate):
    id: int
    model_config = ConfigDict(from_attributes=True)


class WorkoutRead(BaseModel):
    id: int
    user_id: int
    date: date
    title: str
    is_completed: bool
    exercises: list[WorkoutExerciseRead]
    model_config = ConfigDict(from_attributes=True)
