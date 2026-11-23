"""Creación idempotente de recargas.

La idempotencia la garantiza la base: restricción única (product_id, env, external_ref) +
INSERT … ON CONFLICT DO NOTHING. Dos solicitudes simultáneas producen una sola fila.
Los topes de presupuesto y el límite por teléfono se evalúan con el producto bloqueado.
"""

from dataclasses import dataclass
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

from app.domain.limits import LimitExceededError, check_limits
from app.domain.phones import InvalidPhoneError, normalize_mobile
from app.domain.rewards import talktime_skus
from app.ids import new_correlation_id, new_topup_id
from app.models import (
    Country,
    Env,
    OperatorSource,
    Product,
    Reward,
    Topup,
    TopupEvent,
)


class TopupRejectedError(Exception):
    """Rechazo de la solicitud. `code` y el mensaje se devuelven al producto."""

    def __init__(self, code: str, http_status: int, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.http_status = http_status
        self.message = message


@dataclass(frozen=True)
class CreateResult:
    topup: Topup
    created: bool


def find_by_external_ref(
    session: Session, product_id: int, env: Env, external_ref: str
) -> Topup | None:
    return session.scalar(
        select(Topup).where(
            Topup.product_id == product_id,
            Topup.env == env,
            Topup.external_ref == external_ref,
        )
    )


def resolve_operator(session: Session, env: Env, country_code: str, nombre: str) -> str:
    """Nombre de operador enviado por el producto → operatorId de Prepaynation (catálogo)."""
    operadores = {
        (k.operator_name or "").strip().lower(): k.operator_id
        for k in talktime_skus(session, env, country_code)
        if k.operator_id
    }
    encontrado = operadores.get(nombre.strip().lower())
    if encontrado is None:
        validos = ", ".join(sorted(n.title() for n in operadores)) or "catálogo no disponible"
        raise TopupRejectedError(
            "unknown_operator", 422, f"Operador desconocido «{nombre}». Válidos: {validos}"
        )
    return encontrado


def create_topup(
    session: Session,
    *,
    product: Product,
    env: Env,
    reward_code: str,
    phone: str,
    external_ref: str,
    operator: str | None = None,
) -> CreateResult:
    """Crea la recarga en PENDING (202) o devuelve la existente para esa external_ref (200).

    Hace commit. Lanza TopupRejectedError ante una solicitud inválida.
    """
    # Primero la idempotencia: un reintento devuelve lo mismo aunque la recompensa
    # se haya desactivado después.
    existente = find_by_external_ref(session, product.id, env, external_ref)
    if existente is not None:
        return CreateResult(existente, created=False)

    recompensa = session.scalar(
        select(Reward).where(
            Reward.product_id == product.id, Reward.code == reward_code, Reward.active
        )
    )
    if recompensa is None:
        raise TopupRejectedError(
            "unknown_reward", 422, f"La recompensa «{reward_code}» no existe o está inactiva"
        )

    pais = session.get_one(Country, product.country_code)
    try:
        e164 = normalize_mobile(phone, pais)
    except InvalidPhoneError as e:
        raise TopupRejectedError("invalid_phone", 422, str(e)) from None

    operator_id = None
    if operator is not None:
        operator_id = resolve_operator(session, env, pais.code, operator)

    # Bloquear el producto: serializa las creaciones del producto para que los topes no se
    # superen con solicitudes simultáneas. Se libera con el commit o el rollback.
    session.execute(select(Product.id).where(Product.id == product.id).with_for_update())
    # Mientras esperábamos el bloqueo pudo crearse la misma external_ref: devolver esa
    # antes de evaluar topes (un reintento nunca debe recibir budget_exceeded).
    existente = find_by_external_ref(session, product.id, env, external_ref)
    if existente is not None:
        session.commit()
        return CreateResult(existente, created=False)
    try:
        check_limits(
            session,
            product=product,
            env=env,
            tz=pais.timezone,
            phone=e164,
            amount=recompensa.amount,
            currency=recompensa.currency,
            now=datetime.now(UTC),
        )
    except LimitExceededError as e:
        session.rollback()
        raise TopupRejectedError(e.code, 429, e.message) from None

    nuevo_id = session.execute(
        insert(Topup)
        .values(
            id=new_topup_id(),
            product_id=product.id,
            env=env,
            reward_id=recompensa.id,
            external_ref=external_ref,
            phone=e164,
            operator_id=operator_id,
            operator_source=OperatorSource.USER if operator_id else None,
            amount=recompensa.amount,
            currency=recompensa.currency,
            correlation_id=new_correlation_id(),
        )
        .on_conflict_do_nothing(index_elements=["product_id", "env", "external_ref"])
        .returning(Topup.id)
    ).scalar_one_or_none()

    if nuevo_id is None:
        # Otra solicitud con la misma external_ref ganó la carrera: devolver esa.
        session.rollback()
        ganadora = find_by_external_ref(session, product.id, env, external_ref)
        if ganadora is None:  # pragma: no cover - imposible salvo borrado manual
            raise RuntimeError("conflicto de external_ref sin fila existente")
        return CreateResult(ganadora, created=False)

    session.add(
        TopupEvent(
            topup_id=nuevo_id,
            env=env,
            type="validated",
            request={
                "reward_code": reward_code,
                "phone": e164,
                "external_ref": external_ref,
                "operator": operator,
            },
            response={"status": "PENDING", "amount": str(recompensa.amount)},
        )
    )
    session.commit()
    return CreateResult(session.get_one(Topup, nuevo_id), created=True)


def reward_codes(session: Session, reward_ids: set[int]) -> dict[int, str]:
    if not reward_ids:
        return {}
    filas = session.execute(select(Reward.id, Reward.code).where(Reward.id.in_(reward_ids)))
    return {reward_id: code for reward_id, code in filas}
