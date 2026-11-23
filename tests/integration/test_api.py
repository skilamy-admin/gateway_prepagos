"""API v1 contra PostgreSQL real (esquema temporal). Criterios de aceptación del brief."""

import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.api.main import create_app
from app.db import get_session
from app.models import (
    ApiKey,
    Country,
    Env,
    PnCatalogSku,
    Product,
    ProductStatus,
    Reward,
    Topup,
    TopupEvent,
    TopupStatus,
)
from app.security import generate_api_key, hash_api_key, key_prefix

BODY = {"reward_code": "CURSO", "phone": "+18765550123", "external_ref": "user_123:curso_7"}


@dataclass
class Escenario:
    client: TestClient
    product: Product
    test_key: str
    live_key: str

    def post(self, body: dict[str, Any] | None = None, key: str | None = None) -> Any:
        return self.client.post(
            "/v1/topups", json=BODY if body is None else body, headers=_auth(key or self.test_key)
        )


def _auth(key: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {key}"}


def _llave(s: Session, product: Product, env: Env) -> str:
    llave = generate_api_key(env)
    s.add(
        ApiKey(
            product_id=product.id, env=env, prefix=key_prefix(llave), key_hash=hash_api_key(llave)
        )
    )
    return llave


@pytest.fixture
def esc(session_factory: sessionmaker[Session]) -> Iterator[Escenario]:
    with session_factory() as s:
        s.add(Country(code="JM", name="Jamaica", dial_prefix="+1876", currency="JMD"))
        p = Product(code="demo", name="Demo", country_code="JM")
        s.add(p)
        s.flush()
        s.add(
            Reward(
                product_id=p.id, code="CURSO", label="Curso", amount=Decimal(1600), currency="JMD"
            )
        )
        s.add(
            Reward(
                product_id=p.id,
                code="VIEJA",
                label="Vieja",
                amount=Decimal(800),
                currency="JMD",
                active=False,
            )
        )
        test_key, live_key = _llave(s, p, Env.TEST), _llave(s, p, Env.LIVE)
        s.commit()

    app = create_app()

    def sesion() -> Iterator[Session]:
        with session_factory() as s:
            yield s

    app.dependency_overrides[get_session] = sesion
    with TestClient(app, raise_server_exceptions=False, client=("203.0.113.10", 50000)) as client:
        yield Escenario(client, p, test_key, live_key)


def _contar(factory: sessionmaker[Session]) -> int:
    with factory() as s:
        return s.scalar(select(func.count()).select_from(Topup)) or 0


# --- Autenticación ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "cabecera",
    [
        None,
        "",
        "Bearer",
        "Basic sk_test_x",
        "Bearer sk_test_corta",
        "Bearer " + "sk_test_" + "a" * 32,
    ],
)
def test_llave_invalida_401(esc: Escenario, cabecera: str | None) -> None:
    headers = {} if cabecera is None else {"Authorization": cabecera}
    r = esc.client.post("/v1/topups", json=BODY, headers=headers)
    assert r.status_code == 401
    assert r.json()["error"] == "invalid_key"
    assert r.headers["WWW-Authenticate"] == "Bearer"


def test_llave_revocada_401(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        for k in s.scalars(select(ApiKey)):
            k.revoked_at = datetime.now(UTC)
        s.commit()
    assert esc.post().status_code == 401
    assert esc.client.get("/v1/rewards", headers=_auth(esc.test_key)).status_code == 401


def test_producto_suspendido_403(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    with session_factory() as s:
        s.get_one(Product, esc.product.id).status = ProductStatus.SUSPENDED
        s.commit()
    r = esc.post()
    assert (r.status_code, r.json()["error"]) == (403, "product_suspended")
    assert _contar(session_factory) == 0


def test_llave_live_con_producto_en_test_403(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    r = esc.post(key=esc.live_key)
    assert (r.status_code, r.json()["error"]) == (403, "product_suspended")
    assert _contar(session_factory) == 0


def test_llave_live_con_producto_live(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        s.get_one(Product, esc.product.id).status = ProductStatus.LIVE
        s.commit()
    live = esc.post(key=esc.live_key)
    assert live.status_code == 202 and live.json()["env"] == "live"
    # La misma external_ref con la llave test es otra recarga, en sandbox.
    test = esc.post()
    assert test.status_code == 202 and test.json()["env"] == "test"
    assert test.json()["id"] != live.json()["id"]
    # Cada llave consulta la suya.
    for llave, id_ in ((esc.live_key, live.json()["id"]), (esc.test_key, test.json()["id"])):
        r = esc.client.get("/v1/topups", params={"external_ref": BODY["external_ref"]},
                           headers=_auth(llave))  # fmt: skip
        assert r.json()["id"] == id_
    assert esc.post(key=esc.live_key).status_code == 200


def test_last_used_at(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    esc.client.get("/v1/rewards", headers=_auth(esc.test_key))
    with session_factory() as s:
        usadas = {k.env: k.last_used_at for k in s.scalars(select(ApiKey))}
    assert usadas[Env.TEST] is not None and usadas[Env.LIVE] is None


# --- POST /v1/topups --------------------------------------------------------------------------


def test_crea_en_pending_202(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    r = esc.post()
    assert r.status_code == 202
    j = r.json()
    assert j["id"].startswith("tp_") and j["status"] == "PENDING" and j["env"] == "test"
    assert (j["reward_code"], j["amount"], j["currency"]) == ("CURSO", 1600, "JMD")
    with session_factory() as s:
        t = s.get_one(Topup, j["id"])
        assert t.status == TopupStatus.PENDING and t.phone == "+18765550123"
        assert 1 <= len(t.correlation_id) <= 50
        eventos = list(s.scalars(select(TopupEvent).where(TopupEvent.topup_id == t.id)))
        assert [e.type for e in eventos] == ["validated"]


def test_numero_en_formato_nacional_se_normaliza(esc: Escenario) -> None:
    r = esc.post({**BODY, "phone": "876 555 0123"})
    assert r.status_code == 202 and r.json()["phone"] == "+18765550123"


def test_repetir_5_veces_devuelve_la_misma(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    respuestas = [esc.post() for _ in range(5)]
    assert [r.status_code for r in respuestas] == [202, 200, 200, 200, 200]
    assert len({r.json()["id"] for r in respuestas}) == 1
    assert _contar(session_factory) == 1


def test_10_simultaneas_una_sola_fila(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    barrera = threading.Barrier(10)
    resultados: list[tuple[int, str]] = []
    candado = threading.Lock()

    def pedir() -> None:
        barrera.wait()
        r = esc.post()
        with candado:
            resultados.append((r.status_code, r.json().get("id", "")))

    hilos = [threading.Thread(target=pedir) for _ in range(10)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sorted(c for c, _ in resultados) == [200] * 9 + [202]
    assert len({i for _, i in resultados}) == 1
    assert _contar(session_factory) == 1


def test_reintento_tras_desactivar_recompensa(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    id_ = esc.post().json()["id"]
    with session_factory() as s:
        s.scalars(select(Reward).where(Reward.code == "CURSO")).one().active = False
        s.commit()
    r = esc.post()
    assert (r.status_code, r.json()["id"]) == (200, id_)


@pytest.mark.parametrize("codigo", ["NO_EXISTE", "VIEJA"])
def test_recompensa_inexistente_o_inactiva_422(esc: Escenario, codigo: str) -> None:
    r = esc.post({**BODY, "reward_code": codigo})
    assert (r.status_code, r.json()["error"]) == (422, "unknown_reward")


@pytest.mark.parametrize(
    "telefono",
    [
        "+50688887777",  # móvil de Costa Rica, producto de Jamaica
        "+16585550123",  # Jamaica (658), pero el producto usa +1876
        "+12125550123",  # Estados Unidos
        "123",
        "no-es-un-numero",
    ],
)
def test_telefono_invalido_422(
    esc: Escenario, session_factory: sessionmaker[Session], telefono: str
) -> None:
    r = esc.post({**BODY, "phone": telefono})
    assert (r.status_code, r.json()["error"]) == (422, "invalid_phone")
    assert _contar(session_factory) == 0


@pytest.mark.parametrize(
    "body",
    [
        {**BODY, "amount": 5000},  # el producto nunca envía montos
        {k: v for k, v in BODY.items() if k != "external_ref"},
        {**BODY, "external_ref": ""},
        {**BODY, "external_ref": "x" * 201},
        {**BODY, "phone": "1" * 40},
    ],
)
def test_solicitud_malformada_422(esc: Escenario, body: dict[str, Any]) -> None:
    r = esc.post(body)
    assert (r.status_code, r.json()["error"]) == (422, "invalid_request")


def test_operador_enviado_por_el_producto(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    with session_factory() as s:
        s.add(PnCatalogSku(env=Env.TEST, sku_id="4056", operator_id="2", operator_name="Flow",
                           country_code="JM", category_id=2, benefit_type="Talktime",
                           refreshed_at=datetime.now(UTC)))  # fmt: skip
        s.commit()
    r = esc.post({**BODY, "operator": "flow"})
    assert r.status_code == 202
    with session_factory() as s:
        t = s.get_one(Topup, r.json()["id"])
        assert (t.operator_id, t.operator_source) == ("2", "user")
    r = esc.post({**BODY, "external_ref": "otra", "operator": "Digicel"})
    assert (r.status_code, r.json()["error"]) == (422, "unknown_operator")
    assert "Flow" in r.json()["message"]


# --- Consultas --------------------------------------------------------------------------------


def test_get_por_id_y_por_external_ref(esc: Escenario) -> None:
    id_ = esc.post().json()["id"]
    a = esc.client.get(f"/v1/topups/{id_}", headers=_auth(esc.test_key))
    b = esc.client.get("/v1/topups", params={"external_ref": BODY["external_ref"]},
                       headers=_auth(esc.test_key))  # fmt: skip
    assert a.status_code == b.status_code == 200
    assert a.json() == b.json()
    assert a.json()["status"] == "PENDING" and a.json()["error_code"] is None


def test_get_inexistente_404(esc: Escenario) -> None:
    for ruta, params in [("/v1/topups/tp_nada", None), ("/v1/topups", {"external_ref": "nada"})]:
        r = esc.client.get(ruta, params=params, headers=_auth(esc.test_key))
        assert (r.status_code, r.json()["error"]) == (404, "not_found")


def test_no_se_ven_recargas_de_otro_producto(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    id_ = esc.post().json()["id"]
    with session_factory() as s:
        otro = Product(code="otro", name="Otro", country_code="JM")
        s.add(otro)
        s.flush()
        llave = _llave(s, otro, Env.TEST)
        s.commit()
    assert esc.client.get(f"/v1/topups/{id_}", headers=_auth(llave)).status_code == 404
    r = esc.client.get(
        "/v1/topups", params={"external_ref": BODY["external_ref"]}, headers=_auth(llave)
    )
    assert r.status_code == 404


def test_rewards_solo_activas(esc: Escenario) -> None:
    r = esc.client.get("/v1/rewards", headers=_auth(esc.test_key))
    assert r.status_code == 200
    assert r.json() == [{"code": "CURSO", "label": "Curso", "amount": 1600.0, "currency": "JMD"}]


def test_error_interno_no_filtra_detalles(esc: Escenario, monkeypatch: pytest.MonkeyPatch) -> None:
    import app.api.routes_topups as rutas

    def explota(*_: object, **__: object) -> None:
        raise RuntimeError("detalle interno con secreto")

    monkeypatch.setattr(rutas, "create_topup", explota)
    r = esc.post()
    assert r.status_code == 500
    assert r.json() == {"error": "internal_error", "message": "Error interno del gateway"}


# --- Lista de IPs y rate limiting -------------------------------------------------------------


def _producto(factory: sessionmaker[Session], product_id: int, **campos: object) -> None:
    with factory() as s:
        p = s.get_one(Product, product_id)
        for k, v in campos.items():
            setattr(p, k, v)
        s.commit()


@pytest.mark.parametrize(
    ("lista", "permitida"),
    [
        (None, True),
        (["203.0.113.10/32"], True),
        (["203.0.113.0/24"], True),
        (["198.51.100.0/24", "203.0.113.10/32"], True),
        (["198.51.100.7/32"], False),
        (["2001:db8::/32"], False),
    ],
)
def test_lista_de_ips(
    esc: Escenario, session_factory: sessionmaker[Session], lista: list[str] | None, permitida: bool
) -> None:
    import ipaddress

    redes = [ipaddress.ip_network(r) for r in lista] if lista else None
    _producto(session_factory, esc.product.id, allowed_ips=redes)
    r = esc.post()
    if permitida:
        assert r.status_code == 202
    else:
        assert (r.status_code, r.json()["error"]) == (403, "ip_not_allowed")
        assert "203.0.113.10" in r.json()["message"]
        assert _contar(session_factory) == 0


def test_rate_limit_por_producto(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    _producto(session_factory, esc.product.id, rate_limit_per_minute=3)
    codigos = [esc.post({**BODY, "external_ref": f"ref-{i}"}).status_code for i in range(3)]
    assert codigos == [202, 202, 202]
    r = esc.client.get("/v1/rewards", headers=_auth(esc.test_key))
    assert (r.status_code, r.json()["error"]) == (429, "rate_limited")
    assert 1 <= int(r.headers["Retry-After"]) <= 60
    assert esc.post({**BODY, "external_ref": "ref-9"}).status_code == 429
    assert _contar(session_factory) == 3


def test_rate_limit_concurrente_es_exacto(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    _producto(session_factory, esc.product.id, rate_limit_per_minute=5)
    barrera = threading.Barrier(12)
    codigos: list[int] = []
    candado = threading.Lock()

    def pedir() -> None:
        barrera.wait()
        r = esc.client.get("/v1/rewards", headers=_auth(esc.test_key))
        with candado:
            codigos.append(r.status_code)

    hilos = [threading.Thread(target=pedir) for _ in range(12)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    # Si el minuto cambia durante la prueba podrían pasar más; con 12 hilos no ocurre.
    assert codigos.count(200) == 5 and codigos.count(429) == 7


def test_rate_limit_se_reinicia_cada_minuto(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    from app.api.limits import consume

    with session_factory() as s:
        p = s.get_one(Product, esc.product.id)
        p.rate_limit_per_minute = 1
        t0 = datetime(2026, 11, 23, 20, 0, 30, tzinfo=UTC)
        assert consume(s, p, t0) == (True, 30)
        assert consume(s, p, t0.replace(second=59))[0] is False
        assert consume(s, p, t0.replace(minute=1, second=0)) == (True, 60)
        s.commit()


def test_llave_invalida_no_consume_limite(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    _producto(session_factory, esc.product.id, rate_limit_per_minute=1)
    for _ in range(5):
        esc.client.get("/v1/rewards", headers=_auth("sk_test_" + "b" * 32))
    assert esc.client.get("/v1/rewards", headers=_auth(esc.test_key)).status_code == 200


# --- Topes de presupuesto y límite por teléfono -----------------------------------------------


def _fallar(factory: sessionmaker[Session], topup_id: str) -> None:
    with factory() as s:
        t = s.get_one(Topup, topup_id)
        t.status = TopupStatus.FAILED
        t.finalized_at = datetime.now(UTC)
        s.commit()


def _ref(i: int, **extra: str) -> dict[str, str]:
    return {**BODY, "external_ref": f"ref-{i}", "phone": f"+1876555{i:04d}", **extra}


def test_tope_diario(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    _producto(session_factory, esc.product.id, daily_cap=Decimal(3200))  # 2 recompensas de 1600 JMD
    assert [esc.post(_ref(i)).status_code for i in range(2)] == [202, 202]
    r = esc.post(_ref(2))
    assert (r.status_code, r.json()["error"]) == (429, "budget_exceeded")
    assert "3200" in r.json()["message"]
    assert _contar(session_factory) == 2
    # Un reintento de una recarga existente nunca recibe budget_exceeded.
    assert esc.post(_ref(0)).status_code == 200


def test_tope_mensual(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    _producto(session_factory, esc.product.id, monthly_cap=Decimal(1600))
    assert esc.post(_ref(0)).status_code == 202
    assert esc.post(_ref(1)).json()["error"] == "budget_exceeded"


def test_las_fallidas_liberan_presupuesto(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    _producto(session_factory, esc.product.id, daily_cap=Decimal(1600))
    _fallar(session_factory, esc.post(_ref(0)).json()["id"])
    assert esc.post(_ref(1)).status_code == 202


def test_tope_separado_por_entorno(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    _producto(session_factory, esc.product.id, daily_cap=Decimal(1600), status=ProductStatus.LIVE)
    assert esc.post(_ref(0)).status_code == 202  # sandbox
    assert esc.post(_ref(1), key=esc.live_key).status_code == 202  # producción, su propio tope
    assert esc.post(_ref(2), key=esc.live_key).json()["error"] == "budget_exceeded"


def test_tope_con_solicitudes_simultaneas(
    esc: Escenario, session_factory: sessionmaker[Session]
) -> None:
    _producto(session_factory, esc.product.id, daily_cap=Decimal(4800))  # 3 recompensas de 1600
    barrera = threading.Barrier(10)
    codigos: list[int] = []
    candado = threading.Lock()

    def pedir(i: int) -> None:
        barrera.wait()
        r = esc.post(_ref(i))
        with candado:
            codigos.append(r.status_code)

    hilos = [threading.Thread(target=pedir, args=(i,)) for i in range(10)]
    for h in hilos:
        h.start()
    for h in hilos:
        h.join()
    assert sorted(codigos) == [202] * 3 + [429] * 7
    assert _contar(session_factory) == 3


def test_limite_por_telefono(esc: Escenario, session_factory: sessionmaker[Session]) -> None:
    _producto(session_factory, esc.product.id, max_per_phone=2, per_phone_window_days=30)
    tel = {"phone": "+18765550123"}
    assert esc.post(_ref(0, **tel)).status_code == 202
    assert esc.post(_ref(1, **tel)).status_code == 202
    r = esc.post(_ref(2, **tel))
    assert (r.status_code, r.json()["error"]) == (429, "phone_limit_reached")
    assert esc.post(_ref(3)).status_code == 202  # otro número
    # Formato nacional del mismo número: se normaliza y también cuenta.
    assert esc.post(_ref(4, phone="876-555-0123")).json()["error"] == "phone_limit_reached"
