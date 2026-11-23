"""Aplicación FastAPI. Escucha solo en 127.0.0.1; Nginx la publica."""

from fastapi import FastAPI

from app import __version__


def create_app() -> FastAPI:
    app = FastAPI(
        title="Gateway_prepagos",
        version=__version__,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )

    @app.get("/health", include_in_schema=False)
    def health() -> dict[str, str]:
        return {"status": "ok"}

    return app


app = create_app()
