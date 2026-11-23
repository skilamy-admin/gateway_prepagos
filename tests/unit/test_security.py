import re

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings
from app.models import Env
from app.security import (
    DecryptionError,
    MasterKeyError,
    decrypt_secret,
    encrypt_secret,
    env_of_key,
    generate_api_key,
    hash_api_key,
    hashes_match,
    key_prefix,
)


@pytest.mark.parametrize(("env", "inicio"), [(Env.TEST, "sk_test_"), (Env.LIVE, "sk_live_")])
def test_formato_de_llave(env: Env, inicio: str) -> None:
    llave = generate_api_key(env)
    assert re.fullmatch(inicio + r"[A-Za-z0-9]{32}", llave)
    assert env_of_key(llave) == env
    assert key_prefix(llave) == llave[:12]


def test_llaves_unicas() -> None:
    assert len({generate_api_key(Env.TEST) for _ in range(1000)}) == 1000


@pytest.mark.parametrize(
    "llave",
    [
        "",
        "sk_test_corta",
        "sk_prod_" + "a" * 32,
        "sk_test_" + "a" * 31 + "!",
        "sk_test_" + "a" * 33,
    ],
)
def test_formato_invalido(llave: str) -> None:
    assert env_of_key(llave) is None


def test_hash_sha256_hex() -> None:
    llave = generate_api_key(Env.TEST)
    h = hash_api_key(llave)
    assert re.fullmatch(r"[0-9a-f]{64}", h)
    assert llave not in h
    assert hashes_match(llave, h)
    assert not hashes_match(generate_api_key(Env.TEST), h)


def test_cifrado_ida_y_vuelta() -> None:
    token = encrypt_secret("p4ss-secreta")
    assert b"p4ss-secreta" not in token
    assert decrypt_secret(token) == "p4ss-secreta"


def test_otra_llave_maestra_no_descifra(monkeypatch: pytest.MonkeyPatch) -> None:
    token = encrypt_secret("p4ss-secreta")
    monkeypatch.setenv("GATEWAY_MASTER_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    with pytest.raises(DecryptionError) as e:
        decrypt_secret(token)
    assert "p4ss" not in str(e.value)


@pytest.mark.parametrize("valor", ["", "no-es-fernet"])
def test_llave_maestra_ausente_o_invalida(monkeypatch: pytest.MonkeyPatch, valor: str) -> None:
    monkeypatch.setenv("GATEWAY_MASTER_KEY", valor)
    get_settings.cache_clear()
    with pytest.raises(MasterKeyError) as e:
        encrypt_secret("x")
    assert valor == "" or valor not in str(e.value)
