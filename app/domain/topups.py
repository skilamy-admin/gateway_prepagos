"""Creación idempotente de recargas.

La idempotencia la garantiza la base: restricción única (product_id, external_ref) +
INSERT … ON CONFLICT DO NOTHING. Dos solicitudes simultáneas producen una sola fila.
"""

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

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


def find_by_external_ref(session: Session, product_id: int, external_ref: str) -> Topup | None:
    return session.scalar(
        select(Topup).where(Topup.product_id == product_id, Topup.external_ref == external_ref)
    )


def _existente(topup: Topup, env: Env) -> CreateResult:
    if topup.env != env:
        # La misma external_ref ya se usó en el otro entorno. Devolver esa transacción haría
        # creer al producto que pagó (o que probó) cuando no es así.
        raise TopupRejectedError(
            "external_ref_conflict",
            409,
            f"external_ref ya fue usada con una llave de {topup.env.value}",
        )
    return CreateResult(topup, created=False)


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
    existente = find_by_external_ref(session, product.id, external_ref)
    if existente is not None:
        return _existente(existente, env)

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
        .on_conflict_do_nothing(index_elements=["product_id", "external_ref"])
        .returning(Topup.id)
    ).scalar_one_or_none()

    if nuevo_id is None:
        # Otra solicitud con la misma external_ref ganó la carrera: devolver esa.
        session.rollback()
        ganadora = find_by_external_ref(session, product.id, external_ref)
        if ganadora is None:  # pragma: no cover - imposible salvo borrado manual
            raise RuntimeError("conflicto de external_ref sin fila existente")
        return _existente(ganadora, env)

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
