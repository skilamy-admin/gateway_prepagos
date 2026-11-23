"""Controles de negocio al crear una recarga (RF7): topes de presupuesto y límite por teléfono.

- Topes diario y mensual en la moneda local del país del producto, sobre el monto de las
  recompensas. Se conoce exacto antes de ejecutar, a diferencia del costo en USD.
- Cuentan todas las recargas del mismo producto y entorno que no terminaron en FAILED
  (PENDING, SUBMITTED, UNKNOWN y SUCCESS): una UNKNOWN pudo haber cobrado.
- Día y mes según la zona horaria del país.
- Quien llama debe tener bloqueado el producto (SELECT … FOR UPDATE) para que dos
  solicitudes simultáneas no superen juntas el tope.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from sqlalchemy import ColumnElement, func, select
from sqlalchemy.orm import Session

from app.models import Env, Product, Topup, TopupStatus


class LimitExceededError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class Usage:
    day: Decimal
    month: Decimal


def period_starts(now: datetime, tz: str) -> tuple[datetime, datetime]:
    """Inicio del día y del mes actuales en la zona del país (como datetime con zona)."""
    local = now.astimezone(ZoneInfo(tz))
    dia = local.replace(hour=0, minute=0, second=0, microsecond=0)
    return dia, dia.replace(day=1)


def _vigentes(product_id: int, env: Env) -> list[ColumnElement[bool]]:
    return [
        Topup.product_id == product_id,
        Topup.env == env,
        Topup.status != TopupStatus.FAILED,
    ]


def usage(session: Session, product: Product, env: Env, tz: str, now: datetime) -> Usage:
    dia, mes = period_starts(now, tz)

    def suma_desde(inicio: datetime) -> Decimal:
        total = session.scalar(
            select(func.coalesce(func.sum(Topup.amount), 0)).where(
                *_vigentes(product.id, env), Topup.created_at >= inicio
            )
        )
        return Decimal(total or 0)

    return Usage(day=suma_desde(dia), month=suma_desde(mes))


def check_limits(
    session: Session,
    *,
    product: Product,
    env: Env,
    tz: str,
    phone: str,
    amount: Decimal,
    currency: str,
    now: datetime,
) -> None:
    """Lanza LimitExceededError si la nueva recarga supera algún límite del producto."""
    if product.max_per_phone is not None:
        dias = product.per_phone_window_days or 1
        recargas = session.scalar(
            select(func.count()).where(
                *_vigentes(product.id, env),
                Topup.phone == phone,
                Topup.created_at >= now - timedelta(days=dias),
            )
        )
        if (recargas or 0) >= product.max_per_phone:
            raise LimitExceededError(
                "phone_limit_reached",
                f"El número ya recibió {recargas} recarga(s) en los últimos {dias} día(s); "
                f"el máximo es {product.max_per_phone}",
            )

    if product.daily_cap is None and product.monthly_cap is None:
        return
    uso = usage(session, product, env, tz, now)
    for tope, usado, periodo in (
        (product.daily_cap, uso.day, "diario"),
        (product.monthly_cap, uso.month, "mensual"),
    ):
        if tope is not None and usado + amount > tope:
            raise LimitExceededError(
                "budget_exceeded",
                f"Se alcanzó el tope {periodo} del producto ({tope} {currency}; "
                f"usado {usado} {currency})",
            )
