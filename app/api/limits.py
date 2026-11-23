"""Rate limiting por producto y lista de IPs permitidas."""

import ipaddress
import random
from datetime import UTC, datetime, timedelta

from sqlalchemy import delete
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.models import ApiRateWindow, Product

_LIMPIEZA_PROBABILIDAD = 0.01  # 1 de cada 100 solicitudes borra ventanas viejas
_RETENCION = timedelta(hours=1)


def ip_allowed(product: Product, client_ip: str | None) -> bool:
    """Sin lista configurada se acepta cualquier IP. Con lista, la IP debe estar en ella."""
    if not product.allowed_ips:
        return True
    if not client_ip:
        return False
    try:
        ip = ipaddress.ip_address(client_ip)
    except ValueError:
        return False
    return any(ip in red for red in product.allowed_ips)


def consume(session: Session, product: Product, now: datetime | None = None) -> tuple[bool, int]:
    """Cuenta una solicitud en la ventana del minuto actual.

    Devuelve (permitida, segundos hasta la próxima ventana). El incremento es un UPSERT
    atómico: dos solicitudes simultáneas nunca leen el mismo valor. No hace commit.
    """
    now = now or datetime.now(UTC)
    ventana = now.replace(second=0, microsecond=0)
    cuenta = session.execute(
        insert(ApiRateWindow)
        .values(product_id=product.id, window_start=ventana, count=1)
        .on_conflict_do_update(
            index_elements=["product_id", "window_start"],
            set_={"count": ApiRateWindow.count + 1},
        )
        .returning(ApiRateWindow.count)
    ).scalar_one()
    if random.random() < _LIMPIEZA_PROBABILIDAD:  # noqa: S311 - no es criptográfico
        session.execute(delete(ApiRateWindow).where(ApiRateWindow.window_start < now - _RETENCION))
    restante = 60 - now.second
    return cuenta <= product.rate_limit_per_minute, restante
