from decimal import Decimal

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from app.domain.rewards import coverage, talktime_skus
from app.models import Env, PnCatalogSku, PnErrorCode, TopupEvent
from app.prepaynation.catalog_sync import CatalogSyncError, sync_catalog
from app.prepaynation.client import PrepaynationClient
from app.prepaynation.events import persist_calls
from tests.fakes.prepaynation import SKUS, catalog_handler, ok


def cliente(handler: object, factory: sessionmaker[Session] | None = None) -> PrepaynationClient:
    return PrepaynationClient(
        Env.TEST, "u", "p",
        on_call=persist_calls(factory, Env.TEST) if factory else None,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )  # fmt: skip


def contar(s: Session, modelo: type) -> int:
    return s.scalar(select(func.count()).select_from(modelo)) or 0


def test_sync_guarda_solo_rtr(session: Session) -> None:
    r = sync_catalog(session, cliente(catalog_handler()))
    assert (r.products, r.skus, r.errors) == (4, 5, 3)
    assert session.get(PnCatalogSku, (Env.TEST, "9999")) is None
    kolbi = session.get_one(PnCatalogSku, (Env.TEST, "3390"))
    assert (kolbi.operator_name, kolbi.country_code, kolbi.benefit_type) == (
        "Kolbi",
        "CR",
        "Talktime",
    )
    assert (kolbi.min_delivered, kolbi.max_delivered, kolbi.delivery_currency) == (
        Decimal("1512.0000"), Decimal("25200.0000"), "CRC",
    )  # fmt: skip
    assert (kolbi.min_amount, kolbi.face_value_currency) == (Decimal("3.0000"), "USD")
    assert kolbi.exchange_rate == Decimal("504.000000")
    assert session.get_one(PnErrorCode, (Env.TEST, "510")).message == "Out of stock"


def test_sync_reemplaza_la_cache(session: Session) -> None:
    sync_catalog(session, cliente(catalog_handler()))
    pocos = ok([s for s in SKUS if s["skuId"] == 193])
    sync_catalog(session, cliente(catalog_handler(skus=pocos)))
    assert [k.sku_id for k in session.scalars(select(PnCatalogSku))] == ["193"]


@pytest.mark.parametrize(
    "falla",
    [
        {"skus": {"responseCode": "999", "responseMessage": "boom"}},
        {"errors": {"responseCode": "500", "responseMessage": "boom"}},
        {"skus": ok([])},
        {"status": 401},
    ],
)
def test_sync_fallido_conserva_la_cache(session: Session, falla: dict[str, object]) -> None:
    sync_catalog(session, cliente(catalog_handler()))
    with pytest.raises(CatalogSyncError):
        sync_catalog(session, cliente(catalog_handler(**falla)))  # type: ignore[arg-type]
    session.rollback()
    assert contar(session, PnCatalogSku) == 5
    assert contar(session, PnErrorCode) == 3


def test_cada_llamada_queda_en_topup_events(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    sync_catalog(session, cliente(catalog_handler(), session_factory))
    eventos = list(session.scalars(select(TopupEvent).order_by(TopupEvent.id)))
    assert [e.request["path"] for e in eventos if e.request] == [
        "/catalog/getproducts", "/catalog/skus", "/catalog/errors",
    ]  # fmt: skip
    assert all(e.topup_id is None and e.env == Env.TEST and e.http_status == 200 for e in eventos)
    assert all(e.latency_ms is not None and e.latency_ms >= 0 for e in eventos)


def test_eventos_se_guardan_aunque_falle_el_sync(
    session: Session, session_factory: sessionmaker[Session]
) -> None:
    with pytest.raises(CatalogSyncError):
        sync_catalog(session, cliente(catalog_handler(status=401), session_factory))
    session.rollback()
    assert contar(session, TopupEvent) == 1


@pytest.mark.parametrize(
    ("monto", "cubiertos"),
    [
        (Decimal(1000), set()),  # bajo el mínimo de ambos
        (Decimal(2000), {"Kolbi"}),  # bajo el mínimo de Claro (3474,73)
        (Decimal(5000), {"Kolbi", "Claro"}),
        (Decimal(20000), {"Kolbi"}),  # sobre el máximo de Claro (17373,65)
    ],
)
def test_cobertura_por_operador_costa_rica(
    session: Session, monto: Decimal, cubiertos: set[str]
) -> None:
    sync_catalog(session, cliente(catalog_handler()))
    cob = coverage(talktime_skus(session, Env.TEST, "CR"), monto, "CRC")
    assert {c.operator_name for c in cob} == {"Kolbi", "Claro"}
    assert {c.operator_name for c in cob if c.covered} == cubiertos


def test_cobertura_sku_fijo_jamaica(session: Session) -> None:
    sync_catalog(session, cliente(catalog_handler()))
    skus = talktime_skus(session, Env.TEST, "JM")
    flow = next(c for c in coverage(skus, Decimal("799.60"), "JMD") if c.operator_name == "Flow")
    assert [k.sku_id for k in flow.covering] == ["4056"]
    flow = next(c for c in coverage(skus, Decimal("800"), "JMD") if c.operator_name == "Flow")
    assert not flow.covered
    assert not any(c.covered for c in coverage(skus, Decimal("800"), "USD"))
