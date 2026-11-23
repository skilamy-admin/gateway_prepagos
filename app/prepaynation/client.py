"""Cliente HTTP de Valuetopup API v2. Un método por endpoint.

Reglas (CLAUDE.md):
- Éxito = HTTP 200 y responseCode "000". El dato viene en `payLoad`.
- Autenticación Basic con las credenciales del entorno; sandbox y producción nunca se mezclan.
- Timeout de 130 s en topup (Prepaynation tarda hasta 2 min) y 30 s en el resto.
- Este cliente nunca reintenta: un timeout de topup lo resuelve el worker con /transaction/status.

Cada llamada se entrega a `on_call` como CallRecord (sin credenciales) para registrarla en
topup_events. El cliente no toca la base: así se prueba sin PostgreSQL.
"""

import json
import time
from collections.abc import Callable
from dataclasses import dataclass
from decimal import Decimal
from typing import Any, Final

import httpx
from pydantic import TypeAdapter, ValidationError
from sqlalchemy.orm import Session

from app.models import Env, Setting
from app.prepaynation.models import (
    CostInformation,
    Country,
    ErrorCode,
    LookupProduct,
    Operator,
    OrderReceipt,
    Product,
    Sku,
)
from app.security import decrypt_secret

BASE_URLS: Final = {
    Env.TEST: "https://sandbox.valuetopup.com/api/v2",
    Env.LIVE: "https://www.valuetopup.com/api/v2",
}
SUCCESS: Final = "000"
IN_PROGRESS: Final = "852"
CATEGORY_RTR: Final = 2

TOPUP_TIMEOUT: Final = httpx.Timeout(130.0, connect=10.0)
DEFAULT_TIMEOUT: Final = httpx.Timeout(30.0, connect=10.0)
_MAX_RAW: Final = 4000  # caracteres de una respuesta no JSON que se guardan en el registro


@dataclass(frozen=True)
class CallRecord:
    """Lo que se guarda en topup_events. Nunca incluye cabeceras ni credenciales."""

    method: str
    path: str
    request: dict[str, Any] | None
    response: Any
    http_status: int | None
    latency_ms: int
    error: str | None = None


@dataclass(frozen=True)
class PnResponse[T]:
    http_status: int
    response_code: str | None
    response_message: str | None
    payload: T | None  # solo se interpreta cuando ok es True
    raw: Any

    @property
    def ok(self) -> bool:
        return self.http_status == 200 and self.response_code == SUCCESS

    @property
    def in_progress(self) -> bool:
        return self.http_status == 200 and self.response_code == IN_PROGRESS


class PnError(Exception):
    """Error al hablar con Prepaynation."""


class PnTransportError(PnError):
    """Timeout o error de red: no hay respuesta. En un topup, el resultado es desconocido.

    `may_have_reached` es False solo si la conexión ni siquiera se estableció. Aun así, el
    worker no reenvía: consulta /transaction/status.
    """

    def __init__(self, mensaje: str, *, timeout: bool, may_have_reached: bool) -> None:
        super().__init__(mensaje)
        self.timeout = timeout
        self.may_have_reached = may_have_reached


class PnPayloadError(PnError):
    """HTTP 200 + "000" pero el payLoad no tiene la forma esperada."""


def mobile_for_pn(e164: str) -> str:
    """Prepaynation recibe el número con código de país y sin «+» (p. ej. 18765550123)."""
    digitos = e164.removeprefix("+")
    if not digitos.isdigit():
        raise ValueError("número no está en formato E.164")
    return digitos


def available_balance(balance: Decimal) -> Decimal:
    """En cuentas prepagas el saldo viene negativo: -50 significa 50 disponibles."""
    return -balance


def _json_amount(valor: Decimal) -> float:
    """Monto como número JSON, verificando que no se pierda precisión."""
    f = float(valor)
    if Decimal(repr(f)) != valor:
        raise ValueError(f"el monto {valor} no se puede representar con exactitud")
    return f


class PrepaynationClient:
    def __init__(
        self,
        env: Env,
        user_id: str,
        password: str,
        *,
        on_call: Callable[[CallRecord], None] | None = None,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        self.env = env
        self._on_call = on_call
        self._http = httpx.Client(
            base_url=BASE_URLS[env],
            auth=httpx.BasicAuth(user_id, password),
            timeout=DEFAULT_TIMEOUT,
            headers={"Accept": "application/json"},
            transport=transport,
        )

    def __repr__(self) -> str:
        return f"PrepaynationClient(env={self.env.value})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> "PrepaynationClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # --- Núcleo -------------------------------------------------------------------------------

    def _call[T](
        self,
        method: str,
        path: str,
        adapter: TypeAdapter[T],
        *,
        params: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        timeout: httpx.Timeout = DEFAULT_TIMEOUT,
    ) -> PnResponse[T]:
        registro_req: dict[str, Any] | None = None
        if params or body:
            registro_req = {k: v for k, v in (("params", params), ("body", body)) if v}
        inicio = time.monotonic()
        try:
            r = self._http.request(method, path, params=params, json=body, timeout=timeout)
        except httpx.TransportError as e:
            ms = int((time.monotonic() - inicio) * 1000)
            es_timeout = isinstance(e, httpx.TimeoutException)
            sin_conexion = isinstance(e, httpx.ConnectError | httpx.ConnectTimeout)
            self._registrar(
                CallRecord(method, path, registro_req, None, None, ms, type(e).__name__)
            )
            raise PnTransportError(
                f"{type(e).__name__} en {method} {path}",
                timeout=es_timeout,
                may_have_reached=not sin_conexion,
            ) from None
        ms = int((time.monotonic() - inicio) * 1000)

        try:
            datos: Any = json.loads(r.text, parse_float=Decimal)
            registro_resp: Any = json.loads(r.text)
        except ValueError:
            datos = None
            registro_resp = {"_no_json": r.text[:_MAX_RAW]}
        self._registrar(CallRecord(method, path, registro_req, registro_resp, r.status_code, ms))

        codigo = mensaje = None
        bruto = None
        if isinstance(datos, dict):
            codigo = datos.get("responseCode")
            mensaje = datos.get("responseMessage")
            bruto = datos.get("payLoad")
        respuesta: PnResponse[T] = PnResponse(r.status_code, codigo, mensaje, None, datos)
        if not respuesta.ok:
            return respuesta
        try:
            payload = adapter.validate_python(bruto)
        except ValidationError as e:
            raise PnPayloadError(f"payLoad inesperado en {method} {path}: {e}") from None
        return PnResponse(r.status_code, codigo, mensaje, payload, datos)

    def _registrar(self, registro: CallRecord) -> None:
        if self._on_call is not None:
            self._on_call(registro)

    # --- Endpoints ----------------------------------------------------------------------------

    def balance(self) -> PnResponse[Decimal]:
        return self._call("GET", "/account/balance", TypeAdapter(Decimal))

    def countries(self) -> PnResponse[list[Country]]:
        return self._call("GET", "/catalog/countries", TypeAdapter(list[Country]))

    def operators(self, country_code: str) -> PnResponse[list[Operator]]:
        return self._call(
            "GET", "/catalog/operators", TypeAdapter(list[Operator]),
            params={"countryCode": country_code},
        )  # fmt: skip

    def products(
        self, category_id: int = CATEGORY_RTR, country_code: str | None = None
    ) -> PnResponse[list[Product]]:
        params: dict[str, Any] = {"categoryId": category_id}
        if country_code:
            params["countryCode"] = country_code
        return self._call("GET", "/catalog/getproducts", TypeAdapter(list[Product]), params=params)

    def skus(self, product_id: int | None = None) -> PnResponse[list[Sku]]:
        params = {"productId": product_id} if product_id is not None else None
        return self._call("GET", "/catalog/skus", TypeAdapter(list[Sku]), params=params)

    def errors(self) -> PnResponse[list[ErrorCode]]:
        return self._call("GET", "/catalog/errors", TypeAdapter(list[ErrorCode]))

    def lookup_mobile(self, e164: str) -> PnResponse[list[LookupProduct]]:
        return self._call(
            "GET", f"/lookup/mobile/{mobile_for_pn(e164)}", TypeAdapter(list[LookupProduct])
        )

    def topup(
        self, *, sku_id: int, amount: Decimal, currency: str, e164: str, correlation_id: str
    ) -> PnResponse[OrderReceipt]:
        """Recarga. Con PnTransportError NO reintentar: consultar transaction_status."""
        if not 1 <= len(correlation_id) <= 50:
            raise ValueError("correlationId debe tener entre 1 y 50 caracteres")
        body = {
            "skuId": sku_id,
            "amount": _json_amount(amount),
            "mobile": mobile_for_pn(e164),
            "correlationId": correlation_id,
            "transactionCurrencyCode": currency,  # regla 5: siempre la moneda local
        }
        return self._call(
            "POST", "/transaction/topup", TypeAdapter(OrderReceipt),
            body=body, timeout=TOPUP_TIMEOUT,
        )  # fmt: skip

    def transaction_status(self, correlation_id: str) -> PnResponse[OrderReceipt]:
        return self._call("GET", f"/transaction/status/{correlation_id}", TypeAdapter(OrderReceipt))

    def estimate_cost(
        self, *, sku_id: int, amount: Decimal, currency: str
    ) -> PnResponse[CostInformation]:
        body = {
            "skuId": sku_id,
            "amount": _json_amount(amount),
            "transactionCurrencyCode": currency,
        }
        return self._call(
            "POST", "/transaction/estimatecost", TypeAdapter(CostInformation), body=body
        )


class MissingCredentialsError(PnError):
    pass


def client_for_env(
    session: Session,
    env: Env,
    *,
    on_call: Callable[[CallRecord], None] | None = None,
    transport: httpx.BaseTransport | None = None,
) -> PrepaynationClient:
    """Cliente con las credenciales de ese entorno (test → sandbox, live → producción)."""
    fila = session.get(Setting, env)
    if fila is None or not fila.pn_user_id or not fila.pn_password_enc:
        nombre = "sandbox" if env == Env.TEST else "production"
        raise MissingCredentialsError(
            f"Faltan credenciales de {nombre}: gateway credentials set --env {nombre}"
        )
    return PrepaynationClient(
        env,
        fila.pn_user_id,
        decrypt_secret(fila.pn_password_enc),
        on_call=on_call,
        transport=transport,
    )
