"""Autenticación por llave de producto: Authorization: Bearer sk_test_… | sk_live_…"""

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Annotated

from fastapi import Depends, Header, Request
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.api.errors import ApiError
from app.api.limits import consume, ip_allowed
from app.db import get_session
from app.models import ApiKey, Env, Product, ProductStatus
from app.security import env_of_key, hash_api_key, hashes_match

_INVALIDA = ApiError(
    401,
    "invalid_key",
    "Llave inexistente o revocada",
    headers={"WWW-Authenticate": "Bearer"},
)


@dataclass(frozen=True)
class AuthContext:
    product: Product
    env: Env
    key_id: int


def authenticate(
    request: Request,
    session: Annotated[Session, Depends(get_session)],
    authorization: Annotated[str | None, Header()] = None,
) -> AuthContext:
    esquema, _, llave = (authorization or "").partition(" ")
    llave = llave.strip()
    if esquema.lower() != "bearer" or env_of_key(llave) is None:
        raise _INVALIDA

    hash_ = hash_api_key(llave)
    registro = session.scalar(select(ApiKey).where(ApiKey.key_hash == hash_))
    if registro is None or not hashes_match(llave, registro.key_hash):
        raise _INVALIDA
    if registro.revoked_at is not None:
        raise _INVALIDA

    producto = session.get_one(Product, registro.product_id)
    if producto.status == ProductStatus.SUSPENDED:
        raise ApiError(403, "product_suspended", "El producto está suspendido")
    if registro.env == Env.LIVE and producto.status != ProductStatus.LIVE:
        raise ApiError(
            403, "product_suspended", "El producto está en test: las llaves live no se aceptan"
        )

    # La IP real del cliente la entrega Nginx (uvicorn confía solo en 127.0.0.1).
    ip = request.client.host if request.client else None
    if not ip_allowed(producto, ip):
        raise ApiError(403, "ip_not_allowed", f"La IP {ip} no está autorizada para este producto")

    permitida, espera = consume(session, producto)
    registro.last_used_at = datetime.now(UTC)
    session.commit()
    if not permitida:
        raise ApiError(
            429,
            "rate_limited",
            f"Límite de {producto.rate_limit_per_minute} solicitudes por minuto alcanzado",
            headers={"Retry-After": str(espera)},
        )
    return AuthContext(product=producto, env=registro.env, key_id=registro.id)
