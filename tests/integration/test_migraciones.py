import secrets

from alembic import command
from alembic.autogenerate import compare_metadata
from alembic.migration import MigrationContext
from sqlalchemy import Engine, create_engine, inspect, text

from app.config import get_settings
from app.db import Base
from tests.integration.conftest import alembic_config, engine_en_esquema


def test_migraciones_coinciden_con_los_modelos(schema_engine: Engine) -> None:
    with schema_engine.connect() as conn:
        diferencias = compare_metadata(MigrationContext.configure(conn), Base.metadata)
    assert diferencias == []


def test_estan_todas_las_tablas_del_prd(schema_engine: Engine) -> None:
    tablas = set(inspect(schema_engine).get_table_names())
    prd = {
        "countries", "products", "api_keys", "rewards", "topups", "topup_events",
        "pn_catalog", "settings", "admin_users", "audit_log",
    }  # fmt: skip
    assert prd <= tablas


def test_downgrade_y_upgrade_completos() -> None:
    esquema = f"test_{secrets.token_hex(6)}"
    admin = create_engine(get_settings().database_url)
    with admin.begin() as conn:
        conn.execute(text(f'CREATE SCHEMA "{esquema}"'))
    engine = engine_en_esquema(esquema)
    try:
        with engine.begin() as conn:
            command.upgrade(alembic_config(conn), "head")
            command.downgrade(alembic_config(conn), "base")
            assert inspect(conn).get_table_names() == ["alembic_version"]
            command.upgrade(alembic_config(conn), "head")
    finally:
        engine.dispose()
        with admin.begin() as conn:
            conn.execute(text(f'DROP SCHEMA "{esquema}" CASCADE'))
        admin.dispose()
