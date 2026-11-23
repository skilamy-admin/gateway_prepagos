# Registro de decisiones — Gateway_prepagos

Desvíos del PRD y decisiones técnicas importantes, con su motivo. La más reciente arriba.

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

Pregunta abierta para Skilamy (afecta la fase 2, no bloquea): ¿en qué moneda van los topes `daily_cap` y `monthly_cap`? RF7 dice «moneda local» y la tabla de decisiones dice USD sobre `invoiceAmount`. Las columnas no fijan moneda; se decide al implementar los topes.

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
