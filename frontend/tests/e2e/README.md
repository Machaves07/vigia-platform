# Mundo local de U-05

Desde la raíz: `make frontend-e2e`. Alternativamente, después de `npm ci` y
`npx playwright install --with-deps chromium firefox webkit` en `frontend/`:

```sh
cd backend && uv sync --frozen
cd ../frontend && npm run build
npm run e2e -- tests/e2e/smoke.spec.ts
```

Necesita Docker. En el WSL del proyecto se usa
`DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales`, como las pruebas de integración.
`VIGIA_TEST_USE_COMPOSE=1` reutiliza los servicios previamente levantados con `make up`;
no detiene servicios ajenos. Por omisión cada ejecución posee sus contenedores de
PostgreSQL y LocalStack, y los libera con el mismo arnés de U-04.

`globalSetup` mantiene abierto un canal al mundo Python: migraciones y bootstrap,
API con la raíz de producción y `frontend/dist`, TLS local, worker con el único doble
`FakeAssistantProvider`, y nodos `u04-e2e` que llenan bandeja y cola. El EOF del canal
o una señal liberan el mundo; los fallos del setup también cierran el canal. No se
admite sobrescribir el manifiesto de otra ejecución.

El manifiesto `.world.json` solo contiene URL, identificadores, correos sintéticos
y rutas temporales. Contraseñas aleatorias y semillas TOTP viven fuera del árbol,
en un archivo `0600` dentro de un directorio `0700`; TOTP se almacena cifrado por
KMS de LocalStack en PostgreSQL. Al terminar se eliminan ambos archivos y se
buscan fugas en `frontend/`, `backend/`, registros del mundo y trazas ZIP.

Importa `test` y `expect` de `./fixtures`, no directamente de Playwright. La guarda
de CSP/Trusted Types se instala antes de los guiones, y también observa marcos y
ventanas nuevas. Una violación falla la prueba; no se desactiva la política.

La opción `role` selecciona uno de los seis roles; `authenticatedPage` inicia sesión
por la interfaz y reutiliza el contexto por rol y proyecto. Estos contextos privados
no producen trazas, vídeo ni capturas: un registro de `fill()` expondría contraseña
y OTP. Las páginas sin autenticación sí conservan traza y vídeo solo al fallar.
La ejecución es serial para no competir por un mismo TOTP.

**Bloqueos reproducidos:** VIG-342 corrige la llamada nativa sin receptor de la
sonda H.264 que hoy impide cargar React, incluso en Chromium compatible. VIG-321
debe sustituir el relevo de `/login` antes de validar el acceso por interfaz. El
smoke exige los campos de acceso, no solo un encabezado de relevo. Este arnés no
implementa esas correcciones ni sustituye el acceso por cookies sembradas o rutas
HTTP simuladas. Los recorridos funcionales por rol pertenecen a VIG-332 y VIG-333.
