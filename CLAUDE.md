# CLAUDE.md — Gateway_prepagos

Servidor de Skilamy que centraliza las recargas móviles de sus productos (bot de WhatsApp, LMS LearnDash…) hacia la API Valuetopup v2 de Prepaynation. Mueve dinero real: ante la duda, detente y pregunta.

Especificación: PRD y brief técnico de fase 1 (enlaces en `README.md`). Si algo de este archivo contradice el PRD, manda el PRD; anota la discrepancia en `docs/decisiones.md`.

## Stack

- Python 3.13 (el del sistema Debian), FastAPI, Pydantic v2, SQLAlchemy 2 + Alembic, PostgreSQL 17 (paquete de Debian)
- httpx (cliente Prepaynation), phonenumbers, cryptography (Fernet), Typer (CLI)
- pytest, ruff, mypy
- Debian 13 (trixie), Nginx + Let's Encrypt, dos servicios systemd: `gateway-api` y `gateway-worker`
- uv usa solo el Python del sistema (`UV_PYTHON_PREFERENCE=only-system`, `.python-version` = 3.13): los parches de seguridad llegan por apt. No descargar otros intérpretes con `uv python install`.

## Comandos

```bash
uv sync                          # instalar dependencias
uv run ruff check . && uv run mypy app
uv run pytest tests/unit tests/integration
uv run pytest tests/sandbox -m sandbox   # contra el sandbox real, solo a mano
uv run alembic upgrade head
uv run gateway --help            # CLI de administración
```

## Estructura

- `app/api/` — API v1 para productos (FastAPI, escucha solo en 127.0.0.1)
- `app/domain/` — validación de teléfonos, recompensas → SKU, transiciones de estado
- `app/prepaynation/` — cliente HTTP y sincronización del catálogo
- `app/worker/` — ejecución (`execute.py`) y reconciliación (`reconcile.py`)
- `app/cli.py` — administración: productos, llaves, recompensas, credenciales, transacciones
- `deploy/` — unidades systemd, config de Nginx, `env.example`

## Reglas de Prepaynation (no negociables)

1. **Éxito = HTTP 200 y `responseCode == "000"`.** Cualquier otro código es fallo. El campo de datos se llama `payLoad`.
2. **Cada transacción lleva un `correlationId` único** (1–50 caracteres) generado por el gateway y guardado antes de llamar.
3. **Nunca reenviar un `topup`** después de un timeout o error de red. Consultar `GET /transaction/status/{correlationId}`; `852` = en curso, seguir consultando.
4. **Timeout de `topup`: 130 s.** Prepaynation puede tardar hasta 2 minutos.
5. **Enviar siempre `transactionCurrencyCode`** con la moneda local (CRC para Costa Rica). Sin él, el monto se interpreta en la moneda del wallet (USD) y no se valida contra las denominaciones del SKU.
6. **No fijar `skuId` en el código.** Salen del catálogo cacheado (`/catalog/getproducts?categoryId=2`, `/catalog/skus`), refrescado cada 24 h.
7. El saldo de `/account/balance` viene con **signo negativo** en cuentas prepagas (−50 = 50 disponibles).
8. URLs: sandbox `https://sandbox.valuetopup.com/api/v2`, producción `https://www.valuetopup.com/api/v2`. Credenciales distintas por entorno; nunca mezclarlas.

## Reglas del gateway

- **Idempotencia por base de datos:** restricción única sobre (`product_id`, `external_ref`) y sobre `correlation_id`. No confiar en comprobaciones en código.
- **Máquina de estados:** `PENDING → SUBMITTED → SUCCESS | FAILED | UNKNOWN`, y `UNKNOWN → SUCCESS | FAILED` solo vía `/transaction/status`. Pasar a `SUBMITTED` y hacer commit **antes** de llamar a `topup`. Los estados finales no cambian nunca.
- La cola es la tabla `topups` con `SELECT … FOR UPDATE SKIP LOCKED`. Sin Redis.
- Cada llamada a Prepaynation se registra en `topup_events` (request/response sin credenciales, latencia).
- El entorno lo decide la llave del producto: `sk_test_…` → sandbox, `sk_live_…` → producción.
- El producto pide recompensas por `reward_code`; nunca acepta montos desde la API.
- Pruebas en sandbox con un producto de país Jamaica (+1876): el sandbox no hace lookup de Costa Rica.

## Seguridad

- Llaves de producto: solo hash SHA-256 + prefijo visible en base; comparación en tiempo constante.
- Credenciales de Prepaynation cifradas con Fernet; llave maestra en `GATEWAY_MASTER_KEY` (`/etc/gateway_prepagos/env`, `0600`).
- Ningún secreto en el repositorio, logs, excepciones, tests ni respuestas de la API. Usar `env.example` sin valores.
- **No usar ni pedir credenciales de producción.** Las carga Skilamy. Todo el desarrollo y las pruebas van contra el sandbox.

## Forma de trabajo

- Seguir el plan del brief paso a paso; un paso por rama, tests en verde antes de merge a `main`.
- Commits pequeños con mensaje en español que diga el porqué.
- Toda duda sobre la API de Prepaynation se resuelve probando en sandbox o consultando la documentación, nunca suponiendo.
- Desvíos del PRD: anotarlos en `docs/decisiones.md` y avisar antes de seguir.
- No tocar otros servidores de Skilamy ni instalar servicios fuera de lo listado sin aprobación.
