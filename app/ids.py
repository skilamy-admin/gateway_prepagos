"""Identificadores ULID: 48 bits de tiempo en ms + 80 bits aleatorios, en Crockford base32.

Se implementa aquí (unas líneas) para no sumar una dependencia. Los ULID son ordenables por
tiempo y tienen 26 caracteres, dentro del límite de 1-50 del correlationId de Prepaynation.
"""

import secrets
import time

_ALFABETO = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
_PREFIJO_TOPUP = "tp_"


def new_ulid(ms: int | None = None) -> str:
    if ms is None:
        ms = time.time_ns() // 1_000_000
    if not 0 <= ms < 2**48:
        raise ValueError("marca de tiempo fuera del rango de ULID")
    valor = (ms << 80) | secrets.randbits(80)
    caracteres = []
    for _ in range(26):
        caracteres.append(_ALFABETO[valor & 0x1F])
        valor >>= 5
    return "".join(reversed(caracteres))


def new_topup_id() -> str:
    """ID público de una recarga, p. ej. ``tp_01JD…`` (formato del PRD)."""
    return _PREFIJO_TOPUP + new_ulid()


def new_correlation_id() -> str:
    """correlationId único que el gateway envía a Prepaynation."""
    return new_ulid()
