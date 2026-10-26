# Mundo local de U-05

Desde la raíz: `make frontend-e2e`. Alternativamente, después de `npm ci` y
`npx playwright install --with-deps chromium firefox webkit` en `frontend/`:

```sh
cd backend && uv sync --frozen
cd ../frontend && npm run build
node scripts/install_webkit.mjs
npm run e2e -- tests/e2e/smoke.spec.ts
```

Necesita Docker. En el WSL del proyecto se usa
`DOCKER_CONFIG=/home/manu/vigia/docker-sin-credenciales`, como las pruebas de integración.
`VIGIA_TEST_USE_COMPOSE=1` reutiliza los servicios previamente levantados con `make up`;
no detiene servicios ajenos. Por omisión cada ejecución posee sus contenedores de
PostgreSQL y LocalStack, y los libera con el mismo arnés de U-04.

Chromium corre gráfico con su agente de usuario nativo: necesita una pantalla o Xvfb.
WebKit usa la revisión fijada en VIG-345, instalada por `scripts/install_webkit.mjs`
(también admite `VIGIA_WEBKIT_DIR`); no se relaja CSP ni se filtran sus eventos.

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

Para pruebas autenticadas importa `authenticatedTest as test` de `./fixtures`.
La opción `role` selecciona uno de los seis roles; `authenticatedPage` inicia sesión
por la interfaz y reutiliza el contexto por rol y proyecto. Estos contextos privados
no producen trazas, vídeo ni capturas: una guarda automática rechaza activarlas
también mediante `test.use()`. Playwright instrumenta incluso contextos creados a
mano; un registro de `fill()` expondría contraseña
y OTP. Las páginas sin autenticación sí conservan traza y vídeo solo al fallar.
La ejecución es serial para no competir por un mismo TOTP.

Los guiones que cierran sesión declaran `test.use({ reuseRoleSession: false })`:
su `authenticatedPage` inicia una sesión propia, nunca la incorpora al caché del
worker y cierra su contexto al terminar. El resto conserva la reutilización por
rol. `roleSession(role)` sigue devolviendo únicamente la sesión compartida.
`logout_isolation.spec.ts` comprueba para ambos roles que un logout propio no
revoca esa sesión, y `session.spec.ts` puede ejecutarse después de los recorridos
en el mismo worker.

El smoke exige los campos reales de acceso, no un encabezado de relevo ni el aviso
de navegador incompatible. El arnés no sustituye el acceso por cookies sembradas
o rutas HTTP simuladas. La prueba `session.spec.ts` solo verifica la infraestructura
de autenticación de cada cuenta; los recorridos funcionales por rol pertenecen a
VIG-332 y VIG-333.

## Comprobaciones de despliegue nº 8 y 9 contra el mundo (sin AWS)

`backend/tools/deploy_checks.py` corre en local contra este mundo con `--target`
(VIG-352). El mundo emite, por el servicio real, una invitación de un solo uso de
coordinación SST en la organización sintética y la escribe en
`deploy_checks_file` (`0600`, en su directorio temporal `0700`), con la misma forma
que el secreto `vigia/<entorno>/deploy-checks`. Ese archivo se borra al parar el
mundo, y el barrido de fugas busca también el token de la invitación.

Con el mundo en marcha (`uv run --frozen python -m tests.e2e_u05` en `backend/`,
que espera `VIGIA_WORLD_READY` y se para con EOF), desde `backend/`:

```sh
WORLD=../frontend/tests/e2e/.world.json
TARGET=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["base_url"])' "$WORLD")
CA=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["ca_file"])' "$WORLD")
FIXTURES=$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["deploy_checks_file"])' "$WORLD")
VERSION=$(python3 -c 'import json; print(json.load(open("../frontend/dist/version.json"))["app_version"])')
UNUSED=$(python3 -c 'print("0" * 64)')  # la nº 6 no corre con --only
# nº 8: recorrido de Playwright de staging (Chromium headless)
uv run --no-project python tools/deploy_checks.py --environment staging-1 --domain vigia.test \
  --verifier-sha256 "$UNUSED" --target "$TARGET" --ca-file "$CA" \
  --fixtures-file "$FIXTURES" --only 8
# nº 9: índice, version.json, paquete immutable y sonda local de vigia-app-index
uv run --no-project python tools/deploy_checks.py --environment pilot --domain vigia.test \
  --verifier-sha256 "$UNUSED" --target "$TARGET" --ca-file "$CA" \
  --app-version "$VERSION" --only 9
```

- `--only` deja las demás comprobaciones en `skipped` con motivo: dependen de AWS.
- `--ca-file` verifica el TLS con la raíz efímera. Chromium no admite esa raíz sin tocar el
  almacén del usuario, así que se le fija solo la clave del certificado que esa raíz verificó
  (`--ignore-certificate-errors-spki-list`); nunca `ignoreHTTPSErrors`.
- En `--target`, la nº 9 cambia la comprobación `vigia-app-index` de Route 53 por una sonda
  local equivalente: `HTTPS_STR_MATCH` de `app_version` en los primeros 5120 bytes de
  `/version.json`, respuesta 2xx o 3xx.
- La invitación es de un solo uso: para repetir la nº 8, arranca otro mundo.
- `--fixtures-file` y `--ca-file` solo se admiten con `--target`; el archivo de material se
  rechaza si es un enlace o tiene permisos de grupo u otros. Ni el informe ni la salida
  incluyen el enlace, el token o el correo.

## Recorridos de gerente, administración, instalador, COPASST y proveedor

```sh
npm run e2e -- tests/e2e/manager.spec.ts tests/e2e/admin.spec.ts tests/e2e/installer_tablet.spec.ts tests/e2e/copasst_tablet.spec.ts tests/e2e/provider.spec.ts
```

Los recorridos de escritorio se ejecutan en Chromium, Firefox y WebKit; los del
instalador y COPASST, en los proyectos táctiles de 768 × 1024. Las combinaciones
que no corresponden al dispositivo se omiten explícitamente.

El administrador invita, asigna roles compatibles y configura el catálogo. El acta
de alcance y el alta del acuerdo requieren `commissioning.run`: esa parte del
recorrido la realiza la sesión del instalador bajo concesión; después el
administrador confirma únicamente su propia firma desde su sesión. COPASST recibe
otro acuerdo pendiente preparado por el instalador mediante la API real.

La carga inicial usa servicios internos, por lo que el recorrido del gerente
provoca antes una consulta HTTP real del proveedor para obtener su rastro de
acceso. El mundo también concede, sube y confirma clips sintéticos por las rutas
mTLS del nodo: sirven al selector de evidencia, no a una prueba de reproducción
ni al cierre del acta.

Las concesiones viven solo en memoria. Para abrir una subruta del instalador por
URL, el recorrido entra al panel, activa la concesión y vuelve con «Atrás», sin
inyectar tokens ni estado de React. La vista local del nodo no existe: se inspecciona
el fragmento del enlace y se pulsa cancelando solo la navegación externa al nodo
inexistente, sin impedir que el manejador real descarte el enlace ni afirmar que reproduzca.

Los diagnósticos del arranque del mundo incluyen solo el tipo de excepción y las
ubicaciones de la pila, nunca el mensaje de una librería o su cuerpo HTTP.
Para invitaciones y códigos de alta se devuelven solo proyecciones booleanas desde
el navegador: las aserciones se realizan después de descartar el secreto. La
desaparición se observa antes de cualquier limpieza defensiva del diagnóstico.

## Recorridos del coordinador y del mando

```sh
npm run build
npm run e2e -- tests/e2e/coordinator.spec.ts tests/e2e/line_lead.spec.ts
# Regresión de orden: logout propio y sesiones reutilizables en el mismo worker.
npm run e2e -- tests/e2e/coordinator.spec.ts tests/e2e/line_lead.spec.ts tests/e2e/logout_isolation.spec.ts tests/e2e/session.spec.ts --project=chromium-desktop
```

Estos recorridos usan los tres proyectos de escritorio; los de tableta se omiten
porque no pertenecen a este guion. Conservan la guarda de CSP del arnés. El mundo
usa el perfil de nodos `u04-e2e` y la semilla `449`, reserva dos hallazgos distintos
por motor y prepara una licencia sintética, una política por las rutas reales del
instalador bajo concesión y un paquete de mes vencido mediante el compositor real.
La cuenta del mando nace con alcance de una sola zona. Las clasificaciones, la
evidencia PNG, las firmas, las resoluciones y los compromisos del recorrido se
registran desde el navegador, sin interceptar ni simular la API.

Si faltan bibliotecas del sistema para WebKit y no se puede instalar
`npx playwright install-deps webkit`, se admite `playwright run-server` de la misma
versión de Playwright dentro de su imagen oficial, usando **el ejecutable fijado**
por `webkit-build.json`, no otro navegador. Publique su puerto solo en
`127.0.0.1` y pase su dirección por `VIGIA_WEBKIT_WS_ENDPOINT`. El cliente expone
únicamente su red de bucle local al navegador remoto; el modo `launchServer` no
activa ese túnel. Ni la API ni las cuentas
se sustituyen y CSP sigue activa. El contenedor debe detenerse al terminar.
