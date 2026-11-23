"""Aplicación FastAPI. Escucha solo en 127.0.0.1; Nginx la publica."""

from fastapi import FastAPI

from app import __version__
from app.api.errors import install_error_handlers
from app.api.routes_topups import router as topups_router


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gateway_prepagos",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    install_error_handlers(app)
    app.include_router(topups_router)

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
