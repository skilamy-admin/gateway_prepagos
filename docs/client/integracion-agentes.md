# Integración con Gateway_prepagos — guía para agentes

Documento para un agente (Claude u otro) que va a conectar un servicio de Skilamy (bot de
WhatsApp, LMS LearnDash, etc.) al gateway de recargas móviles. Describe el contrato de la
API v1 tal como está implementado. Si algo aquí contradice lo que responde el gateway,
manda el gateway: avisa a Skilamy de la diferencia.

> Versión del contrato: API v1, 2026-11-24. Fuente: rama `paso-6-api-v1` del repositorio
> `skilamy-admin/gateway_prepagos` (`app/api/`).

## 0. Estado actual del gateway (leer primero)

| Pieza | Estado |
|---|---|
| API v1 (`/v1/topups`, `/v1/rewards`) | Implementada y probada. |
| Ejecución de recargas (worker) | **En desarrollo.** Hoy las recargas quedan en `PENDING`. |
| Dominio público `https://recargas.skilamy.com` | **Aún no desplegado** (Nginx + HTTPS llega en el paso 8). |
| Webhooks de resultado | **No implementados** (fase 2). Hoy el resultado se obtiene consultando. |
| Entorno de producción (`sk_live_`) | No habilitado. Todo el desarrollo va contra `sk_test_` (sandbox). |

Diseña la integración para que funcione **solo con consultas** (polling) y deja el
receptor de webhook preparado pero opcional (ver §8).

## 1. Qué hace el gateway

El servicio cliente pide una **recompensa** por código (p. ej. `CURSO_FINALIZADO`) para un
teléfono. El gateway decide el monto, valida el número, aplica topes y ejecuta la recarga
con Prepaynation de forma asíncrona. El cliente **nunca envía montos**.

## 2. Configuración que necesita el cliente

| Variable sugerida | Ejemplo | De dónde sale |
|---|---|---|
| `GATEWAY_BASE_URL` | `https://recargas.skilamy.com` | Skilamy. Sin barra final. |
| `GATEWAY_API_KEY` | `sk_test_…` (40 caracteres) | Skilamy la genera con la CLI del gateway y la entrega **una sola vez**. |
| `GATEWAY_REWARD_CODES` | `CURSO_FINALIZADO` | Códigos acordados con Skilamy; se pueden listar con `GET /v1/rewards`. |
| `GATEWAY_WEBHOOK_SECRET` | — | Futuro (fase 2). Dejar la variable prevista. |
| IP pública de salida del cliente | `203.0.113.10` | El cliente se la informa a Skilamy si el producto usa lista de IPs. |

Reglas:

- La llave es un secreto: solo del lado servidor (variable de entorno, `wp-config.php`,
  gestor de secretos). Nunca en el navegador, en el repositorio, en logs ni en mensajes.
- `sk_test_…` → sandbox (sin dinero real). `sk_live_…` → producción (dinero real). El
  entorno lo decide la llave; no hay otro parámetro.
- Cada producto tiene **un país**. El teléfono debe ser de ese país.
  - Producto real: Costa Rica (+506).
  - Producto de pruebas en sandbox: Jamaica (+1876), porque el sandbox de Prepaynation no
    procesa Costa Rica.

## 3. Autenticación y transporte

- Cabecera en todas las llamadas: `Authorization: Bearer <GATEWAY_API_KEY>`.
- Cuerpo y respuestas en JSON (`Content-Type: application/json`), UTF-8.
- Solo HTTPS.
- Timeout recomendado del cliente: 15 s. `POST /v1/topups` responde rápido: no espera a
  Prepaynation.
- Límite de tasa por producto (por defecto 60 solicitudes/minuto, sumando todas sus
  llaves). Al superarlo: `429 rate_limited` con cabecera `Retry-After` en segundos.

## 4. Endpoints

### 4.1 `POST /v1/topups` — pedir una recompensa

Solicitud:

```json
{
  "reward_code": "CURSO_FINALIZADO",
  "phone": "+50688887777",
  "external_ref": "user_123:curso_7",
  "operator": "Kolbi"
}
```

| Campo | Obligatorio | Reglas |
|---|---|---|
| `reward_code` | sí | 1–50 caracteres. Formato `[A-Z0-9_]`. Debe existir y estar activo para el producto. |
| `phone` | sí | Hasta 32 caracteres. Preferir E.164 (`+506…`); también acepta formato nacional del país del producto. Debe ser **móvil** y del país del producto. |
| `external_ref` | sí | 1–200 caracteres. Clave de idempotencia (ver §5). |
| `operator` | no | Nombre del operador (Kolbi, Claro, Liberty…), sin distinguir mayúsculas. Si no coincide con el catálogo: `422 unknown_operator`, y el mensaje lista los válidos. |

Cualquier otro campo (por ejemplo `amount`) se rechaza con `422 invalid_request`.

Respuesta:

- `202 Accepted`: recarga nueva creada.
- `200 OK`: ya existía una recarga con esa `external_ref` en ese entorno; se devuelve la
  misma, **sin crear otra**.

En ambos casos el cuerpo es un objeto Topup (§4.4).

### 4.2 `GET /v1/topups/{id}` — consultar por id

`id` es el que devolvió el POST (`tp_` + 26 caracteres). Responde un objeto Topup.

### 4.3 `GET /v1/topups?external_ref=<ref>` — consultar por referencia propia

Codifica la referencia en la URL (`user_123%3Acurso_7`). Responde un objeto Topup.

Un producto solo ve sus propias recargas y solo las del entorno de su llave. Si no existe,
la respuesta es `404 not_found`.

### 4.4 Objeto Topup

```json
{
  "id": "tp_01JDXXXXXXXXXXXXXXXXXXXXXX",
  "external_ref": "user_123:curso_7",
  "status": "PENDING",
  "env": "test",
  "reward_code": "CURSO_FINALIZADO",
  "amount": 1000.0,
  "currency": "CRC",
  "phone": "+50688887777",
  "error_code": null,
  "created_at": "2026-12-01T15:03:58.123456Z",
  "finalized_at": null
}
```

| Campo | Significado |
|---|---|
| `id` | Id del gateway. Guárdalo. |
| `status` | `PENDING`, `SUBMITTED`, `UNKNOWN`, `SUCCESS` o `FAILED` (ver §6). |
| `env` | `test` (sandbox) o `live` (producción). |
| `amount`, `currency` | Monto en moneda local que fija la recompensa. `amount` es número JSON. |
| `phone` | Número normalizado a E.164. |
| `error_code` | Código de Prepaynation, solo si `status == "FAILED"`; si no, `null`. |
| `created_at`, `finalized_at` | ISO 8601. `finalized_at` solo en estados finales. |

### 4.5 `GET /v1/rewards` — recompensas activas del producto

```json
[
  {"code": "CURSO_FINALIZADO", "label": "Curso finalizado", "amount": 1000.0, "currency": "CRC"}
]
```

### 4.6 `GET /health`

Sin autenticación. `{"status": "ok"}` si la API está arriba. No comprueba Prepaynation.

## 5. Idempotencia: `external_ref`

- Genera una `external_ref` **estable y determinista por recompensa ganada**, por ejemplo
  `user_123:curso_7` o `wa:50688887777:promo_dic`. Nunca uses un valor aleatorio ni la
  hora.
- Reenviar el mismo POST con la misma `external_ref` es **seguro**: devuelve la misma
  recarga (200) y nunca paga dos veces, aunque las solicitudes sean simultáneas.
- Por eso, ante un timeout, un error de red, `429` o `5xx`, **reintenta el mismo POST con la
  misma `external_ref`** (con espera creciente).
- La idempotencia es por producto y entorno: la misma referencia en `test` y en `live` son
  recargas distintas.
- Si la recarga ya existía, se devuelve aunque ahora cambien el teléfono o la recompensa del
  cuerpo. Una `external_ref` identifica una sola recompensa.

## 6. Estados y cómo obtener el resultado

```
PENDING → SUBMITTED → SUCCESS | FAILED | UNKNOWN
UNKNOWN → SUCCESS | FAILED
```

| Estado | Significado | Qué hace el cliente |
|---|---|---|
| `PENDING` | Aceptada, en cola. | Seguir consultando. |
| `SUBMITTED` | Enviada a Prepaynation (puede tardar hasta ~2 min). | Seguir consultando. |
| `UNKNOWN` | Timeout o en curso; el gateway lo está confirmando. **Puede haberse cobrado.** | Seguir consultando, con menos frecuencia. **No crear otra recarga.** |
| `SUCCESS` | Recarga entregada. **Final.** | Avisar al usuario. |
| `FAILED` | Rechazada (número inválido, sin stock…). **Final.** Ver `error_code`. | Avisar al usuario; decidir con Skilamy si se ofrece otra recompensa (con **otra** `external_ref`). |

Los estados finales no cambian nunca.

Consulta sugerida (hasta que existan webhooks):

- cada 10 s durante los primeros 3 min;
- luego cada 1 min hasta 30 min;
- luego cada 15 min.

Si sigue en `UNKNOWN` después de 24 h, escala a Skilamy con `id` y `external_ref`.

Los rechazos de Prepaynation **no** llegan en la respuesta del POST: llegan después como
`FAILED` con `error_code` (p. ej. `501` número inválido, `510` sin stock).

## 7. Errores

Formato único:

```json
{"error": "<código>", "message": "<texto legible en español>"}
```

| HTTP | `error` | Cuándo | ¿Reintentar? |
|---|---|---|---|
| 401 | `invalid_key` | Llave ausente, mal formada, inexistente o revocada. | No. Revisar configuración. |
| 403 | `product_suspended` | Producto suspendido, o llave `sk_live_` con el producto aún en test. | No. Contactar a Skilamy. |
| 403 | `ip_not_allowed` | La IP de salida no está en la lista del producto. | No. Informar la IP a Skilamy. |
| 404 | `not_found` | Recarga inexistente, de otro producto o de otro entorno; o ruta inexistente. | No. |
| 405 | `method_not_allowed` | Método HTTP incorrecto. | No. |
| 422 | `invalid_request` | JSON inválido, falta un campo, campo de más (p. ej. `amount`) o largo fuera de rango. El mensaje nombra los campos. | No. Corregir la solicitud. |
| 422 | `unknown_reward` | `reward_code` no existe o está inactivo. | No. |
| 422 | `invalid_phone` | Formato inválido, otro país o no es móvil. | No. Pedir el número al usuario de nuevo. |
| 422 | `unknown_operator` | `operator` no coincide con el catálogo del país. | No. Usar uno de los listados en el mensaje. |
| 429 | `rate_limited` | Más solicitudes por minuto que las permitidas. | Sí, tras `Retry-After`, con la misma `external_ref`. |
| 429 | `phone_limit_reached` | El número llegó al máximo de recargas en la ventana del producto. | No (no se libera reintentando de inmediato). Informar al usuario. |
| 429 | `budget_exceeded` | Se alcanzó el tope diario o mensual del producto. | No automático. Avisar a Skilamy; reintentar más tarde (otro día/mes). |
| 500 | `internal_error` | Error del gateway. | Sí, con espera creciente y la misma `external_ref`. |
| 502/503/504 | (respuesta de Nginx, puede no ser JSON) | Gateway caído o reiniciando. | Sí, con espera creciente y la misma `external_ref`. |

Distingue los 429 por el campo `error`, no por el código HTTP.

## 8. Webhooks (fase 2, todavía no disponibles)

Diseño previsto en el PRD, sujeto a cambios:

- El gateway hará `POST` a la `webhook_url` del producto cuando una recarga llegue a
  estado final.
- Cuerpo:
  ```json
  {"id": "tp_01JD…", "external_ref": "user_123:curso_7", "status": "SUCCESS",
   "error_code": null, "finalized_at": "2026-12-01T15:04:05Z"}
  ```
- Firma HMAC-SHA256 del cuerpo con el secreto del producto, en la cabecera
  `X-Gateway-Signature`.
- El cliente debe:
  - verificar la firma en tiempo constante;
  - tratar el webhook como aviso y, ante duda, confirmar con `GET /v1/topups/{id}`;
  - ser idempotente (el mismo webhook puede llegar más de una vez).

El esquema exacto de la firma se documentará al implementarlo. Hasta entonces, el polling
de §6 es la fuente de verdad.

## 9. Pruebas en sandbox (`sk_test_`)

- Usa el producto de pruebas que te asigne Skilamy (país Jamaica, números `+1876XXXXXXX`).
- Prepaynation simula escenarios según cómo **termina** el número:
  - `666666`: timeout de 120 s (verás `UNKNOWN`);
  - `555555`: timeout de 90 s;
  - `510`: sin stock (`FAILED`, `error_code` `510`);
  - termina en `888`: denominación inválida;
  - `500`: cuenta no activada.
- Prueba al menos:
  - creación y consulta hasta estado final;
  - reintento con la misma `external_ref` (debe dar 200 y el mismo `id`);
  - `invalid_phone`, `unknown_reward`, `invalid_request` con `amount`;
  - manejo de `UNKNOWN` y de `429`.

## 10. Lista de verificación para el agente que integra

1. Llave en una variable de entorno del servidor; verificar que no aparece en logs ni en
   el repositorio.
2. Teléfono capturado y guardado en E.164; operador elegido por el usuario y enviado en
   `operator`.
3. `external_ref` determinista por recompensa, guardada en la base del cliente **antes**
   de llamar al gateway.
4. Guardar `id` y `status` de la respuesta.
5. Reintentos solo para red, `429 rate_limited` y `5xx`, siempre con la misma
   `external_ref`.
6. Proceso de consulta periódica hasta `SUCCESS` o `FAILED`; `UNKNOWN` nunca se trata como
   fallo.
7. Mensajes al usuario a partir del estado final, nunca del `202`.
8. Ningún monto en el cliente: el monto lo fija la recompensa en el gateway.

## Ejemplo mínimo (Python, httpx)

```python
import os, time, httpx

BASE = os.environ["GATEWAY_BASE_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['GATEWAY_API_KEY']}"}
FINALES = {"SUCCESS", "FAILED"}

def pedir_recarga(reward_code: str, phone: str, external_ref: str, operator: str | None = None) -> dict:
    cuerpo = {"reward_code": reward_code, "phone": phone, "external_ref": external_ref}
    if operator:
        cuerpo["operator"] = operator
    for intento in range(5):
        try:
            r = httpx.post(f"{BASE}/v1/topups", json=cuerpo, headers=HEADERS, timeout=15)
        except httpx.TransportError:
            time.sleep(2 ** intento)
            continue
        if r.status_code in (200, 202):
            return r.json()
        error = r.json().get("error") if r.headers.get("content-type", "").startswith("application/json") else None
        if r.status_code >= 500 or error == "rate_limited":
            time.sleep(int(r.headers.get("Retry-After", 2 ** intento)))
            continue
        raise ValueError(f"{r.status_code} {error}: {r.text}")  # no reintentar
    raise RuntimeError("gateway no disponible; reintentar más tarde con la misma external_ref")

def consultar(topup_id: str) -> dict:
    r = httpx.get(f"{BASE}/v1/topups/{topup_id}", headers=HEADERS, timeout=15)
    r.raise_for_status()
    return r.json()
```
