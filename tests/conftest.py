from collections.abc import Iterator

import pytest
from cryptography.fernet import Fernet

from app.config import get_settings


@pytest.fixture(autouse=True)
def master_key_de_prueba(monkeypatch: pytest.MonkeyPatch) -> Iterator[str]:
    """Llave maestra propia de cada test: nunca se usa la del servidor."""
    llave = Fernet.generate_key().decode()
    monkeypatch.setenv("GATEWAY_MASTER_KEY", llave)
    get_settings.cache_clear()
    yield llave
    get_settings.cache_clear()
