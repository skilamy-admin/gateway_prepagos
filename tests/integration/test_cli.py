from collections.abc import Iterator

import pytest
from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker
from typer.testing import CliRunner

from app import db
from app.cli import app
from app.models import ApiKey, AuditLog, Env, Product, ProductStatus, Reward, Setting
from app.security import decrypt_secret, hash_api_key

runner = CliRunner()


@pytest.fixture(autouse=True)
def cli_en_esquema_de_prueba(
    monkeypatch: pytest.MonkeyPatch, session_factory: sessionmaker[Session]
) -> Iterator[None]:
    monkeypatch.setattr(db, "get_sessionmaker", lambda: session_factory)
    yield


def gw(*args: str, input: str | None = None, ok: bool = True) -> str:
    r = runner.invoke(app, list(args), input=input)
    assert (r.exit_code == 0) == ok, r.output
    return r.output


@pytest.fixture
def jamaica() -> None:
    gw("country", "add", "JM", "--name", "Jamaica", "--dial-prefix", "+1876", "--currency", "JMD")


@pytest.fixture
def producto(jamaica: None) -> str:
    gw("product", "add", "demo", "--name", "Demo Jamaica", "--country", "JM")
    return "demo"


def test_country_add_valida_prefijo() -> None:
    gw("country", "add", "CR", "--name", "Costa Rica", "--dial-prefix", "+1876",
       "--currency", "CRC", ok=False)  # fmt: skip
    gw("country", "add", "XX", "--name", "X", "--dial-prefix", "+1", "--currency", "XXX", ok=False)
    gw("country", "add", "CR", "--name", "Costa Rica", "--dial-prefix", "+506", "--currency", "CRC")
    assert "CR" in gw("country", "list")


def test_country_duplicado(jamaica: None) -> None:
    out = gw("country", "add", "JM", "--name", "J", "--dial-prefix", "+1876", "--currency", "JMD",
             ok=False)  # fmt: skip
    assert "ya existe" in out


def test_product_empieza_en_test(producto: str, session: Session) -> None:
    p = session.scalar(select(Product).where(Product.code == producto))
    assert p is not None and p.status == ProductStatus.TEST
    assert "demo" in gw("product", "list")


def test_product_pais_inexistente() -> None:
    gw("product", "add", "x", "--name", "X", "--country", "CR", ok=False)


def test_paso_a_live_pide_confirmacion(producto: str, session: Session) -> None:
    gw("product", "set-status", producto, "live", input="n\n", ok=False)
    assert session.scalar(select(Product.status)) == ProductStatus.TEST
    gw("product", "set-status", producto, "live", input="y\n")
    gw("product", "set-status", producto, "suspended")
    assert session.scalar(select(Product.status)) == ProductStatus.SUSPENDED


def test_key_create_muestra_la_llave_una_vez_y_guarda_solo_hash(
    producto: str, session: Session
) -> None:
    out = gw("key", "create", "--product", producto, "--env", "test")
    llave = next(p for p in out.split() if p.startswith("sk_test_"))
    k = session.scalars(select(ApiKey)).one()
    assert k.key_hash == hash_api_key(llave)
    assert k.prefix == llave[:12]
    assert llave not in gw("key", "list", "--product", producto)
    # La llave no queda en la auditoría.
    for fila in session.scalars(select(AuditLog)):
        assert llave not in str(fila.details)


def test_key_live_en_producto_test_avisa(producto: str) -> None:
    assert "product_suspended" in gw("key", "create", "--product", producto, "--env", "live")


def test_key_revoke(producto: str, session: Session) -> None:
    gw("key", "create", "--product", producto, "--env", "test")
    key_id = str(session.scalars(select(ApiKey.id)).one())
    gw("key", "revoke", key_id, "--yes")
    assert session.scalars(select(ApiKey)).one().revoked_at is not None
    assert "REVOCADA" in gw("key", "list", "--product", producto)


def test_reward_add_usa_moneda_del_pais(producto: str, session: Session) -> None:
    gw("reward", "add", "--product", producto, "--code", "CURSO_FINALIZADO",
       "--label", "Curso finalizado", "--amount", "1600")  # fmt: skip
    r = session.scalars(select(Reward)).one()
    assert (r.amount, r.currency, r.active) == (1600, "JMD", True)


@pytest.mark.parametrize(
    ("codigo", "monto"),
    [
        ("minusculas", "100"),
        ("OK", "0"),
        ("OK", "-5"),
        ("OK", "1.234"),
        ("OK", "abc"),
        ("OK", "NaN"),
    ],
)
def test_reward_add_invalida(producto: str, codigo: str, monto: str) -> None:
    gw("reward", "add", "--product", producto, "--code", codigo, "--label", "x",
       "--amount", monto, ok=False)  # fmt: skip


def test_reward_duplicada_y_disable(producto: str, session: Session) -> None:
    args = ["reward", "add", "--product", producto, "--code", "A", "--label", "a", "--amount", "5"]
    gw(*args)
    gw(*args, ok=False)
    gw("reward", "disable", "--product", producto, "--code", "A")
    assert session.scalars(select(Reward.active)).one() is False
    assert "A" not in gw("reward", "list", "--product", producto).split()
    assert "A" in gw("reward", "list", "--product", producto, "--all").split()


def test_credentials_set_cifra_el_password(session: Session) -> None:
    out = gw("credentials", "set", "--env", "sandbox", input="usuario1\nS3creta!\nS3creta!\n")
    assert "S3creta!" not in out
    fila = session.get_one(Setting, Env.TEST)
    assert fila.pn_user_id == "usuario1"
    assert fila.pn_password_enc is not None
    assert b"S3creta!" not in fila.pn_password_enc
    assert decrypt_secret(fila.pn_password_enc) == "S3creta!"
    show = gw("credentials", "show")
    assert "usuario1" in show and "cargado" in show and "S3creta!" not in show
    for a in session.scalars(select(AuditLog)):
        assert "S3creta!" not in str(a.details)


def test_credentials_production_pide_confirmacion(session: Session) -> None:
    gw("credentials", "set", "--env", "production", input="u\nn\n", ok=False)
    assert session.get(Setting, Env.LIVE) is None


def test_auditoria(producto: str, session: Session) -> None:
    acciones = [a.action for a in session.scalars(select(AuditLog).order_by(AuditLog.id))]
    assert acciones == ["country.add", "product.add"]
    assert all(a.actor.startswith("cli:") for a in session.scalars(select(AuditLog)))
