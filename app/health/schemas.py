"""Pydantic-схемы домена health."""
from datetime import datetime
from decimal import Decimal
from typing import Optional

from pydantic import AliasChoices, BaseModel, Field, field_validator


class WeightCreateSchema(BaseModel):
    weight_kg: Decimal = Field(..., gt=20, le=400)
    bmi: Optional[Decimal] = Field(default=None, gt=5, le=100)
    timestamp: Optional[datetime] = None  # по умолчанию — сейчас


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
