"""CLI de administración: países, productos, llaves, recompensas, credenciales y catálogo.

Toda acción que cambia datos queda en audit_log (sin secretos).
"""

import getpass
import ipaddress
import os
import re
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import phonenumbers
import phonenumbers.timezone
import typer
import uvicorn
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app import __version__, db
from app.config import get_settings
from app.domain.limits import usage
from app.domain.rewards import coverage, talktime_skus
from app.models import (
    ApiKey,
    AuditLog,
    Country,
    Env,
    PnCatalogSku,
    Product,
    ProductStatus,
    Reward,
    Setting,
)
from app.prepaynation.catalog_sync import sync_catalog
from app.prepaynation.client import (
    MissingCredentialsError,
    PnError,
    PrepaynationClient,
    available_balance,
    client_for_env,
)
from app.prepaynation.events import persist_calls
from app.security import (
    DecryptionError,
    MasterKeyError,
    encrypt_secret,
    generate_api_key,
    hash_api_key,
    key_prefix,
)

app = typer.Typer(help="Administración de Gateway_prepagos", no_args_is_help=True)
country_app = typer.Typer(help="Países", no_args_is_help=True)
product_app = typer.Typer(help="Productos de Skilamy que piden recargas", no_args_is_help=True)
key_app = typer.Typer(help="Llaves de API de los productos", no_args_is_help=True)
reward_app = typer.Typer(help="Recompensas de cada producto", no_args_is_help=True)
credentials_app = typer.Typer(help="Credenciales de Prepaynation", no_args_is_help=True)
catalog_app = typer.Typer(
    help="Catálogo de SKUs de Prepaynation (caché local)", no_args_is_help=True
)
app.add_typer(country_app, name="country")
app.add_typer(product_app, name="product")
app.add_typer(key_app, name="key")
app.add_typer(reward_app, name="reward")
app.add_typer(credentials_app, name="credentials")
app.add_typer(catalog_app, name="catalog")


class PnEnv(StrEnum):
    """Nombre del entorno de Prepaynation en la CLI."""

    SANDBOX = "sandbox"
    PRODUCTION = "production"


PN_ENV_TO_ENV = {PnEnv.SANDBOX: Env.TEST, PnEnv.PRODUCTION: Env.LIVE}
ENV_TO_PN_ENV = {v: k for k, v in PN_ENV_TO_ENV.items()}

_CODIGO_RECOMPENSA = re.compile(r"^[A-Z0-9_]{1,50}$")
_CODIGO_PRODUCTO = re.compile(r"^[a-z0-9_-]{1,50}$")


# --- Utilidades -------------------------------------------------------------------------------


def _actor() -> str:
    return "cli:" + (os.environ.get("SUDO_USER") or getpass.getuser())


def _fail(mensaje: str) -> typer.Exit:
    typer.secho(mensaje, fg=typer.colors.RED, err=True)
    return typer.Exit(code=1)


@contextmanager
def _session() -> Iterator[Session]:
    with db.get_sessionmaker()() as s:
        yield s


def _audit(
    s: Session,
    action: str,
    target_type: str | None = None,
    target_id: object = None,
    details: dict[str, Any] | None = None,
) -> None:
    s.add(
        AuditLog(
            actor=_actor(),
            action=action,
            target_type=target_type,
            target_id=None if target_id is None else str(target_id),
            details=details,
        )
    )


def _commit(s: Session, duplicado: str) -> None:
    try:
        s.commit()
    except IntegrityError:
        s.rollback()
        raise _fail(duplicado) from None


def _product(s: Session, code: str) -> Product:
    producto = s.scalar(select(Product).where(Product.code == code))
    if producto is None:
        raise _fail(f"No existe el producto «{code}»")
    return producto


def _fecha(valor: datetime | None) -> str:
    return valor.astimezone(UTC).strftime("%Y-%m-%d %H:%M UTC") if valor else "—"


def _tabla(filas: list[list[str]], encabezado: list[str]) -> None:
    if not filas:
        typer.echo("(vacío)")
        return
    anchos = [max(len(str(x)) for x in col) for col in zip(encabezado, *filas, strict=False)]
    for fila in [encabezado, *filas]:
        typer.echo("  ".join(str(v).ljust(a) for v, a in zip(fila, anchos, strict=True)))


# --- General ----------------------------------------------------------------------------------


@app.command()
def version() -> None:
    """Muestra la versión instalada."""
    typer.echo(__version__)


@app.command()
def serve() -> None:
    """Arranca la API (servicio gateway-api)."""
    settings = get_settings()
    uvicorn.run(
        "app.api.main:app",
        host=settings.api_host,
        port=settings.api_port,
        log_level=settings.log_level.lower(),
        proxy_headers=True,
        forwarded_allow_ips="127.0.0.1",
    )


@app.command()
def worker() -> None:
    """Arranca el worker (servicio gateway-worker)."""
    from app.worker.main import run

    run()


# --- Países -----------------------------------------------------------------------------------


@country_app.command("add")
def country_add(
    code: Annotated[str, typer.Argument(help="ISO 3166 alfa-2, p. ej. CR")],
    name: Annotated[str, typer.Option(help="Nombre, p. ej. «Costa Rica»")],
    dial_prefix: Annotated[str, typer.Option(help="Prefijo internacional, p. ej. +506 o +1876")],
    currency: Annotated[str, typer.Option(help="Moneda local ISO 4217, p. ej. CRC")],
    timezone: Annotated[
        str | None,
        typer.Option(help="Zona IANA para los topes diarios, p. ej. America/Costa_Rica"),
    ] = None,
) -> None:
    """Agrega un país."""
    code, currency = code.upper(), currency.upper()
    if timezone is None:
        zonas = _zonas_del_pais(code)
        if len(zonas) != 1:
            raise _fail(f"No se puede deducir la zona horaria de {code}: indica --timezone")
        timezone = zonas[0]
    try:
        ZoneInfo(timezone)
    except (ZoneInfoNotFoundError, ValueError):
        raise _fail(f"«{timezone}» no es una zona horaria IANA válida") from None
    cc = phonenumbers.country_code_for_region(code)
    if cc == 0:
        raise _fail(f"«{code}» no es un código de país válido")
    if not re.fullmatch(r"\+[0-9]{1,6}", dial_prefix) or not dial_prefix.startswith(f"+{cc}"):
        raise _fail(f"El prefijo de {code} debe empezar por +{cc} (recibido «{dial_prefix}»)")
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise _fail("La moneda debe tener 3 letras (ISO 4217)")
    with _session() as s:
        s.add(
            Country(
                code=code, name=name, dial_prefix=dial_prefix, currency=currency, timezone=timezone
            )
        )
        _audit(
            s, "country.add", "country", code,
            {"dial_prefix": dial_prefix, "currency": currency, "timezone": timezone},
        )  # fmt: skip
        _commit(s, f"El país {code} ya existe")
    typer.echo(f"País {code} agregado (zona {timezone})")


def _zonas_del_pais(code: str) -> tuple[str, ...]:
    ejemplo = phonenumbers.example_number_for_type(code, phonenumbers.PhoneNumberType.MOBILE)
    return phonenumbers.timezone.time_zones_for_number(ejemplo) if ejemplo else ()


@country_app.command("list")
def country_list() -> None:
    """Lista los países."""
    with _session() as s:
        paises = s.scalars(select(Country).order_by(Country.code)).all()
        _tabla(
            [
                [p.code, p.name, p.dial_prefix, p.currency, "sí" if p.active else "no"]
                for p in paises
            ],
            ["CÓDIGO", "NOMBRE", "PREFIJO", "MONEDA", "ACTIVO"],
        )


# --- Productos --------------------------------------------------------------------------------


@product_app.command("add")
def product_add(
    code: Annotated[str, typer.Argument(help="Código corto en minúsculas, p. ej. whatsapp_bot")],
    name: Annotated[str, typer.Option(help="Nombre legible")],
    country: Annotated[str, typer.Option(help="Código del país (uno por producto)")],
) -> None:
    """Agrega un producto. Empieza en estado test (solo sandbox)."""
    if not _CODIGO_PRODUCTO.fullmatch(code):
        raise _fail("El código solo admite minúsculas, dígitos, «_» y «-» (máx. 50)")
    with _session() as s:
        pais = s.get(Country, country.upper())
        if pais is None or not pais.active:
            raise _fail(f"El país «{country}» no existe o no está activo")
        producto = Product(code=code, name=name, country_code=pais.code)
        s.add(producto)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            raise _fail(f"El producto «{code}» ya existe") from None
        _audit(s, "product.add", "product", producto.id, {"code": code, "country": pais.code})
        s.commit()
    typer.echo(f"Producto «{code}» agregado en estado test")


@product_app.command("list")
def product_list() -> None:
    """Lista los productos."""
    with _session() as s:
        llaves_activas = (
            select(ApiKey.product_id, func.count().label("n"))
            .where(ApiKey.revoked_at.is_(None))
            .group_by(ApiKey.product_id)
            .subquery()
        )
        filas = s.execute(
            select(Product, func.coalesce(llaves_activas.c.n, 0))
            .outerjoin(llaves_activas, llaves_activas.c.product_id == Product.id)
            .order_by(Product.code)
        ).all()
        _tabla(
            [[p.code, p.name, p.country_code, p.status.value, str(n)] for p, n in filas],
            ["CÓDIGO", "NOMBRE", "PAÍS", "ESTADO", "LLAVES ACTIVAS"],
        )


@product_app.command("set-status")
def product_set_status(
    code: Annotated[str, typer.Argument()],
    status: Annotated[ProductStatus, typer.Argument(help="test, live o suspended")],
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación")] = False,
) -> None:
    """Cambia el estado: test (sandbox), live (producción) o suspended (kill switch)."""
    with _session() as s:
        producto = _product(s, code)
        anterior = producto.status
        if anterior == status:
            typer.echo(f"«{code}» ya está en {status.value}")
            return
        if status == ProductStatus.LIVE and not yes:
            typer.confirm(
                f"«{code}» pasará a live: sus llaves sk_live_ ejecutarán recargas con dinero "
                "real en producción. ¿Continuar?",
                abort=True,
            )
        producto.status = status
        _audit(
            s, "product.set_status", "product", producto.id,
            {"from": anterior.value, "to": status.value},
        )  # fmt: skip
        s.commit()
    typer.echo(f"«{code}»: {anterior.value} → {status.value}")


# --- Llaves -----------------------------------------------------------------------------------


@product_app.command("show")
def product_show(code: Annotated[str, typer.Argument()]) -> None:
    """Muestra la configuración de un producto."""
    with _session() as s:
        p = _product(s, code)
        pais = s.get_one(Country, p.country_code)
        ahora = datetime.now(UTC)
        uso = {e: usage(s, p, e, pais.timezone, ahora) for e in Env}
        mon = pais.currency

        def tope(valor: Decimal | None, usado_live: Decimal, usado_test: Decimal) -> str:
            limite = f"{valor} {mon}" if valor is not None else "sin tope"
            return f"{limite} (usado hoy/mes live: {usado_live} {mon}, test: {usado_test} {mon})"

        if p.max_per_phone is None:
            por_tel = "sin límite"
        else:
            por_tel = f"{p.max_per_phone} cada {p.per_phone_window_days or 1} día(s)"
        filas = [
            ("Código", p.code),
            ("Nombre", p.name),
            ("País", f"{p.country_code} (zona {pais.timezone})"),
            ("Estado", p.status.value),
            ("Tope diario", tope(p.daily_cap, uso[Env.LIVE].day, uso[Env.TEST].day)),
            ("Tope mensual", tope(p.monthly_cap, uso[Env.LIVE].month, uso[Env.TEST].month)),
            ("Por teléfono", por_tel),
            ("Límite por minuto", str(p.rate_limit_per_minute)),
            ("IPs permitidas", ", ".join(map(str, p.allowed_ips or [])) or "cualquiera"),
            ("Creado", _fecha(p.created_at)),
        ]
    for etiqueta, valor in filas:
        typer.echo(f"{etiqueta + ':':20} {valor}")


@product_app.command("set-caps")
def product_set_caps(
    code: Annotated[str, typer.Argument()],
    daily: Annotated[str | None, typer.Option(help="Tope diario en moneda local")] = None,
    monthly: Annotated[str | None, typer.Option(help="Tope mensual en moneda local")] = None,
    clear: Annotated[bool, typer.Option("--clear", help="Quitar ambos topes")] = False,
) -> None:
    """Topes de presupuesto en la moneda local del país (cuentan las recargas no fallidas)."""
    if clear == (daily is not None or monthly is not None):
        raise _fail("Indica --daily y/o --monthly, o --clear")
    with _session() as s:
        p = _product(s, code)
        antes = {"daily": str(p.daily_cap), "monthly": str(p.monthly_cap)}
        if clear:
            p.daily_cap = p.monthly_cap = None
        if daily is not None:
            p.daily_cap = _monto(daily)
        if monthly is not None:
            p.monthly_cap = _monto(monthly)
        if p.daily_cap and p.monthly_cap and p.daily_cap > p.monthly_cap:
            raise _fail("El tope diario no puede ser mayor que el mensual")
        despues = {"daily": str(p.daily_cap), "monthly": str(p.monthly_cap)}
        _audit(s, "product.set_caps", "product", p.id, {"from": antes, "to": despues})
        s.commit()
    typer.echo(f"«{code}»: diario {despues['daily']}, mensual {despues['monthly']}")


@product_app.command("set-phone-limit")
def product_set_phone_limit(
    code: Annotated[str, typer.Argument()],
    max_: Annotated[int | None, typer.Option("--max", help="Recargas máximas por número")] = None,
    days: Annotated[int, typer.Option(help="Ventana en días")] = 30,
    clear: Annotated[bool, typer.Option("--clear", help="Quitar el límite")] = False,
) -> None:
    """Máximo de recargas por número de teléfono en una ventana de días."""
    if clear == (max_ is not None):
        raise _fail("Indica --max (y --days), o --clear")
    if max_ is not None and (max_ < 1 or not 1 <= days <= 366):
        raise _fail("--max debe ser al menos 1 y --days entre 1 y 366")
    with _session() as s:
        p = _product(s, code)
        antes = {"max": p.max_per_phone, "days": p.per_phone_window_days}
        p.max_per_phone = max_
        p.per_phone_window_days = None if clear else days
        despues = {"max": p.max_per_phone, "days": p.per_phone_window_days}
        _audit(s, "product.set_phone_limit", "product", p.id, {"from": antes, "to": despues})
        s.commit()
    typer.echo(
        f"«{code}»: sin límite por número"
        if clear
        else f"«{code}»: máximo {max_} recarga(s) por número cada {days} día(s)"
    )


@product_app.command("set-ips")
def product_set_ips(
    code: Annotated[str, typer.Argument()],
    ip: Annotated[
        list[str] | None,
        typer.Option("--ip", help="IP o red CIDR (repetible), p. ej. --ip 203.0.113.10"),
    ] = None,
    clear: Annotated[
        bool, typer.Option("--clear", help="Quitar la lista: aceptar cualquier IP")
    ] = False,
) -> None:
    """Define desde qué IPs puede llamar el producto. Reemplaza la lista anterior."""
    if clear == bool(ip):
        raise _fail("Indica una o más --ip, o --clear")
    redes: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for valor in ip or []:
        try:
            redes.append(ipaddress.ip_network(valor.strip(), strict=True))
        except ValueError:
            raise _fail(f"«{valor}» no es una IP ni una red CIDR válida") from None
    with _session() as s:
        p = _product(s, code)
        anterior = [str(r) for r in p.allowed_ips or []]
        p.allowed_ips = redes or None
        nuevas = [str(r) for r in redes]
        _audit(s, "product.set_ips", "product", p.id, {"from": anterior, "to": nuevas})
        s.commit()
    typer.echo(f"«{code}»: " + (", ".join(nuevas) if nuevas else "acepta cualquier IP"))


@product_app.command("set-rate-limit")
def product_set_rate_limit(
    code: Annotated[str, typer.Argument()],
    per_minute: Annotated[int, typer.Argument(help="Solicitudes por minuto (todas sus llaves)")],
) -> None:
    """Cambia el límite de solicitudes por minuto del producto."""
    if not 1 <= per_minute <= 10_000:
        raise _fail("El límite debe estar entre 1 y 10000")
    with _session() as s:
        p = _product(s, code)
        anterior = p.rate_limit_per_minute
        p.rate_limit_per_minute = per_minute
        _audit(s, "product.set_rate_limit", "product", p.id, {"from": anterior, "to": per_minute})
        s.commit()
    typer.echo(f"«{code}»: {anterior} → {per_minute} solicitudes por minuto")


@key_app.command("create")
def key_create(
    product: Annotated[str, typer.Option(help="Código del producto")],
    env: Annotated[Env, typer.Option(help="test (sandbox) o live (producción)")],
) -> None:
    """Crea una llave. Se muestra una sola vez: guárdala en el servidor del producto."""
    llave = generate_api_key(env)
    with _session() as s:
        producto = _product(s, product)
        registro = ApiKey(
            product_id=producto.id, env=env, prefix=key_prefix(llave), key_hash=hash_api_key(llave)
        )
        s.add(registro)
        s.flush()
        _audit(s, "key.create", "api_key", registro.id, {"product": product, "env": env.value})
        s.commit()
        estado = producto.status
    typer.echo(f"Llave {registro.id} creada para «{product}» ({env.value}):\n")
    typer.secho(f"    {llave}\n", bold=True)
    typer.echo("Es la única vez que se muestra. El gateway solo guarda su hash.")
    if env == Env.LIVE and estado != ProductStatus.LIVE:
        typer.secho(
            f"Aviso: «{product}» está en {estado.value}; esta llave recibirá 403 "
            "product_suspended hasta que el producto pase a live.",
            fg=typer.colors.YELLOW,
        )


@key_app.command("list")
def key_list(product: Annotated[str, typer.Option(help="Código del producto")]) -> None:
    """Lista las llaves de un producto (solo prefijo visible)."""
    with _session() as s:
        producto = _product(s, product)
        llaves = s.scalars(
            select(ApiKey).where(ApiKey.product_id == producto.id).order_by(ApiKey.id)
        ).all()
        _tabla(
            [
                [
                    str(k.id),
                    k.env.value,
                    k.prefix + "…",
                    _fecha(k.created_at),
                    _fecha(k.last_used_at),
                    "REVOCADA " + _fecha(k.revoked_at) if k.revoked_at else "activa",
                ]
                for k in llaves
            ],
            ["ID", "ENTORNO", "PREFIJO", "CREADA", "ÚLTIMO USO", "ESTADO"],
        )


@key_app.command("revoke")
def key_revoke(
    key_id: Annotated[int, typer.Argument(help="ID de la llave (ver «key list»)")],
    yes: Annotated[bool, typer.Option("--yes", help="No pedir confirmación")] = False,
) -> None:
    """Revoca una llave. No se puede deshacer."""
    with _session() as s:
        llave = s.get(ApiKey, key_id)
        if llave is None:
            raise _fail(f"No existe la llave {key_id}")
        if llave.revoked_at is not None:
            typer.echo(f"La llave {key_id} ya estaba revocada")
            return
        if not yes:
            typer.confirm(f"¿Revocar la llave {key_id} ({llave.prefix}…)?", abort=True)
        llave.revoked_at = datetime.now(UTC)
        _audit(s, "key.revoke", "api_key", key_id, {"prefix": llave.prefix})
        s.commit()
    typer.echo(f"Llave {key_id} revocada")


# --- Recompensas ------------------------------------------------------------------------------


def _monto(valor: str) -> Decimal:
    try:
        monto = Decimal(valor)
    except InvalidOperation:
        raise _fail(f"Monto inválido: «{valor}»") from None
    exponente = monto.as_tuple().exponent
    if not monto.is_finite() or monto <= 0 or not isinstance(exponente, int) or exponente < -2:
        raise _fail("El monto debe ser positivo y con 2 decimales como máximo")
    return monto


@reward_app.command("add")
def reward_add(
    product: Annotated[str, typer.Option(help="Código del producto")],
    code: Annotated[str, typer.Option(help="Código en mayúsculas, p. ej. CURSO_FINALIZADO")],
    label: Annotated[str, typer.Option(help="Descripción legible")],
    amount: Annotated[str, typer.Option(help="Monto en la moneda local del país del producto")],
    allow_partial: Annotated[
        bool,
        typer.Option(
            "--allow-partial", help="Aceptar aunque algún operador no tenga un SKU para ese monto"
        ),
    ] = False,
) -> None:
    """Agrega una recompensa, validando el monto contra el catálogo de cada operador (RF3).

    La moneda es la del país del producto. Se valida contra el catálogo del entorno del
    producto: sandbox si está en test; producción si está en live o suspended.
    """
    if not _CODIGO_RECOMPENSA.fullmatch(code):
        raise _fail("El código solo admite mayúsculas, dígitos y «_» (máx. 50)")
    monto = _monto(amount)
    with _session() as s:
        producto = _product(s, product)
        moneda = s.get_one(Country, producto.country_code).currency
        env = Env.TEST if producto.status == ProductStatus.TEST else Env.LIVE
        skus = talktime_skus(s, env, producto.country_code)
        if not skus:
            typer.secho(
                f"Aviso: no hay catálogo de {ENV_TO_PN_ENV[env].value} para "
                f"{producto.country_code}; el monto no se pudo validar "
                f"(gateway catalog sync --env {ENV_TO_PN_ENV[env].value}).",
                fg=typer.colors.YELLOW,
            )
        else:
            cobertura = coverage(skus, monto, moneda)
            for op in cobertura:
                if op.covered:
                    typer.echo(f"  ✓ {op.operator_name}: SKU {op.covering[0].sku_id}")
                else:
                    rangos = ", ".join(
                        f"{k.min_delivered}-{k.max_delivered} {k.delivery_currency}"
                        for k in op.skus
                    )
                    typer.secho(
                        f"  ✗ {op.operator_name}: ningún SKU entrega {monto} {moneda} "
                        f"(disponible: {rangos})",
                        fg=typer.colors.RED,
                    )
            faltan = [op.operator_name for op in cobertura if not op.covered]
            if len(faltan) == len(cobertura):
                raise _fail(f"Ningún operador puede entregar {monto} {moneda}")
            if faltan and not allow_partial:
                raise _fail(
                    "Sin SKU para: " + ", ".join(faltan) + ". Usa --allow-partial para aceptar "
                    "que esos números reciban FAILED."
                )
        recompensa = Reward(
            product_id=producto.id, code=code, label=label, amount=monto, currency=moneda
        )
        s.add(recompensa)
        try:
            s.flush()
        except IntegrityError:
            s.rollback()
            raise _fail(f"«{product}» ya tiene la recompensa {code}") from None
        _audit(
            s, "reward.add", "reward", recompensa.id,
            {"product": product, "code": code, "amount": str(monto), "currency": moneda},
        )  # fmt: skip
        s.commit()
    typer.echo(f"Recompensa {code} agregada: {monto} {moneda}")


@reward_app.command("list")
def reward_list(
    product: Annotated[str, typer.Option(help="Código del producto")],
    all_: Annotated[bool, typer.Option("--all", help="Incluir desactivadas")] = False,
) -> None:
    """Lista las recompensas de un producto."""
    with _session() as s:
        producto = _product(s, product)
        consulta = select(Reward).where(Reward.product_id == producto.id).order_by(Reward.code)
        if not all_:
            consulta = consulta.where(Reward.active)
        _tabla(
            [
                [r.code, r.label, f"{r.amount} {r.currency}", "sí" if r.active else "no"]
                for r in s.scalars(consulta)
            ],
            ["CÓDIGO", "DESCRIPCIÓN", "MONTO", "ACTIVA"],
        )


@reward_app.command("disable")
def reward_disable(
    product: Annotated[str, typer.Option(help="Código del producto")],
    code: Annotated[str, typer.Option(help="Código de la recompensa")],
) -> None:
    """Desactiva una recompensa: las solicitudes nuevas reciben 422 unknown_reward."""
    with _session() as s:
        producto = _product(s, product)
        recompensa = s.scalar(
            select(Reward).where(Reward.product_id == producto.id, Reward.code == code)
        )
        if recompensa is None:
            raise _fail(f"«{product}» no tiene la recompensa {code}")
        if not recompensa.active:
            typer.echo(f"{code} ya estaba desactivada")
            return
        recompensa.active = False
        _audit(s, "reward.disable", "reward", recompensa.id, {"product": product, "code": code})
        s.commit()
    typer.echo(f"Recompensa {code} desactivada")


# --- Credenciales de Prepaynation -------------------------------------------------------------


@credentials_app.command("set")
def credentials_set(
    env: Annotated[PnEnv, typer.Option(help="sandbox o production")],
    user_id: Annotated[str, typer.Option(prompt="userId de Prepaynation")],
) -> None:
    """Guarda las credenciales de Prepaynation. El password se pide sin mostrarlo y se cifra."""
    if env == PnEnv.PRODUCTION:
        typer.confirm(
            "Vas a cargar credenciales de PRODUCCIÓN (dinero real). ¿Continuar?", abort=True
        )
    password = typer.prompt("Password", hide_input=True, confirmation_prompt=True)
    if not password:
        raise _fail("El password no puede estar vacío")
    try:
        cifrado = encrypt_secret(password)
    except MasterKeyError as e:
        raise _fail(str(e)) from None
    del password
    entorno = PN_ENV_TO_ENV[env]
    with _session() as s:
        fila = s.get(Setting, entorno) or Setting(env=entorno)
        fila.pn_user_id = user_id
        fila.pn_password_enc = cifrado
        s.add(fila)
        _audit(s, "credentials.set", "settings", env.value)
        s.commit()
    typer.echo(f"Credenciales de {env.value} guardadas (password cifrado)")


@credentials_app.command("show")
def credentials_show() -> None:
    """Muestra qué credenciales están cargadas (nunca el password)."""
    with _session() as s:
        filas = {f.env: f for f in s.scalars(select(Setting))}
        _tabla(
            [
                [
                    pn_env.value,
                    (f.pn_user_id or "—") if f else "—",
                    "cargado" if f and f.pn_password_enc else "falta",
                    _fecha(f.updated_at) if f else "—",
                ]
                for pn_env, f in ((p, filas.get(e)) for p, e in PN_ENV_TO_ENV.items())
            ],
            ["ENTORNO", "USERID", "PASSWORD", "ACTUALIZADO"],
        )


# --- Catálogo y saldo -------------------------------------------------------------------------


@contextmanager
def _client(env: PnEnv) -> Iterator[PrepaynationClient]:
    entorno = PN_ENV_TO_ENV[env]
    with _session() as s:
        try:
            cliente = client_for_env(
                s, entorno, on_call=persist_calls(db.get_sessionmaker(), entorno)
            )
        except (MissingCredentialsError, MasterKeyError, DecryptionError) as e:
            raise _fail(str(e)) from None
    with cliente:
        yield cliente


@catalog_app.command("sync")
def catalog_sync(env: Annotated[PnEnv, typer.Option(help="sandbox o production")]) -> None:
    """Descarga productos RTR, SKUs y códigos de error, y reemplaza la caché del entorno."""
    with _client(env) as cliente, _session() as s:
        try:
            r = sync_catalog(s, cliente)
        except PnError as e:
            raise _fail(f"No se sincronizó: {e}") from None
        _audit(
            s, "catalog.sync", "pn_catalog", env.value,
            {"products": r.products, "skus": r.skus, "errors": r.errors},
        )  # fmt: skip
        s.commit()
    typer.echo(
        f"Catálogo de {env.value}: {r.products} productos RTR, {r.skus} SKUs, "
        f"{r.errors} códigos de error"
    )


@catalog_app.command("show")
def catalog_show(
    country: Annotated[str, typer.Option(help="Código de país, p. ej. CR")],
    env: Annotated[PnEnv, typer.Option(help="sandbox o production")] = PnEnv.SANDBOX,
    all_: Annotated[
        bool, typer.Option("--all", help="Incluir paquetes de datos y bundles, no solo saldo")
    ] = False,
) -> None:
    """Muestra los SKUs en caché de un país (por defecto, solo recargas de saldo)."""
    entorno = PN_ENV_TO_ENV[env]
    with _session() as s:
        if all_:
            skus = list(
                s.scalars(
                    select(PnCatalogSku)
                    .where(
                        PnCatalogSku.env == entorno, PnCatalogSku.country_code == country.upper()
                    )
                    .order_by(PnCatalogSku.operator_name, PnCatalogSku.sku_id)
                )
            )
        else:
            skus = talktime_skus(s, entorno, country.upper())
        _tabla(
            [
                [
                    k.sku_id,
                    k.operator_name or "—",
                    k.benefit_type or "—",
                    _rango(k.min_amount, k.max_amount, k.face_value_currency),
                    _rango(k.min_delivered, k.max_delivered, k.delivery_currency),
                    str(k.exchange_rate or "—"),
                    k.validity or "",
                ]
                for k in skus
            ],
            ["SKU", "OPERADOR", "TIPO", "FACE VALUE", "ENTREGADO", "CAMBIO", "VIGENCIA"],
        )
        if skus:
            typer.echo(f"\nActualizado: {_fecha(skus[0].refreshed_at)}")


def _rango(mn: Decimal | None, mx: Decimal | None, moneda: str | None) -> str:
    if mn is None or mx is None:
        return "—"
    valor = f"{mn.normalize():f}" if mn == mx else f"{mn.normalize():f}-{mx.normalize():f}"
    return f"{valor} {moneda or ''}".strip()


@app.command()
def balance(env: Annotated[PnEnv, typer.Option(help="sandbox o production")]) -> None:
    """Saldo disponible del wallet de Prepaynation."""
    with _client(env) as cliente:
        try:
            r = cliente.balance()
        except PnError as e:
            raise _fail(f"Error consultando el saldo: {e}") from None
    if not r.ok or r.payload is None:
        raise _fail(
            f"Prepaynation respondió HTTP {r.http_status}, código {r.response_code!r} "
            f"({r.response_message or 'sin mensaje'})"
        )
    typer.echo(f"Saldo disponible ({env.value}): {available_balance(r.payload)}")
