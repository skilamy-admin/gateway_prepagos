"""Reglas que la base garantiza por sí misma, aunque el código tenga un error."""

import threading
from datetime import UTC, datetime
from decimal import Decimal

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, sessionmaker

from app.models import ApiKey, Env, Product, Reward, Topup, TopupStatus
from tests.integration.conftest import nueva_topup

AHORA = datetime.now(UTC)


def _guardar(session: Session, topup: Topup) -> Topup:
    session.add(topup)
    session.commit()
    return topup


def _cambiar(session: Session, topup: Topup, **campos: object) -> None:
    for k, v in campos.items():
        setattr(topup, k, v)
    session.commit()


def _falla(session: Session, topup: Topup, **campos: object) -> None:
    with pytest.raises(IntegrityError):
        _cambiar(session, topup, **campos)
    session.rollback()


# --- Idempotencia -----------------------------------------------------------------------------


def test_external_ref_unica_por_producto(session: Session, reward: Reward) -> None:
    _guardar(session, nueva_topup(reward, "ref-1"))
    with pytest.raises(IntegrityError):
        _guardar(session, nueva_topup(reward, "ref-1"))


def test_misma_external_ref_en_otro_producto_es_valida(session: Session, reward: Reward) -> None:
    _guardar(session, nueva_topup(reward, "ref-1"))
    otro = Product(code="otro", name="Otro", country_code="JM")
    session.add(otro)
    session.flush()
    r2 = Reward(product_id=otro.id, code="X", label="X", amount=Decimal(100), currency="JMD")
    session.add(r2)
    session.flush()
    _guardar(session, nueva_topup(r2, "ref-1"))


def test_correlation_id_unico(session: Session, reward: Reward) -> None:
    _guardar(session, nueva_topup(reward, "ref-1", correlation_id="C1"))
    with pytest.raises(IntegrityError):
        _guardar(session, nueva_topup(reward, "ref-2", correlation_id="C1"))


def test_10_solicitudes_simultaneas_crean_una_sola_fila(
    session_factory: sessionmaker[Session], reward: Reward
) -> None:
    """Criterio de aceptación del brief, a nivel de base de datos."""
    n = 10
    barrera = threading.Barrier(n)
    resultados: list[str] = []
    candado = threading.Lock()

    def insertar() -> None:
        with session_factory() as s:
            s.add(nueva_topup(reward, "user_123:curso_7"))
            barrera.wait()
            try:
                s.commit()
                r = "ok"
            except IntegrityError:
                s.rollback()
                r = "duplicada"
        with candado:
            resultados.append(r)

    hilos = [threading.Thread(target=insertar) for _ in range(n)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()

    assert sorted(resultados) == ["duplicada"] * (n - 1) + ["ok"]
    with session_factory() as s:
        assert s.scalar(select(func.count()).select_from(Topup)) == 1


# --- Máquina de estados (trigger topups_guard) ------------------------------------------------


def test_recorrido_valido_hasta_success(session: Session, reward: Reward) -> None:
    t = _guardar(session, nueva_topup(reward))
    _cambiar(session, t, sku_id="123", operator_id="9")
    _cambiar(session, t, status=TopupStatus.SUBMITTED, submitted_at=AHORA)
    _cambiar(session, t, status=TopupStatus.UNKNOWN, attempts=1)
    _cambiar(session, t, attempts=2, next_check_at=AHORA)  # sigue en UNKNOWN
    _cambiar(session, t, status=TopupStatus.SUCCESS, pn_response_code="000", finalized_at=AHORA)


def test_rechazo_antes_del_envio(session: Session, reward: Reward) -> None:
    t = _guardar(session, nueva_topup(reward))
    _cambiar(session, t, status=TopupStatus.FAILED, pn_response_code=None, finalized_at=AHORA)


def test_no_se_inserta_en_estado_distinto_de_pending(session: Session, reward: Reward) -> None:
    with pytest.raises(IntegrityError):
        _guardar(session, nueva_topup(reward, status=TopupStatus.SUBMITTED, submitted_at=AHORA))


@pytest.mark.parametrize("final", [TopupStatus.SUCCESS, TopupStatus.FAILED])
def test_estado_final_no_cambia(session: Session, reward: Reward, final: TopupStatus) -> None:
    t = _guardar(session, nueva_topup(reward))
    _cambiar(session, t, status=TopupStatus.SUBMITTED, submitted_at=AHORA)
    codigo = "000" if final == TopupStatus.SUCCESS else "510"
    _cambiar(session, t, status=final, pn_response_code=codigo, finalized_at=AHORA)
    _falla(session, t, status=TopupStatus.UNKNOWN, finalized_at=None)
    _falla(session, t, pn_response_message="otro mensaje")


@pytest.mark.parametrize(
    ("desde", "hacia"),
    [
        (TopupStatus.PENDING, TopupStatus.SUCCESS),
        (TopupStatus.PENDING, TopupStatus.UNKNOWN),
        (TopupStatus.SUBMITTED, TopupStatus.PENDING),
        (TopupStatus.UNKNOWN, TopupStatus.PENDING),
        (TopupStatus.UNKNOWN, TopupStatus.SUBMITTED),
    ],
)
def test_transiciones_invalidas(
    session: Session, reward: Reward, desde: TopupStatus, hacia: TopupStatus
) -> None:
    t = _guardar(session, nueva_topup(reward))
    if desde != TopupStatus.PENDING:
        _cambiar(session, t, status=TopupStatus.SUBMITTED, submitted_at=AHORA)
    if desde == TopupStatus.UNKNOWN:
        _cambiar(session, t, status=TopupStatus.UNKNOWN)
    extra: dict[str, object] = {"status": hacia}
    if hacia == TopupStatus.SUCCESS:
        extra |= {"pn_response_code": "000", "finalized_at": AHORA, "submitted_at": AHORA}
    if hacia == TopupStatus.UNKNOWN:
        extra |= {"submitted_at": AHORA}
    _falla(session, t, **extra)


@pytest.mark.parametrize(
    "campos",
    [
        {"amount": Decimal("999")},
        {"phone": "+18765550999"},
        {"correlation_id": "OTRO"},
        {"external_ref": "otra"},
    ],
)
def test_campos_inmutables(session: Session, reward: Reward, campos: dict[str, object]) -> None:
    t = _guardar(session, nueva_topup(reward))
    _falla(session, t, **campos)


def test_sku_no_cambia_despues_de_enviar(session: Session, reward: Reward) -> None:
    t = _guardar(session, nueva_topup(reward, sku_id="123"))
    _cambiar(session, t, status=TopupStatus.SUBMITTED, submitted_at=AHORA)
    _falla(session, t, sku_id="456")


# --- CHECKs -----------------------------------------------------------------------------------


def test_success_exige_codigo_000(session: Session, reward: Reward) -> None:
    t = _guardar(session, nueva_topup(reward))
    _cambiar(session, t, status=TopupStatus.SUBMITTED, submitted_at=AHORA)
    _falla(session, t, status=TopupStatus.SUCCESS, pn_response_code="852", finalized_at=AHORA)


def test_estado_final_exige_fecha_de_cierre(session: Session, reward: Reward) -> None:
    t = _guardar(session, nueva_topup(reward))
    _falla(session, t, status=TopupStatus.FAILED)


@pytest.mark.parametrize("telefono", ["18765550123", "+0123456", "+1876 555 0123", "+1"])
def test_telefono_debe_ser_e164(session: Session, reward: Reward, telefono: str) -> None:
    with pytest.raises(IntegrityError):
        _guardar(session, nueva_topup(reward, phone=telefono))


def test_monto_positivo(session: Session, reward: Reward) -> None:
    with pytest.raises(IntegrityError):
        _guardar(session, nueva_topup(reward, amount=Decimal(0)))


@pytest.mark.parametrize(
    ("env", "prefijo", "valido"),
    [
        (Env.TEST, "sk_test_abcd", True),
        (Env.LIVE, "sk_live_abcd", True),
        (Env.LIVE, "sk_test_abcd", False),
        (Env.TEST, "sk_live_abcd", False),
    ],
)
def test_prefijo_de_llave_coincide_con_entorno(
    session: Session, reward: Reward, env: Env, prefijo: str, valido: bool
) -> None:
    llave = ApiKey(product_id=reward.product_id, env=env, prefix=prefijo, key_hash="a" * 64)
    session.add(llave)
    if valido:
        session.commit()
    else:
        with pytest.raises(IntegrityError):
            session.commit()
