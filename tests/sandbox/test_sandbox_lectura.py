"""Pruebas de solo lectura contra el sandbox real (sin recargas).

    uv run pytest tests/sandbox -m sandbox

Usan las credenciales cifradas de la base y la llave maestra del servidor.
"""

import pytest
from sqlalchemy.orm import Session

from app import db
from app.config import get_settings
from app.models import Env
from app.prepaynation.client import client_for_env

pytestmark = pytest.mark.sandbox


@pytest.fixture(autouse=True)
def master_key_de_prueba() -> None:
    """Anula la llave de prueba de tests/conftest.py: aquí se usa la del servidor."""
    get_settings.cache_clear()


@pytest.fixture
def session() -> Session:
    return db.get_sessionmaker()()


def test_balance(session: Session) -> None:
    with client_for_env(session, Env.TEST) as c:
        r = c.balance()
    assert r.ok, (r.http_status, r.response_code, r.response_message)


def test_catalogo_rtr_de_jamaica(session: Session) -> None:
    with client_for_env(session, Env.TEST) as c:
        productos = c.products(country_code="JM")
        assert productos.ok and productos.payload
        skus = c.skus(product_id=productos.payload[0].productId)
    assert skus.ok and skus.payload
    assert all(s.min is not None and s.max is not None for s in skus.payload)
