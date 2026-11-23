"""CLI de administración: productos, llaves, recompensas, credenciales, transacciones."""

import typer
import uvicorn

from app import __version__
from app.config import get_settings

app = typer.Typer(help="Administración de Gateway_prepagos", no_args_is_help=True)


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
