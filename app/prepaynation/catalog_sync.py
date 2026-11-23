"""Sincronización del catálogo RTR y de la tabla de errores, por entorno.

Reemplaza la caché completa en una sola transacción. Si alguna llamada falla o el catálogo
llega vacío, la caché anterior queda intacta.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import delete
from sqlalchemy.orm import Session

from app.models import Env, PnCatalogSku, PnErrorCode
from app.prepaynation.client import CATEGORY_RTR, PnError, PnResponse, PrepaynationClient
from app.prepaynation.models import Product, Sku


class CatalogSyncError(PnError):
    pass


@dataclass(frozen=True)
class SyncResult:
    env: Env
    products: int
    skus: int
    errors: int


def _exigir[T](nombre: str, r: PnResponse[T]) -> T:
    if not r.ok or r.payload is None:
        raise CatalogSyncError(
            f"{nombre}: HTTP {r.http_status}, responseCode {r.response_code!r} "
            f"({r.response_message or 'sin mensaje'}). La caché anterior no se modificó."
        )
    return r.payload


def _fila(env: Env, sku: Sku, producto: Product, ahora: datetime) -> PnCatalogSku:
    mn, mx = sku.min, sku.max
    operador = sku.operatorId if sku.operatorId is not None else producto.operatorId
    return PnCatalogSku(
        env=env,
        sku_id=str(sku.skuId),
        pn_product_id=str(producto.productId),
        product_name=sku.productName or producto.productName,
        sku_name=sku.skuName,
        operator_id=None if operador is None else str(operador),
        operator_name=sku.operatorName or producto.operatorName,
        country_code=(sku.countryCode or producto.countryCode or "").upper()[:2] or None,
        category_id=CATEGORY_RTR,
        benefit_type=sku.benefitType,
        validity=sku.validity,
        face_value_currency=(mn.faceValueCurrency if mn else None),
        min_amount=mn.faceValue if mn else None,
        max_amount=mx.faceValue if mx else None,
        delivery_currency=(mn.deliveryCurrencyCode if mn else None),
        min_delivered=mn.deliveredAmount if mn else None,
        max_delivered=mx.deliveredAmount if mx else None,
        min_wallet=mn.faceValueInWalletCurrency if mn else None,
        max_wallet=mx.faceValueInWalletCurrency if mx else None,
        exchange_rate=sku.exchangeRate,
        fee=sku.fee,
        allow_decimal=sku.allowDecimal,
        delivery_amount_type=sku.deliveryAmountType,
        raw=sku.model_dump(mode="json", exclude_none=True),
        refreshed_at=ahora,
    )


def sync_catalog(session: Session, client: PrepaynationClient) -> SyncResult:
    env = client.env
    productos = {p.productId: p for p in _exigir("getproducts", client.products(CATEGORY_RTR))}
    skus = _exigir("skus", client.skus())
    errores = _exigir("errors", client.errors())

    ahora = datetime.now(UTC)
    filas: dict[str, PnCatalogSku] = {}
    for sku in skus:
        producto = productos.get(sku.productId) if sku.productId is not None else None
        if producto is not None and str(sku.skuId) not in filas:
            filas[str(sku.skuId)] = _fila(env, sku, producto, ahora)
    if not filas:
        raise CatalogSyncError("El catálogo RTR llegó vacío. La caché anterior no se modificó.")

    codigos = {e.errorCode: e for e in errores}
    session.execute(delete(PnCatalogSku).where(PnCatalogSku.env == env))
    session.execute(delete(PnErrorCode).where(PnErrorCode.env == env))
    session.add_all(filas.values())
    session.add_all(
        PnErrorCode(env=env, code=c, message=e.errorDescription or "", refreshed_at=ahora)
        for c, e in codigos.items()
    )
    session.commit()
    return SyncResult(env=env, products=len(productos), skus=len(filas), errors=len(codigos))
