# Gateway_prepagos

Servidor de Skilamy que centraliza las recargas móviles de sus productos (bot de WhatsApp,
LMS LearnDash…) hacia la API Valuetopup v2 de Prepaynation.

## Especificación

- PRD: [`docs/Gateway_prepagos — PRD.docx`](docs/Gateway_prepagos%20—%20PRD.docx)
- Brief técnico fase 1: [`docs/Gateway_prepagos — Brief técnico fase 1.docx`](docs/Gateway_prepagos%20—%20Brief%20técnico%20fase%201.docx)

Reglas de trabajo y del dominio: ver [`CLAUDE.md`](CLAUDE.md).
Desvíos y decisiones: ver [`docs/decisiones.md`](docs/decisiones.md).

Integración de servicios cliente: [`docs/client/`](docs/client/) — `integracion-agentes.md`
(contrato técnico, para agentes) y `Guía de conexión al gateway.docx` (para personas).

## Desarrollo

```bash
uv sync
uv run ruff check . && uv run mypy app
uv run pytest
uv run alembic upgrade head
uv run gateway --help
```

La configuración se lee de variables de entorno con prefijo `GATEWAY_`
(ver [`deploy/env.example`](deploy/env.example)). En producción viven en
`/etc/gateway_prepagos/env` con permisos `0600`.
