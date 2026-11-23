"""Registro de cada llamada a Prepaynation en topup_events."""

from collections.abc import Callable

from sqlalchemy.orm import Session, sessionmaker

from app.models import Env, TopupEvent
from app.prepaynation.client import CallRecord

PN_CALL = "pn_call"


def event_from_call(
    env: Env, record: CallRecord, *, topup_id: str | None = None, type_: str = PN_CALL
) -> TopupEvent:
    request = {"method": record.method, "path": record.path, **(record.request or {})}
    response = record.response
    if record.error is not None:
        response = {"error": record.error}
    return TopupEvent(
        topup_id=topup_id,
        env=env,
        type=type_,
        request=request,
        response=response if isinstance(response, dict) else {"value": response},
        http_status=record.http_status,
        latency_ms=record.latency_ms,
    )


def persist_calls(
    factory: sessionmaker[Session], env: Env, *, topup_id: str | None = None
) -> Callable[[CallRecord], None]:
    """Callback que guarda cada llamada en su propia transacción.

    Así queda registrada aunque la operación que la hizo falle y haga rollback.
    """

    def guardar(record: CallRecord) -> None:
        with factory() as s:
            s.add(event_from_call(env, record, topup_id=topup_id))
            s.commit()

    return guardar
