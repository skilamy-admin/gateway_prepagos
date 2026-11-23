"""Errores de la API v1. Formato: {"error": "<código>", "message": "<texto>"}.

Los códigos son los del PRD (invalid_key, product_suspended, unknown_reward, invalid_phone…).
Nunca se incluyen secretos ni detalles internos.
"""

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException


class ApiError(Exception):
    def __init__(
        self, status: int, code: str, message: str, headers: dict[str, str] | None = None
    ) -> None:
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message
        self.headers = headers


def _respuesta(
    status: int, code: str, message: str, headers: dict[str, str] | None = None
) -> JSONResponse:
    return JSONResponse({"error": code, "message": message}, status_code=status, headers=headers)


def install_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def api_error(_: Request, exc: ApiError) -> JSONResponse:
        return _respuesta(exc.status, exc.code, exc.message, exc.headers)

    @app.exception_handler(RequestValidationError)
    async def validation_error(_: Request, exc: RequestValidationError) -> JSONResponse:
        campos = sorted({".".join(str(p) for p in e["loc"][1:]) or "body" for e in exc.errors()})
        return _respuesta(422, "invalid_request", "Solicitud inválida: " + ", ".join(campos))

    @app.exception_handler(StarletteHTTPException)
    async def http_error(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        codigo = {404: "not_found", 405: "method_not_allowed"}.get(exc.status_code, "http_error")
        return _respuesta(exc.status_code, codigo, str(exc.detail))

    @app.exception_handler(Exception)
    async def unexpected(_: Request, __: Exception) -> JSONResponse:
        # El detalle va al log del servidor (uvicorn), nunca al producto.
        return _respuesta(500, "internal_error", "Error interno del gateway")
