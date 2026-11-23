# Registro de decisiones — Gateway_prepagos

Desvíos del PRD y decisiones técnicas importantes, con su motivo. La más reciente arriba.

## 2026-11-23 — API v1 (paso 6)

- **Formato de error:** `{"error": "<código>", "message": "<texto>"}`. Los códigos son los del PRD; nunca se incluyen trazas ni detalles internos.
- **Códigos nuevos** (desvío, para avisar a Skilamy):
  - `invalid_request` (422): JSON mal formado, falta un campo, o llega un campo no permitido (por ejemplo `amount`: el producto nunca envía montos).
  - `unknown_operator` (422): el campo `operator` no coincide con ningún operador del catálogo del país. El mensaje lista los válidos.
  - `external_ref_conflict` (409): la `external_ref` ya se usó con una llave del **otro entorno**. Ver la pregunta abierta más abajo.
  - `not_found` (404): la recarga no existe, es de otro producto o es del otro entorno.
- **Orden de validación:** la idempotencia va primero, antes de validar recompensa y teléfono. El brief pone primero las validaciones, pero así un reintento devuelve la misma transacción aunque la recompensa se haya desactivado después.
- **Creación idempotente:** `INSERT … ON CONFLICT (product_id, external_ref) DO NOTHING RETURNING id`. Si otra solicitud ganó la carrera, se devuelve esa con 200. Probado con 10 solicitudes simultáneas por HTTP: una fila, un 202 y nueve 200.
- **Teléfono:** se acepta en formato internacional o nacional del país. Debe ser válido para la región, empezar por el `dial_prefix` del país (+1876 excluye el 658 de Jamaica) y ser móvil según phonenumbers.
- **`operator`:** es el *nombre* del operador (Kolbi, Claro, Liberty, como dicen las notas del PRD). Se resuelve al `operatorId` de Prepaynation con el catálogo del entorno de la llave, sin distinguir mayúsculas. Se guarda con `operator_source = user`.
- **Visibilidad:** un producto solo ve sus recargas y solo las del entorno de su llave (separación estricta test/live).
- **Respuesta de consulta:**
  - campos: `id`, `external_ref`, `status`, `env`, `reward_code`, `amount` (número JSON), `currency`, `phone`, `error_code`, `created_at` y `finalized_at`;
  - `error_code` es el código de Prepaynation y solo aparece si la recarga terminó en FAILED.
- **No incluido en esta fase** (PRD, no está en el brief de fase 1): rate limiting por llave y lista de IPs permitidas por producto (la columna `allowed_ips` ya existe).

**Pregunta abierta para Skilamy:** la única es `(product_id, external_ref)`, como dicen el PRD y el CLAUDE.md, así que test y live comparten el espacio de `external_ref`. Si un producto prueba con `user_123:curso_7` en sandbox y después pide lo mismo en producción, hoy recibe 409 `external_ref_conflict` en lugar de pagar. Así no se paga dos veces ni se pierde la recompensa en silencio, pero obliga a los productos a usar referencias distintas en test. Alternativa: hacer la única `(product_id, env, external_ref)`. Requiere aprobación porque cambia una regla de idempotencia.

## 2026-11-23 — Cliente de Prepaynation y catálogo (paso 5)

**Fuente:** especificación OpenAPI oficial (`/redocusaurus/api-v2.yaml`, enlazada desde el portal de desarrolladores de Valuetopup) y guías del portal (Transaction Flow, Authentication, Test Simulation).

- **Campo `correlationId`:** la guía «Transaction Flow» lo escribe `corelationId`, pero la especificación usa `correlationId` (1 a 50 caracteres). Se usa el de la especificación.
- **Tipos:** `skuId`, `productId`, `operatorId` y `transactionId` son enteros en la API. En la base se guardan como texto, como se decidió en el paso 3.
- **`mobile`:** se envía con código de país y sin «+» (p. ej. `18765550123`), según el ejemplo del sandbox.
- **Montos:** las respuestas se leen como `Decimal`, nunca como `float`. Al enviar, se verifica que el monto se pueda representar como número JSON sin perder precisión.
- **El cliente nunca reintenta.** Un timeout o error de red lanza `PnTransportError`, con un indicador de si la petición pudo llegar. La decisión (UNKNOWN y consultar `/transaction/status`) es del worker.
- **Registro de llamadas:** cada llamada se guarda en `topup_events`, en su propia transacción, para que quede aunque la operación falle. Se guardan método, ruta, parámetros, cuerpo y respuesta, nunca cabeceras ni credenciales.
- **`pn_catalog`** se ajusta con una segunda migración, según el modelo `SkuModel` de la API:
  - el rango de cada SKU (`min`/`max`) en tres monedas: face value (`faceValueCurrency`), entregado (`deliveryCurrencyCode`) y wallet;
  - además, `exchangeRate`, `benefitType`, `validity`, `allowDecimal`, `fee` y `deliveryAmountType`;
  - se elimina `denominations`: un SKU fijo tiene mínimo = máximo.
- **Sincronización:** descarga `getproducts?categoryId=2`, `skus` y `errors`, y reemplaza la caché del entorno en una sola transacción. Si algo falla o el catálogo llega vacío, la caché anterior queda intacta. Solo se guardan SKUs de productos RTR.
- **`reward add` valida el monto (RF3)** contra el rango *entregado* de los SKUs de saldo (Talktime) de cada operador del país. Usa el catálogo del entorno del producto (test → sandbox; live/suspended → producción).
  - Si ningún operador cubre el monto, se rechaza.
  - Si falta alguno, se rechaza salvo con `--allow-partial`.
  - Si no hay catálogo, avisa.
- **Pendiente de verificar en sandbox** antes de implementar el worker (paso 7): la especificación dice que `transactionCurrencyCode` debe ser una moneda que aparezca como `FaceValueCurrency` en `/catalog/skus`. Si para Costa Rica el face value está en USD, quizás no se pueda enviar `CRC`. Se prueba con Jamaica (JMD) en el sandbox y, si hace falta, se pregunta a support@prepaynation.com.
- **Escenarios de prueba del sandbox** (guía «Test Simulation»), según cómo termina el número:
  - `666666`: timeout de 120 s;
  - `555555`: timeout de 90 s;
  - `510`: sin stock;
  - empieza por `1876` y termina en `888`: denominación inválida;
  - `500`: cuenta no activada.
- **Bloqueo (2026-11-23):** el sandbox responde HTTP 401 a todas las llamadas con las credenciales cargadas (sin espacios ni caracteres extraños). Falta la prueba manual de `balance` y `catalog sync` que pide el brief. Se pidió a Skilamy verificar las credenciales.

## 2026-11-23 — Seguridad y CLI de configuración (paso 4)

- La configuración se lee de `/etc/gateway_prepagos/env` (pydantic-settings) además del entorno, para que la CLI funcione sin exportar variables. Las variables de entorno tienen prioridad; los tests usan su propia llave maestra.
- Llaves de producto: `sk_test_` / `sk_live_` + 32 caracteres alfanuméricos de `secrets`. En base solo quedan el SHA-256 y los primeros 12 caracteres. La comparación usa `hmac.compare_digest`.
- `credentials set --env sandbox|production` pide el password sin mostrarlo y con confirmación, y lo cifra con Fernet antes de tocar la base. Con `production` exige una confirmación adicional; en esta fase no se usa.
- Paso a `live` con `product set-status` pide confirmación.
- Toda acción de la CLI que cambia datos se registra en `audit_log` (actor `cli:<usuario>`), sin llaves ni passwords.
- Pendiente para el paso 5: `reward add` todavía no valida el monto contra los rangos del catálogo de cada operador (RF3), porque el catálogo llega en ese paso. Hoy solo valida formato, monto positivo y moneda del país.

## 2026-11-23 — Modelo de datos (paso 3)

Cambios y agregados respecto a la tabla «Modelo de datos inicial» del PRD:

- **Tabla nueva `pn_error_codes`** (env, code, message): el brief pide sincronizar `/catalog/errors`, que no cabe en `pn_catalog`. Son 11 tablas en lugar de 10.
- **`topups.next_check_at`** (nuevo): próxima consulta a `/transaction/status`, para la espera creciente de la reconciliación (30 s, 1 min, 2 min, 5 min, cada 15 min). `attempts` cuenta esas consultas.
- **`topup_events`**: `topup_id` admite NULL para registrar también las llamadas que no son de una recarga (balance, catálogo), como pide el brief («cada llamada se registra en topup_events»). Se agregan `env` y `http_status`.
- **`products.webhook_secret` → `webhook_secret_enc`**: el secreto HMAC se guarda cifrado con Fernet, igual que las credenciales.
- **`pn_catalog.product_id` → `pn_product_id`**, para no confundirlo con nuestros productos. Se agregan `product_name`, `operator_name` y `raw` (respuesta original, por si el formato del catálogo trae algo que no se modeló).
- **Un solo vocabulario de entorno: `test` / `live`** en llaves, recargas, catálogo y settings (test = sandbox, live = producción). La CLI acepta `--env sandbox|production` y lo traduce.
- **`settings`** es una fila por entorno: `pn_user_id`, `pn_password_enc` (Fernet), `wallet_currency`, `low_balance_threshold` (valor absoluto).
- **`operator_source`** con valores `lookup` / `user` (el PRD dice «lookup/usuario»).
- **Identificadores de Prepaynation** (`sku_id`, `operator_id`, `transactionId`…) como texto, para no depender de su tipo.
- **`external_ref`** de hasta 200 caracteres; el PRD no fija un largo.
- **ID de recarga** `tp_` + ULID (29 caracteres) y `correlation_id` = ULID (26 caracteres). El ULID se implementa en `app/ids.py` para no sumar una dependencia.
- **Enumeraciones** como VARCHAR + CHECK, no ENUM nativo de PostgreSQL, porque son más fáciles de migrar.

Reglas que garantiza la base, además de las únicas (product_id, external_ref) y correlation_id:

- **Trigger `topups_guard`**:
  - toda recarga nueva entra en PENDING;
  - solo se permiten PENDING → SUBMITTED | FAILED, SUBMITTED → SUCCESS | FAILED | UNKNOWN y UNKNOWN → SUCCESS | FAILED (PENDING → FAILED es el rechazo antes del envío);
  - una fila en estado final no se modifica;
  - identidad, monto, moneda, teléfono, external_ref y correlation_id no cambian nunca, y SKU, operador y submitted_at no cambian después del envío.
- **CHECK**:
  - SUCCESS exige `pn_response_code = '000'`;
  - los estados finales exigen `finalized_at`, y lo que pasó por SUBMITTED exige `submitted_at`;
  - teléfono en E.164, montos positivos y prefijo de llave coherente con su entorno.

Pregunta abierta para Skilamy (afecta la fase 2, no bloquea): ¿en qué moneda van los topes `daily_cap` y `monthly_cap`? RF7 dice «moneda local» y la tabla de decisiones dice USD sobre `invoiceAmount`. Las columnas no fijan moneda; se decide al implementar los topes. Recomendación: USD sobre `invoice_amount`, porque es el dinero que sale del wallet.

**Catálogo de Prepaynation (`docs/Product_Catalog.xlsx`, revisado el 2026-11-23).** Es el único catálogo disponible; falta confirmar si el del sandbox es el mismo.

- 8 843 SKUs y wallet siempre en USD. Columnas: Product Id, Sku Id, Country, Operator, Denomination Name, Wallet currency, Face Value (Wallet currency), Target Currency, Type, Benefit Type, Exchange Rate, validity, Delivery Amount, Product Description.
- **Corrección:** las recargas de saldo de Costa Rica (Type RTR, Benefit Type Talktime) **no son de monto fijo: son de rango**, un SKU por operador. El rango se define en USD, y el monto en CRC es USD × tipo de cambio:

  | Operador | SKU | USD | CRC | Tipo de cambio |
  |---|---|---|---|---|
  | Kolbi | 3390 | 3–50 | 1 512–25 200 | 504,00 |
  | Claro | 1886 | 7–35 | 3 474,73–17 373,65 | 496,39 |
  | Liberty | 5752 | 5–80 | 2 690,25–43 044 | 538,05 |
  | Movistar | 2374 | 4–85 | 2 104–44 710 | 526,00 |

- Los SKUs de monto fijo de Costa Rica son paquetes de Claro (Bundle «Superpack» y Data «Internet Super pack», con vigencia de 7 a 30 días). No son recargas de saldo.
- Consecuencias para el diseño:
  - Una recompensa en CRC es válida para un operador si cae dentro de su rango en CRC. Hoy el mínimo común a los cuatro operadores es el de Claro: unos 3 475 CRC. Una recompensa de 1 000 CRC no sirve para ningún operador.
  - El tipo de cambio cambia, y con él los límites en CRC. Hay que validar contra el catálogo vigente al crear la recompensa (RF3) y otra vez antes de cada recarga.
  - El costo en USD de una misma recompensa depende del operador (cada uno tiene su tipo de cambio). Afecta los topes y los reportes.
  - Movistar ya no opera en Costa Rica (lo absorbió Liberty), pero sigue en el catálogo. Hay que preguntar a Prepaynation si el SKU 2374 sigue activo.
- Jamaica (pruebas en sandbox): Digicel tiene SKUs de rango (193, 2639, 14197) y Flow tiene SKUs de monto fijo (4056 a 4075, de 5 a 100 USD). Sirve para probar ambos tipos, si el sandbox usa el mismo catálogo.
- En el paso 5 se ajusta `pn_catalog` con una migración, después de ver la respuesta real de `/catalog/skus` en el sandbox: rango en USD y en moneda local, tipo de cambio, tipo de beneficio y vigencia. Los nombres de las columnas de la hoja pueden no coincidir con los campos JSON de la API.

Tests de integración: usan un esquema temporal `test_<aleatorio>` dentro de `gateway_prepagos`, que se crea y se borra en cada ejecución. No hace falta otra base ni superusuario.

## 2026-11-23 — Esqueleto del proyecto (paso 0)

- Configuración con `pydantic-settings` (variables `GATEWAY_*`), dependencia oficial de Pydantic v2; no está en el stack listado pero no agrega servicios.
- Driver de PostgreSQL: `psycopg` 3. Conexión por socket local con autenticación peer (`postgresql+psycopg:///gateway_prepagos`), sin contraseña en la URL.
- La API arranca con `gateway serve` (uvicorn en 127.0.0.1) y sin `/docs` ni `/openapi.json` públicos.
- Repositorio git inicializado en local, sin remoto todavía.
- Migraciones en `migrations/` (como propone el brief), configuradas desde `app.config`.

## 2026-11-23 — Diferencias entre el servidor real y el brief

- Ubicación del código (no es desvío): `/home/gateway/gateway_prepagos` es la copia de desarrollo (pasos 2 a 7). En el paso 8 se despliega en `/opt/gateway_prepagos`, como dice el brief, clonando desde el repositorio: dueño root y solo lectura para el usuario `gateway`, para que el servicio no pueda modificar su propio código, no ejecute código a medio editar y las unidades systemd puedan usar `ProtectHome=yes`. Crear `/opt/gateway_prepagos` requiere sudo (lo hace quien administre el VPS).
- Base de datos: se usa la existente, `gateway_prepagos` (dueño `gateway`, sin contraseña, por socket local); el brief dice base `gateway`, que no existe en el servidor. Confirmado con Skilamy el 2026-11-23.
- Desvío: `/etc/gateway_prepagos/env` con dueño `root:gateway` y permisos `0640` (carpeta `0750`) en lugar de dueño `gateway` y `0600`, para que el servicio pueda leer su configuración pero no modificarla. Aprobado por Skilamy el 2026-11-23. La crea quien tenga sudo; la llave maestra se genera en el servidor y va directo al archivo.
- Paso 1 del brief, verificado el 2026-11-23 sin sudo:
  - Nginx activo y habilitado al arrancar; `recargas.skilamy.com` resuelve en DNS público a la IP del VPS (95.179.222.249).
  - Certificado de Let's Encrypt válido hasta 2027-02-21, renovación automática con `certbot.timer`, TLS 1.2 y 1.3. El puerto 80 solo redirige a HTTPS.
  - ufw (`sudo ufw status verbose`, 2026-11-23): entrada denegada por defecto; 22/tcp en LIMIT (frena fuerza bruta) y 80/tcp y 443/tcp permitidos, en IPv4 e IPv6. PostgreSQL escucha solo en localhost.
  - Desvío: el brief pide solo 22 y 443; el 80 queda abierto porque certbot lo usa para renovar con el desafío HTTP-01, y Nginx solo redirige a HTTPS desde ahí.
  - El sitio de Nginx todavía sirve `/var/www/html`; en el paso 8 se cambia por el proxy hacia la API (`deploy/nginx-recargas.conf`).

## 2026-11-23 — Versiones de la plataforma

- Debian 13 en lugar de Debian 12: el servidor de Vultr se instaló con trixie.
- PostgreSQL 17 en lugar de 15: es la versión de Debian 13 y recibe parches de seguridad por apt.
- Python 3.13 del sistema en lugar de 3.12: los parches de seguridad llegan por unattended-upgrades; uv configurado con UV_PYTHON_PREFERENCE=only-system.
