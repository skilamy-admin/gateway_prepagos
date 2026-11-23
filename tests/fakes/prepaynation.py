"""Prepaynation simulado con httpx.MockTransport, con datos con la forma de la API v2.

Los SKUs imitan Product_Catalog.xlsx: Jamaica (Digicel de rango, Flow fijo) y Costa Rica.
"""

import base64
import json
from collections.abc import Callable
from typing import Any

import httpx


def ok(payload: Any) -> dict[str, Any]:
    return {"responseCode": "000", "responseMessage": "Success", "payLoad": payload}


def sku(
    sku_id: int, product_id: int, operator_id: int, operator: str, country: str,
    fv: tuple[float, float], delivered: tuple[float, float], delivery_ccy: str,
    rate: float, benefit: str = "Talktime", fv_ccy: str = "USD",
) -> dict[str, Any]:  # fmt: skip
    def extremo(i: int) -> dict[str, Any]:
        return {
            "faceValue": fv[i], "faceValueCurrency": fv_ccy,
            "deliveredAmount": delivered[i], "deliveryCurrencyCode": delivery_ccy,
            "faceValueInWalletCurrency": fv[i],
        }  # fmt: skip

    return {
        "skuId": sku_id, "skuName": f"{operator} {country} {sku_id}", "productId": product_id,
        "productName": f"{operator} {country}", "category": "RTR", "countryCode": country,
        "operatorId": operator_id, "operatorName": operator, "benefitType": benefit,
        "exchangeRate": rate, "min": extremo(0), "max": extremo(1),
        "allowDecimal": fv[0] != fv[1], "fee": 0, "deliveryAmountType": "estimated",
    }  # fmt: skip


PRODUCTS = [
    {"productId": 57, "category": "RTR", "productName": "Digicel Jamaica", "countryCode": "JM",
     "operatorId": 1, "operatorName": "Digicel"},
    {"productId": 923, "category": "RTR", "productName": "Flow-Jamaica", "countryCode": "JM",
     "operatorId": 2, "operatorName": "Flow"},
    {"productId": 784, "category": "RTR", "productName": "kolbi", "countryCode": "CR",
     "operatorId": 10, "operatorName": "Kolbi"},
    {"productId": 411, "category": "RTR", "productName": "Claro Costa Rica", "countryCode": "CR",
     "operatorId": 11, "operatorName": "Claro"},
]  # fmt: skip

SKUS = [
    sku(193, 57, 1, "Digicel", "JM", (5.25, 232.0), (838.61, 37058.40), "JMD", 159.73),
    sku(4056, 923, 2, "Flow", "JM", (5.0, 5.0), (799.60, 799.60), "JMD", 159.92),
    sku(4057, 923, 2, "Flow", "JM", (10.0, 10.0), (1599.20, 1599.20), "JMD", 159.92),
    sku(3390, 784, 10, "Kolbi", "CR", (3.0, 50.0), (1512.0, 25200.0), "CRC", 504.0),
    sku(1886, 411, 11, "Claro", "CR", (7.0, 35.0), (3474.73, 17373.65), "CRC", 496.39),
    # Un SKU de PIN (otro producto, no RTR): no debe entrar en la caché.
    sku(9999, 5000, 99, "Pin", "US", (10.0, 10.0), (10.0, 10.0), "USD", 1.0),
]

ERRORS = [
    {"errorCode": "501", "errorDescription": "Invalid mobile number"},
    {"errorCode": "510", "errorDescription": "Out of stock"},
    {"errorCode": "852", "errorDescription": "Transaction in progress"},
]


Handler = Callable[[httpx.Request], httpx.Response]


def catalog_handler(
    *, products: Any = None, skus: Any = None, errors: Any = None, status: int = 200
) -> Handler:
    rutas = {
        "/api/v2/catalog/getproducts": ok(PRODUCTS) if products is None else products,
        "/api/v2/catalog/skus": ok(SKUS) if skus is None else skus,
        "/api/v2/catalog/errors": ok(ERRORS) if errors is None else errors,
        "/api/v2/account/balance": ok(-1234.56),
    }

    def handler(request: httpx.Request) -> httpx.Response:
        cuerpo = rutas.get(request.url.path)
        if cuerpo is None:
            return httpx.Response(404, json={"title": "Not Found"})
        return httpx.Response(status, content=json.dumps(cuerpo).encode())

    return handler


def basic_auth_of(request: httpx.Request) -> tuple[str, str]:
    valor = request.headers["Authorization"].removeprefix("Basic ")
    usuario, _, password = base64.b64decode(valor).decode().partition(":")
    return usuario, password
