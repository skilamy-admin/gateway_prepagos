"""Tablas del PRD (sección «Modelo de datos inicial»).

Las reglas que protegen dinero viven en la base, no solo en el código:
- única (product_id, external_ref) y única correlation_id → idempotencia;
- CHECK sobre estados, montos y formatos;
- un trigger (en la migración) que impide transiciones de estado inválidas,
  cambiar un estado final o modificar el correlation_id.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from ipaddress import IPv4Network, IPv6Network
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    LargeBinary,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy import Enum as SAEnum
from sqlalchemy.dialects.postgresql import ARRAY, CIDR, JSONB
from sqlalchemy.orm import Mapped, mapped_column

from app.db import Base

type IPNetwork = IPv4Network | IPv6Network

# --- Enumeraciones ---------------------------------------------------------------------------


class Env(StrEnum):
    """Entorno de una llave/transacción. test = sandbox de Prepaynation, live = producción."""

    TEST = "test"
    LIVE = "live"


class ProductStatus(StrEnum):
    TEST = "test"  # solo sandbox
    LIVE = "live"  # sandbox + producción
    SUSPENDED = "suspended"  # kill switch: ninguna recarga nueva


class TopupStatus(StrEnum):
    PENDING = "PENDING"
    SUBMITTED = "SUBMITTED"
    SUCCESS = "SUCCESS"
    FAILED = "FAILED"
    UNKNOWN = "UNKNOWN"


FINAL_STATUSES = frozenset({TopupStatus.SUCCESS, TopupStatus.FAILED})


class OperatorSource(StrEnum):
    LOOKUP = "lookup"  # resuelto con /lookup/mobile
    USER = "user"  # enviado por el producto en el campo operator


def _enum(cls: type[StrEnum], name: str) -> SAEnum:
    """VARCHAR + CHECK (no ENUM nativo de PostgreSQL: más fácil de migrar)."""
    return SAEnum(
        cls,
        name=name,
        native_enum=False,
        create_constraint=True,
        length=20,
        values_callable=lambda e: [m.value for m in e],
        validate_strings=True,
    )


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now(), nullable=False)


Money = Numeric(14, 2)  # montos en moneda local (CRC, JMD…)
PnMoney = Numeric(18, 4)  # montos devueltos por Prepaynation (wallet en USD)

# --- Configuración de negocio ----------------------------------------------------------------


class Country(Base):
    __tablename__ = "countries"
    __table_args__ = (
        CheckConstraint("code ~ '^[A-Z]{2}$'", name="code_iso2"),
        CheckConstraint("dial_prefix ~ '^\\+[0-9]{1,6}$'", name="dial_prefix_format"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
    )

    # ISO 3166-1 alfa-2; también es la región que usa phonenumbers.
    code: Mapped[str] = mapped_column(String(2), primary_key=True)
    name: Mapped[str] = mapped_column(String(100))
    dial_prefix: Mapped[str] = mapped_column(String(8))  # +506, +1876
    currency: Mapped[str] = mapped_column(String(3))  # CRC, JMD
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")


class Product(Base):
    __tablename__ = "products"
    __table_args__ = (
        CheckConstraint("daily_cap IS NULL OR daily_cap >= 0", name="daily_cap_positive"),
        CheckConstraint("monthly_cap IS NULL OR monthly_cap >= 0", name="monthly_cap_positive"),
        CheckConstraint("max_per_phone IS NULL OR max_per_phone > 0", name="max_per_phone_pos"),
        CheckConstraint(
            "per_phone_window_days IS NULL OR per_phone_window_days > 0", name="window_days_pos"
        ),
        CheckConstraint("rate_limit_per_minute > 0", name="rate_limit_positive"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(50), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    country_code: Mapped[str] = mapped_column(ForeignKey("countries.code"))
    status: Mapped[ProductStatus] = mapped_column(
        _enum(ProductStatus, "product_status"), server_default=ProductStatus.TEST.value
    )
    webhook_url: Mapped[str | None] = mapped_column(String(500))
    # Secreto HMAC del webhook, cifrado con Fernet (no se guarda en claro).
    webhook_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)
    allowed_ips: Mapped[list[IPNetwork] | None] = mapped_column(ARRAY(CIDR))
    # Topes de presupuesto (fase 2). Moneda: ver docs/decisiones.md.
    daily_cap: Mapped[Decimal | None] = mapped_column(Money)
    monthly_cap: Mapped[Decimal | None] = mapped_column(Money)
    max_per_phone: Mapped[int | None] = mapped_column(Integer)
    per_phone_window_days: Mapped[int | None] = mapped_column(Integer)
    # Máximo de solicitudes por minuto a la API, sumando todas las llaves del producto.
    rate_limit_per_minute: Mapped[int] = mapped_column(Integer, server_default="60")
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class ApiRateWindow(Base):
    """Contador de solicitudes por producto y minuto (rate limiting sin Redis).

    Una fila por producto y ventana de un minuto; se incrementa con un UPSERT atómico, así
    funciona igual con varios procesos de la API.
    """

    __tablename__ = "api_rate_windows"

    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), primary_key=True)
    window_start: Mapped[datetime] = mapped_column(DateTime(timezone=True), primary_key=True)
    count: Mapped[int] = mapped_column(Integer)


class ApiKey(Base):
    __tablename__ = "api_keys"
    __table_args__ = (
        CheckConstraint("key_hash ~ '^[0-9a-f]{64}$'", name="key_hash_sha256"),
        CheckConstraint(
            "(env = 'test' AND left(prefix, 8) = 'sk_test_') "
            "OR (env = 'live' AND left(prefix, 8) = 'sk_live_')",
            name="prefix_matches_env",
        ),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"), index=True)
    env: Mapped[Env] = mapped_column(_enum(Env, "api_key_env"))
    prefix: Mapped[str] = mapped_column(String(12))  # primeros 12 caracteres, visibles
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)  # SHA-256 en hex
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class Reward(Base):
    __tablename__ = "rewards"
    __table_args__ = (
        UniqueConstraint("product_id", "code"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    code: Mapped[str] = mapped_column(String(50))  # p. ej. CURSO_FINALIZADO
    label: Mapped[str] = mapped_column(String(200))
    amount: Mapped[Decimal] = mapped_column(Money)
    currency: Mapped[str] = mapped_column(String(3))
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = _created_at()


# --- Recargas --------------------------------------------------------------------------------


class Topup(Base):
    __tablename__ = "topups"
    __table_args__ = (
        UniqueConstraint("product_id", "external_ref"),
        CheckConstraint("amount > 0", name="amount_positive"),
        CheckConstraint("currency ~ '^[A-Z]{3}$'", name="currency_iso"),
        CheckConstraint("phone ~ '^\\+[1-9][0-9]{6,14}$'", name="phone_e164"),
        CheckConstraint("char_length(correlation_id) BETWEEN 1 AND 50", name="correlation_len"),
        CheckConstraint("char_length(external_ref) BETWEEN 1 AND 200", name="external_ref_len"),
        CheckConstraint("attempts >= 0", name="attempts_positive"),
        # Un estado final siempre tiene fecha de cierre, y solo los finales la tienen.
        CheckConstraint(
            "(status IN ('SUCCESS', 'FAILED')) = (finalized_at IS NOT NULL)",
            name="finalized_iff_final",
        ),
        # Lo que pasó por SUBMITTED tiene fecha de envío. FAILED puede ser rechazo previo.
        CheckConstraint(
            "status NOT IN ('SUBMITTED', 'UNKNOWN', 'SUCCESS') OR submitted_at IS NOT NULL",
            name="submitted_at_required",
        ),
        # Éxito = responseCode 000 (regla 1 de Prepaynation).
        CheckConstraint(
            "status <> 'SUCCESS' OR pn_response_code = '000'", name="success_requires_000"
        ),
        Index("ix_topups_status_next_check_at", "status", "next_check_at"),
        Index("ix_topups_product_id_created_at", "product_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(29), primary_key=True)  # tp_ + ULID
    product_id: Mapped[int] = mapped_column(ForeignKey("products.id"))
    env: Mapped[Env] = mapped_column(_enum(Env, "topup_env"))
    reward_id: Mapped[int] = mapped_column(ForeignKey("rewards.id"))
    external_ref: Mapped[str] = mapped_column(String(200))
    phone: Mapped[str] = mapped_column(String(16), index=True)  # E.164
    operator_id: Mapped[str | None] = mapped_column(String(50))
    operator_source: Mapped[OperatorSource | None] = mapped_column(
        _enum(OperatorSource, "operator_source")
    )
    sku_id: Mapped[str | None] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Money)  # en moneda local; lo fija la recompensa
    currency: Mapped[str] = mapped_column(String(3))
    correlation_id: Mapped[str] = mapped_column(String(50), unique=True)
    status: Mapped[TopupStatus] = mapped_column(
        _enum(TopupStatus, "topup_status"), server_default=TopupStatus.PENDING.value
    )
    pn_response_code: Mapped[str | None] = mapped_column(String(10))
    pn_response_message: Mapped[str | None] = mapped_column(Text)
    pn_transaction_id: Mapped[str | None] = mapped_column(String(100))
    face_value: Mapped[Decimal | None] = mapped_column(PnMoney)  # lo que recibe el usuario
    invoice_amount: Mapped[Decimal | None] = mapped_column(PnMoney)  # lo que paga Skilamy
    fee: Mapped[Decimal | None] = mapped_column(PnMoney)
    discount: Mapped[Decimal | None] = mapped_column(PnMoney)
    created_at: Mapped[datetime] = _created_at()
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    attempts: Mapped[int] = mapped_column(Integer, server_default="0")
    # Próxima consulta a /transaction/status (reconciliación con espera creciente).
    next_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class TopupEvent(Base):
    """Línea de tiempo: cada transición y cada llamada a Prepaynation (sin credenciales).

    topup_id es NULL para llamadas que no son de una recarga (balance, catálogo).
    """

    __tablename__ = "topup_events"
    __table_args__ = (
        CheckConstraint("latency_ms IS NULL OR latency_ms >= 0", name="latency_positive"),
        Index("ix_topup_events_topup_id_at", "topup_id", "at"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    topup_id: Mapped[str | None] = mapped_column(ForeignKey("topups.id"))
    env: Mapped[Env] = mapped_column(_enum(Env, "event_env"))
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    type: Mapped[str] = mapped_column(String(30))  # validated, submitted, timeout, status_check…
    request: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    response: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    http_status: Mapped[int | None] = mapped_column(Integer)
    latency_ms: Mapped[int | None] = mapped_column(Integer)


# --- Caché del catálogo de Prepaynation ------------------------------------------------------


class PnCatalogSku(Base):
    """Un SKU del catálogo de Prepaynation, por entorno. Se reemplaza en cada sincronización.

    Los identificadores de Prepaynation (enteros en la API) se guardan como texto.
    """

    __tablename__ = "pn_catalog"
    __table_args__ = (Index("ix_pn_catalog_lookup", "env", "country_code", "operator_id"),)

    env: Mapped[Env] = mapped_column(_enum(Env, "catalog_env"), primary_key=True)
    sku_id: Mapped[str] = mapped_column(String(50), primary_key=True)
    pn_product_id: Mapped[str | None] = mapped_column(String(50))
    product_name: Mapped[str | None] = mapped_column(String(200))
    operator_id: Mapped[str | None] = mapped_column(String(50))
    operator_name: Mapped[str | None] = mapped_column(String(200))
    country_code: Mapped[str | None] = mapped_column(String(2))
    category_id: Mapped[int | None] = mapped_column(Integer)
    sku_name: Mapped[str | None] = mapped_column(String(200))
    benefit_type: Mapped[str | None] = mapped_column(String(30))  # Talktime, Data, Bundle
    validity: Mapped[str | None] = mapped_column(String(10))  # p. ej. 30D
    # Rango del SKU (en uno fijo, mínimo = máximo), en tres monedas, según /catalog/skus:
    # face value (lo que se envía en `amount`), entregado al usuario y en moneda del wallet.
    face_value_currency: Mapped[str | None] = mapped_column(String(3))
    min_amount: Mapped[Decimal | None] = mapped_column(PnMoney)
    max_amount: Mapped[Decimal | None] = mapped_column(PnMoney)
    delivery_currency: Mapped[str | None] = mapped_column(String(3))
    min_delivered: Mapped[Decimal | None] = mapped_column(PnMoney)
    max_delivered: Mapped[Decimal | None] = mapped_column(PnMoney)
    min_wallet: Mapped[Decimal | None] = mapped_column(PnMoney)
    max_wallet: Mapped[Decimal | None] = mapped_column(PnMoney)
    exchange_rate: Mapped[Decimal | None] = mapped_column(Numeric(18, 6))
    fee: Mapped[Decimal | None] = mapped_column(PnMoney)
    allow_decimal: Mapped[bool | None] = mapped_column(Boolean)
    delivery_amount_type: Mapped[str | None] = mapped_column(String(20))  # actual / estimated
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)  # respuesta original
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class PnErrorCode(Base):
    """Tabla de códigos de /catalog/errors, por entorno."""

    __tablename__ = "pn_error_codes"

    env: Mapped[Env] = mapped_column(_enum(Env, "error_code_env"), primary_key=True)
    code: Mapped[str] = mapped_column(String(10), primary_key=True)
    message: Mapped[str] = mapped_column(Text)
    refreshed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


# --- Administración --------------------------------------------------------------------------


class Setting(Base):
    """Configuración de Prepaynation por entorno (test = sandbox, live = producción)."""

    __tablename__ = "settings"
    __table_args__ = (
        CheckConstraint(
            "low_balance_threshold IS NULL OR low_balance_threshold >= 0",
            name="threshold_positive",
        ),
    )

    env: Mapped[Env] = mapped_column(_enum(Env, "setting_env"), primary_key=True)
    pn_user_id: Mapped[str | None] = mapped_column(String(100))
    pn_password_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet
    wallet_currency: Mapped[str | None] = mapped_column(String(3))
    low_balance_threshold: Mapped[Decimal | None] = mapped_column(PnMoney)  # valor absoluto
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )


class AdminUser(Base):
    """Usuarios del panel (fase 2). La tabla existe desde la fase 1, como pide el brief."""

    __tablename__ = "admin_users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(255))
    totp_secret_enc: Mapped[bytes | None] = mapped_column(LargeBinary)  # Fernet
    active: Mapped[bool] = mapped_column(Boolean, server_default="true")
    created_at: Mapped[datetime] = _created_at()
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditLog(Base):
    """Acciones de administración (CLI en fase 1, panel en fase 2)."""

    __tablename__ = "audit_log"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True
    )
    actor: Mapped[str] = mapped_column(String(100))  # p. ej. cli:ramon, admin:ana
    action: Mapped[str] = mapped_column(String(50))  # p. ej. key.create, product.set_status
    target_type: Mapped[str | None] = mapped_column(String(50))
    target_id: Mapped[str | None] = mapped_column(String(100))
    details: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
