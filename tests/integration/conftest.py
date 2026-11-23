"""PostgreSQL de prueba: un esquema temporal dentro de la base del gateway.

Cada ejecución crea `test_<aleatorio>`, aplica las migraciones ahí y lo borra al final.
Así no hace falta otra base ni permisos de superusuario, y no se tocan las tablas reales.
"""

import secrets
from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.orm import Session, sessionmaker

from app.config import get_settings
from app.db import Base
from app.ids import new_correlation_id, new_topup_id
from app.models import Country, Env, Product, Reward, Topup

RAIZ = Path(__file__).resolve().parents[2]


def alembic_config(connection: object) -> Config:
    cfg = Config(str(RAIZ / "alembic.ini"))
    cfg.attributes["connection"] = connection
    cfg.attributes["configure_logger"] = False
    return cfg


def engine_en_esquema(esquema: str) -> Engine:
    return create_engine(
        get_settings().database_url,
        connect_args={"options": f"-csearch_path={esquema}"},
        pool_size=20,
    )


@pytest.fixture(scope="session")
def schema_engine() -> Iterator[Engine]:
    esquema = f"test_{secrets.token_hex(6)}"
    admin = create_engine(get_settings().database_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{esquema}"'))
    engine = engine_en_esquema(esquema)
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
        yield engine
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{esquema}" CASCADE'))
        admin.dispose()


@pytest.fixture
def session_factory(schema_engine: Engine) -> Iterator[sessionmaker[Session]]:
    factory = sessionmaker(bind=schema_engine, expire_on_commit=False)
    yield factory
    tablas = ", ".join(t.name for t in Base.metadata.sorted_tables)
    with schema_engine.begin() as conn:
        conn.execute(text(f"TRUNCATE {tablas} RESTART IDENTITY CASCADE"))


@pytest.fixture
def session(session_factory: sessionmaker[Session]) -> Iterator[Session]:
    with session_factory() as s:
        yield s


@pytest.fixture
def reward(session: Session) -> Reward:
    """Producto de prueba de Jamaica (el sandbox no hace lookup de Costa Rica)."""
    session.add(Country(code="JM", name="Jamaica", dial_prefix="+1876", currency="JMD"))
    producto = Product(code="demo", name="Demo", country_code="JM")
    session.add(producto)
    session.flush()
    recompensa = Reward(
        product_id=producto.id,
        code="CURSO_FINALIZADO",
        label="Curso finalizado",
        amount=Decimal("500"),
        currency="JMD",
    )
    session.add(recompensa)
    session.commit()
    return recompensa


def nueva_topup(reward: Reward, external_ref: str = "user_1:curso_1", **extra: object) -> Topup:
    campos: dict[str, object] = {
        "id": new_topup_id(),
        "product_id": reward.product_id,
        "env": Env.TEST,
        "reward_id": reward.id,
        "external_ref": external_ref,
        "phone": "+18765550123",
        "amount": reward.amount,
        "currency": reward.currency,
        "correlation_id": new_correlation_id(),
    }
    campos.update(extra)
    return Topup(**campos)
