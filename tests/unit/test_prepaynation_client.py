import json
from decimal import Decimal

import httpx
import pytest

from app.models import Env
from app.prepaynation.client import (
    BASE_URLS,
    CallRecord,
    PnPayloadError,
    PnTransportError,
    PrepaynationClient,
    available_balance,
    mobile_for_pn,
)
from tests.fakes.prepaynation import basic_auth_of, catalog_handler, ok

USUARIO, PASSWORD = "usuario-prueba", "P4ss!word-secreta"


def cliente(
    handler: object, registros: list[CallRecord] | None = None, env: Env = Env.TEST
) -> PrepaynationClient:
    return PrepaynationClient(
        env, USUARIO, PASSWORD,
        on_call=None if registros is None else registros.append,
        transport=httpx.MockTransport(handler),  # type: ignore[arg-type]
    )  # fmt: skip


def responde(cuerpo: object, status: int = 200) -> object:
    vistos: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        vistos.append(request)
        contenido = cuerpo if isinstance(cuerpo, bytes) else json.dumps(cuerpo).encode()
        return httpx.Response(status, content=contenido)

    handler.vistos = vistos  # type: ignore[attr-defined]
    return handler


RECIBO = {
    "transactionId": 123456, "transactionDate": "2026-11-23T20:00:00", "invoiceAmount": 3.01,
    "faceValue": 3.0, "discount": 0.0, "fee": 0.01,
    "topupDetail": {"localCurrencyAmount": 1512.0, "destinationCurrency": "CRC"},
}  # fmt: skip


def test_urls_por_entorno() -> None:
    assert BASE_URLS[Env.TEST] == "https://sandbox.valuetopup.com/api/v2"
    assert BASE_URLS[Env.LIVE] == "https://www.valuetopup.com/api/v2"
    h = responde(ok(-10))
    cliente(h).balance()
    cliente(h, env=Env.LIVE).balance()
    hosts = [r.url.host for r in h.vistos]  # type: ignore[attr-defined]
    assert hosts == ["sandbox.valuetopup.com", "www.valuetopup.com"]


def test_basic_auth_y_registro_sin_credenciales() -> None:
    h = responde(ok(-50))
    registros: list[CallRecord] = []
    r = cliente(h, registros).balance()
    assert basic_auth_of(h.vistos[0]) == (USUARIO, PASSWORD)  # type: ignore[attr-defined]
    assert r.ok and r.payload == Decimal("-50")
    assert len(registros) == 1
    assert PASSWORD not in repr(registros[0]) and USUARIO not in repr(registros[0])
    assert PASSWORD not in repr(cliente(h))


def test_saldo_negativo_es_disponible() -> None:
    assert available_balance(Decimal("-50")) == Decimal("50")


def test_exito_solo_con_200_y_000() -> None:
    r = cliente(responde({"responseCode": "510", "responseMessage": "Out of stock"})).balance()
    assert not r.ok and r.response_code == "510" and r.payload is None
    r = cliente(responde(ok(-5), status=500)).balance()
    assert not r.ok and r.http_status == 500


def test_401_sin_json_de_prepaynation() -> None:
    registros: list[CallRecord] = []
    r = cliente(responde({"title": "Unauthorized", "status": 401}, 401), registros).balance()
    assert not r.ok and r.http_status == 401 and r.response_code is None
    assert registros[0].http_status == 401


def test_respuesta_no_json() -> None:
    registros: list[CallRecord] = []
    r = cliente(responde(b"<html>error</html>", 502), registros).balance()
    assert not r.ok and r.raw is None
    assert registros[0].response == {"_no_json": "<html>error</html>"}


def test_montos_como_decimal_exacto() -> None:
    r = cliente(responde(ok(RECIBO))).transaction_status("C1")
    assert r.payload is not None
    assert r.payload.invoiceAmount == Decimal("3.01")
    assert r.payload.topupDetail is not None
    assert r.payload.topupDetail.localCurrencyAmount == Decimal("1512.0")


def test_payload_inesperado() -> None:
    with pytest.raises(PnPayloadError):
        cliente(responde(ok("no-es-un-numero"))).balance()


def test_topup_cuerpo_y_timeout() -> None:
    h = responde(ok(RECIBO))
    registros: list[CallRecord] = []
    r = cliente(h, registros).topup(
        sku_id=3390, amount=Decimal("1512.00"), currency="CRC",
        e164="+50688887777", correlation_id="01JDCORRELATION",
    )  # fmt: skip
    req = h.vistos[0]  # type: ignore[attr-defined]
    assert req.method == "POST" and req.url.path == "/api/v2/transaction/topup"
    assert json.loads(req.content) == {
        "skuId": 3390, "amount": 1512.0, "mobile": "50688887777",
        "correlationId": "01JDCORRELATION", "transactionCurrencyCode": "CRC",
    }  # fmt: skip
    assert req.extensions["timeout"]["read"] == 130.0
    assert r.ok and r.payload is not None and r.payload.transactionId == 123456
    assert registros[0].request == {"body": json.loads(req.content)}


def test_otros_endpoints_timeout_30() -> None:
    h = responde(ok(-1))
    cliente(h).balance()
    assert h.vistos[0].extensions["timeout"]["read"] == 30.0  # type: ignore[attr-defined]


@pytest.mark.parametrize("cid", ["", "x" * 51])
def test_correlation_id_fuera_de_rango(cid: str) -> None:
    with pytest.raises(ValueError):
        cliente(responde(ok(RECIBO))).topup(
            sku_id=1, amount=Decimal(1), currency="JMD", e164="+18765550123", correlation_id=cid
        )


def test_monto_no_representable() -> None:
    with pytest.raises(ValueError):
        cliente(responde(ok(RECIBO))).topup(
            sku_id=1, amount=Decimal("0.1000000000000000000001"), currency="JMD",
            e164="+18765550123", correlation_id="C",
        )  # fmt: skip


@pytest.mark.parametrize(
    ("error", "es_timeout", "pudo_llegar"),
    [
        (httpx.ReadTimeout("t"), True, True),
        (httpx.ConnectTimeout("t"), True, False),
        (httpx.ConnectError("c"), False, False),
        (httpx.RemoteProtocolError("r"), False, True),
    ],
)
def test_errores_de_red_no_se_reintentan(
    error: Exception, es_timeout: bool, pudo_llegar: bool
) -> None:
    llamadas = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal llamadas
        llamadas += 1
        raise error

    registros: list[CallRecord] = []
    with pytest.raises(PnTransportError) as e:
        cliente(handler, registros).topup(
            sku_id=1, amount=Decimal(5), currency="JMD", e164="+18765550123", correlation_id="C"
        )
    assert llamadas == 1
    assert (e.value.timeout, e.value.may_have_reached) == (es_timeout, pudo_llegar)
    assert registros[0].error == type(error).__name__ and registros[0].http_status is None


def test_status_en_curso() -> None:
    r = cliente(
        responde({"responseCode": "852", "responseMessage": "In progress"})
    ).transaction_status("C")
    assert r.in_progress and not r.ok


def test_mobile_sin_mas() -> None:
    assert mobile_for_pn("+18765550123") == "18765550123"
    with pytest.raises(ValueError):
        mobile_for_pn("+1 876")


def test_catalogo_simulado() -> None:
    c = cliente(catalog_handler())
    assert {p.productId for p in c.products().payload or []} == {57, 923, 784, 411}
    skus = c.skus().payload or []
    kolbi = next(s for s in skus if s.skuId == 3390)
    assert kolbi.min is not None and kolbi.min.deliveredAmount == Decimal("1512.0")
