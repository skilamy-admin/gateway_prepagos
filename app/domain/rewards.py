"""Recompensa → SKU: qué SKU de recarga de saldo de cada operador entrega un monto dado.

Los SKUs de saldo (RTR, beneficio Talktime) pueden ser de rango o fijos (mínimo = máximo).
El monto de la recompensa está en moneda local y se compara con el rango *entregado*
(`min_delivered`..`max_delivered` en `delivery_currency`).
"""

from collections import defaultdict
from dataclasses import dataclass
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Env, PnCatalogSku
from app.prepaynation.client import CATEGORY_RTR

TALKTIME = "talktime"


@dataclass(frozen=True)
class OperatorCoverage:
    operator_id: str
    operator_name: str
    skus: list[PnCatalogSku]  # todos los SKUs de saldo del operador
    covering: list[PnCatalogSku]  # los que entregan exactamente el monto pedido

    @property
    def covered(self) -> bool:
        return bool(self.covering)


def talktime_skus(session: Session, env: Env, country_code: str) -> list[PnCatalogSku]:
    return list(
        session.scalars(
            select(PnCatalogSku)
            .where(
                PnCatalogSku.env == env,
                PnCatalogSku.country_code == country_code,
                PnCatalogSku.category_id == CATEGORY_RTR,
                func.lower(PnCatalogSku.benefit_type) == TALKTIME,
            )
            .order_by(PnCatalogSku.operator_name, PnCatalogSku.sku_id)
        )
    )


def sku_covers(sku: PnCatalogSku, amount: Decimal, currency: str) -> bool:
    if sku.delivery_currency != currency:
        return False
    if sku.min_delivered is None or sku.max_delivered is None:
        return False
    return sku.min_delivered <= amount <= sku.max_delivered


def coverage(skus: list[PnCatalogSku], amount: Decimal, currency: str) -> list[OperatorCoverage]:
    por_operador: dict[str, list[PnCatalogSku]] = defaultdict(list)
    for sku in skus:
        por_operador[sku.operator_id or "?"].append(sku)
    return [
        OperatorCoverage(
            operator_id=op,
            operator_name=lista[0].operator_name or op,
            skus=lista,
            covering=[s for s in lista if sku_covers(s, amount, currency)],
        )
        for op, lista in sorted(por_operador.items(), key=lambda kv: kv[1][0].operator_name or "")
    ]
