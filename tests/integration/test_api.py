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
    with TestClient(app, raise_server_exceptions=False) as client:
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
    r = esc.post(key=esc.live_key)
    assert r.status_code == 202 and r.json()["env"] == "live"
    assert esc.post().json()["error"] == "external_ref_conflict"  # misma ref con llave test


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
