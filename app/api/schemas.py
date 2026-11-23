"""Modelos de entrada y salida de la API v1 (contrato del PRD)."""

from datetime import datetime
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer

from app.domain.phones import MAX_INPUT_LEN
from app.models import Env, TopupStatus

# Montos como número JSON (p. ej. 1000), como en el ejemplo del PRD.
Amount = Annotated[Decimal, PlainSerializer(float, return_type=float)]


class TopupCreate(BaseModel):
    # extra="forbid": el producto nunca envía montos ni otros campos.
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    reward_code: str = Field(min_length=1, max_length=50)
    phone: str = Field(min_length=1, max_length=MAX_INPUT_LEN)
    external_ref: str = Field(min_length=1, max_length=200)
    operator: str | None = Field(default=None, min_length=1, max_length=100)


class TopupOut(BaseModel):
    id: str
    external_ref: str
    status: TopupStatus
    env: Env
    reward_code: str
    amount: Amount
    currency: str
    phone: str
    error_code: str | None = None  # código de Prepaynation si terminó en FAILED
    created_at: datetime
    finalized_at: datetime | None = None


class RewardOut(BaseModel):
    code: str
    label: str
    amount: Amount
    currency: str
