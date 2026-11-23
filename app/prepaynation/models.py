"""Respuestas tipadas de Valuetopup API v2 (según la especificación OpenAPI oficial).

Todos los campos son opcionales: la API marca casi todo como nullable y puede agregar campos.
Los números se leen como Decimal (nunca float) porque son montos de dinero.
"""

from decimal import Decimal

from pydantic import BaseModel, ConfigDict


class _Pn(BaseModel):
    model_config = ConfigDict(extra="ignore", frozen=True)


class Amount(_Pn):
    """`min` / `max` de un SKU."""

    faceValue: Decimal | None = None
    faceValueCurrency: str | None = None
    deliveredAmount: Decimal | None = None
    deliveryCurrencyCode: str | None = None
    faceValueInWalletCurrency: Decimal | None = None


class Sku(_Pn):
    skuId: int
    skuName: str | None = None
    min: Amount | None = None
    max: Amount | None = None
    exchangeRate: Decimal | None = None
    productId: int | None = None
    productName: str | None = None
    category: str | None = None
    countryCode: str | None = None
    benefitType: str | None = None
    validity: str | None = None
    productDescription: str | None = None
    allowDecimal: bool | None = None
    fee: Decimal | None = None
    operatorId: int | None = None
    operatorName: str | None = None
    deliveryAmountType: str | None = None


class Product(_Pn):
    """Elemento de /catalog/getproducts."""

    productId: int
    category: str | None = None
    productName: str | None = None
    countryCode: str | None = None
    operatorId: int | None = None
    operatorName: str | None = None


class Operator(_Pn):
    operatorId: int
    operatorName: str | None = None


class Country(_Pn):
    countryCode: str
    countryName: str | None = None
    internationalCountryCode: str | None = None
    areaCodes: str | None = None
    numberLength: str | None = None


class ErrorCode(_Pn):
    errorCode: str
    errorDescription: str | None = None


class LookupProduct(_Pn):
    productId: int | None = None
    productName: str | None = None
    countryCode: str | None = None
    operatorId: int | None = None
    operatorName: str | None = None
    skus: list[Sku] | None = None


class TopupDetail(_Pn):
    localCurrencyAmount: Decimal | None = None
    salesTaxAmount: Decimal | None = None
    localCurrencyAmountExcludingTax: Decimal | None = None
    destinationCurrency: str | None = None
    operatorTransactionId: str | None = None


class ReceiptProduct(_Pn):
    skuId: int | None = None
    productName: str | None = None
    faceValue: Decimal | None = None


class OrderReceipt(_Pn):
    """payLoad de /transaction/topup y /transaction/status."""

    transactionId: int | None = None
    transactionDate: str | None = None
    invoiceAmount: Decimal | None = None
    faceValue: Decimal | None = None
    discount: Decimal | None = None
    fee: Decimal | None = None
    product: ReceiptProduct | None = None
    topupDetail: TopupDetail | None = None


class CostInformation(_Pn):
    invoiceAmount: Decimal | None = None
    faceValue: Decimal | None = None
    faceValueCurrency: str | None = None
    faceValueInWallet: Decimal | None = None
    localCurrencyAmount: Decimal | None = None
    destinationCurrency: str | None = None
