# Registro de decisiones — Gateway_prepagos

Desvíos del PRD y decisiones técnicas importantes, con su motivo. La más reciente arriba.

## 2026-11-23 — Esqueleto del proyecto (paso 0)

- Configuración con `pydantic-settings` (variables `GATEWAY_*`), dependencia oficial de Pydantic v2; no está en el stack listado pero no agrega servicios.
- Driver de PostgreSQL: `psycopg` 3. Conexión por socket local con autenticación peer (`postgresql+psycopg:///gateway_prepagos`), sin contraseña en la URL.
- La API arranca con `gateway serve` (uvicorn en 127.0.0.1) y sin `/docs` ni `/openapi.json` públicos.
- Repositorio git inicializado en local, sin remoto todavía.
- Migraciones en `migrations/` (como propone el brief), configuradas desde `app.config`.

## 2026-11-23 — Diferencias entre el servidor real y el brief

- Ubicación del código (no es desvío): `/home/gateway/gateway_prepagos` es la copia de desarrollo (pasos 2 a 7). En el paso 8 se despliega en `/opt/gateway_prepagos`, como dice el brief, clonando desde el repositorio: dueño root y solo lectura para el usuario `gateway`, para que el servicio no pueda modificar su propio código, no ejecute código a medio editar y las unidades systemd puedan usar `ProtectHome=yes`. Crear `/opt/gateway_prepagos` requiere sudo (lo hace quien administre el VPS).
- Base de datos: se usa la existente, `gateway_prepagos` (dueño `gateway`, sin contraseña, por socket local); el brief dice base `gateway`, que no existe en el servidor. Confirmado con Skilamy el 2026-11-23.
- `/etc/gateway_prepagos/env` todavía no existe y crearlo requiere root.
- Paso 1 del brief, verificado el 2026-11-23 sin sudo:
  - Nginx activo y habilitado al arrancar; `recargas.skilamy.com` resuelve en DNS público a la IP del VPS (95.179.222.249).
  - Certificado de Let's Encrypt válido hasta 2027-02-21, renovación automática con `certbot.timer`, TLS 1.2 y 1.3. El puerto 80 solo redirige a HTTPS.
  - ufw activo y habilitado, pero sus reglas no se pueden leer sin root. Puertos que escuchan hacia fuera: 22, 80 y 443; PostgreSQL solo en localhost.
  - Pendiente con sudo: confirmar las reglas con `sudo ufw status verbose`.
  - El brief pide solo 22 y 443, pero se propone dejar abierto el 80: certbot lo necesita para renovar con el desafío HTTP-01 y solo redirige a HTTPS.
  - El sitio de Nginx todavía sirve `/var/www/html`; en el paso 8 se cambia por el proxy hacia la API (`deploy/nginx-recargas.conf`).

## 2026-11-23 — Versiones de la plataforma

- Debian 13 en lugar de Debian 12: el servidor de Vultr se instaló con trixie.
- PostgreSQL 17 en lugar de 15: es la versión de Debian 13 y recibe parches de seguridad por apt.
- Python 3.13 del sistema en lugar de 3.12: los parches de seguridad llegan por unattended-upgrades; uv configurado con UV_PYTHON_PREFERENCE=only-system.
