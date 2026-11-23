"""Llaves de producto y cifrado de secretos.

- Llaves de API: `sk_test_…` / `sk_live_…` + 32 caracteres aleatorios. En base solo se guarda
  el SHA-256 (hex) y los primeros 12 caracteres como prefijo visible.
- Secretos (password de Prepaynation, secreto de webhook, TOTP): Fernet con la llave maestra
  GATEWAY_MASTER_KEY, que vive fuera de la base.
"""

import hashlib
import hmac
import secrets
import string

from cryptography.fernet import Fernet, InvalidToken

from app.config import get_settings
from app.models import Env

_ALFABETO_LLAVE = string.ascii_letters + string.digits
_LARGO_ALEATORIO = 32
PREFIX_LEN = 12
_PREFIJOS = {Env.TEST: "sk_test_", Env.LIVE: "sk_live_"}


class MasterKeyError(RuntimeError):
    """Falta la llave maestra o no es válida. El mensaje nunca incluye la llave."""


class DecryptionError(RuntimeError):
    """Un secreto no se pudo descifrar (llave maestra distinta o dato alterado)."""


# --- Llaves de producto -----------------------------------------------------------------------


def generate_api_key(env: Env) -> str:
    aleatorio = "".join(secrets.choice(_ALFABETO_LLAVE) for _ in range(_LARGO_ALEATORIO))
    return _PREFIJOS[env] + aleatorio


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode()).hexdigest()


def key_prefix(key: str) -> str:
    return key[:PREFIX_LEN]


def env_of_key(key: str) -> Env | None:
    """Entorno según el formato de la llave, o None si el formato no es válido."""
    for env, prefijo in _PREFIJOS.items():
        if (
            key.startswith(prefijo)
            and len(key) == len(prefijo) + _LARGO_ALEATORIO
            and all(c in _ALFABETO_LLAVE for c in key[len(prefijo) :])
        ):
            return env
    return None


def hashes_match(key: str, stored_hash: str) -> bool:
    """Comparación en tiempo constante."""
    return hmac.compare_digest(hash_api_key(key), stored_hash)


# --- Cifrado de secretos ----------------------------------------------------------------------


def _fernet() -> Fernet:
    llave = get_settings().master_key
    if llave is None or not llave.get_secret_value():
        raise MasterKeyError(
            "Falta GATEWAY_MASTER_KEY (en /etc/gateway_prepagos/env o en el entorno)"
        )
    try:
        return Fernet(llave.get_secret_value().encode())
    except ValueError:
        raise MasterKeyError("GATEWAY_MASTER_KEY no es una llave Fernet válida") from None


def encrypt_secret(valor: str) -> bytes:
    return _fernet().encrypt(valor.encode())


def decrypt_secret(token: bytes) -> str:
    try:
        return _fernet().decrypt(token).decode()
    except InvalidToken:
        raise DecryptionError(
            "No se pudo descifrar el secreto: la llave maestra no coincide o el dato cambió"
        ) from None
