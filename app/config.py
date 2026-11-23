"""Configuración leída de variables de entorno con prefijo GATEWAY_."""

from functools import lru_cache

from pydantic import SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict

# Archivo de secretos del servidor (root:gateway 0640). systemd lo carga con EnvironmentFile;
# la CLI lo lee directamente. Las variables de entorno tienen prioridad sobre el archivo.
ENV_FILE = "/etc/gateway_prepagos/env"


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="GATEWAY_", env_file=ENV_FILE, env_file_encoding="utf-8", extra="ignore"
    )

    # Por defecto: socket local de PostgreSQL con autenticación peer.
    database_url: str = "postgresql+psycopg:///gateway_prepagos"

    # Llave Fernet para cifrar credenciales de Prepaynation. Se exigirá cuando
    # exista el módulo de credenciales; nunca se registra ni se muestra.
    master_key: SecretStr | None = None

    api_host: str = "127.0.0.1"
    api_port: int = 8000
    log_level: str = "INFO"


@lru_cache
def get_settings() -> Settings:
    return Settings()
