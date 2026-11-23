"""Validación de teléfonos con phonenumbers: E.164, país del producto y móvil."""

import phonenumbers
from phonenumbers import NumberParseException, PhoneNumberType

from app.models import Country

_MOVIL = {PhoneNumberType.MOBILE, PhoneNumberType.FIXED_LINE_OR_MOBILE}
MAX_INPUT_LEN = 32


class InvalidPhoneError(ValueError):
    """El mensaje se puede mostrar al producto (no contiene secretos)."""


def normalize_mobile(raw: str, country: Country) -> str:
    """Devuelve el número en E.164 o lanza InvalidPhoneError.

    Acepta formato internacional (+506…) o nacional del país del producto.
    """
    if not raw or len(raw) > MAX_INPUT_LEN:
        raise InvalidPhoneError("Número vacío o demasiado largo")
    try:
        numero = phonenumbers.parse(raw, country.code)
    except NumberParseException:
        raise InvalidPhoneError("El número no tiene un formato válido") from None
    if not phonenumbers.is_valid_number_for_region(numero, country.code):
        raise InvalidPhoneError(f"No es un número válido de {country.name} ({country.dial_prefix})")
    e164 = phonenumbers.format_number(numero, phonenumbers.PhoneNumberFormat.E164)
    if not e164.startswith(country.dial_prefix):
        raise InvalidPhoneError(f"El número debe empezar por {country.dial_prefix}")
    if phonenumbers.number_type(numero) not in _MOVIL:
        raise InvalidPhoneError("El número no es un móvil")
    return e164
