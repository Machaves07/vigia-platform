# Plataforma de integración persistente

**Es el entorno al que envía la sombra del nodo y donde se da de alta el nodo real por SCR-07** (TASK-545, VIG-393; adenda A-76 D3). No es el piloto (A-47: sin AWS) ni el entorno de pruebas (`docker-compose.yml`, efímero y solo en `127.0.0.1`). Se levanta con `compose.integration.yml` en la raíz del repositorio; desplegarlo en `server-casa` es otra tarea (VIG-407).

| Servicio | Qué es | ¿Escucha en la máquina? |
|---|---|---|
| `vigia-mtls-edge` | `app.` (8443, personas) y `nodes.` (8444, nodos, con certificado de cliente) | **Sí, y solo en la IP de la tailnet** (`VIGIA_EDGE_BIND`) |
| `vigia-api-1`, `vigia-api-2` | la API de siempre, detrás del balanceador | No |
| `vigia-worker` | tareas periódicas y entrega de eventos | No |
| `postgres` | PostgreSQL 16, volumen `datos` | No |
| `localstack` | S3, KMS y Secrets Manager (4.14.0 comunitaria) | No |
| `init`, `vigia-migrate`, `vigia-admin` | arranque, migraciones y órdenes puntuales (perfil `tools`) | No |

## Por qué solo escucha el balanceador

`AlbCertificateHeaders` confía en las cabeceras `X-Amzn-Mtls-*` **sin verificar la cadena**: la verificación la hace quien termina TLS, en producción el ALB y aquí `vigia-mtls-edge`. Una petición directa a `vigia-api` con las cabeceras públicas de un nodo se aceptó con 200 en la revisión de VIG-381. Por eso:

- `vigia-api`, PostgreSQL y LocalStack están en la red `interna` (`internal: true`: sin entrada ni salida de la máquina) y **ningún servicio salvo el balanceador lleva `ports`**. Lo fija `backend/tests/unit/test_compose_integration_file.py` sobre el YAML, y `test_compose_integration.py` lo comprueba contra `docker compose ps`.
- `VIGIA_EDGE_BIND` es obligatoria (sin ella `docker compose` no interpreta el archivo) y el contenedor se niega a arrancar si es `0.0.0.0`, `::` o no es una IP (`edge_guard.py`). Dentro del contenedor el balanceador escucha en todas sus interfaces (su propio espacio de red); lo que Docker publica en el anfitrión es solo `${VIGIA_EDGE_BIND}:8443` y `:8444`.
- No añadas `ports`, `network_mode: host` ni otra red al resto de servicios: la prueba lo impide, y con ello se reabre el 200 anterior.

### Quién decide en qué autoridad confía `nodes.`

`vigia-mtls-edge` lee la raíz de los nodos de `http://localstack:4566/vigia-edge/ca/root.pem`: **sin autenticar**, y LocalStack no pide ninguna credencial a quien escribe (acepta cualquiera). **Quien pueda escribir ese objeto decide qué nodos se aceptan.** El acceso queda así restringido, y esto es todo lo que lo protege:

1. LocalStack solo está en la red `interna`: lo alcanzan los contenedores de esta composición y nada más.
2. Dentro de esa red, quien escribe es `vigia-admin` (`bootstrap`, `rotate-node-ca`) y `init`/`state.py` al restaurar. `vigia-api` y `vigia-worker` no escriben en `ca/`.
3. Desde el anfitrión, quien pueda hablar con el socket de Docker (el grupo `docker` de `server-casa`: el usuario `agente` y los administradores) puede ejecutar un contenedor en esa red y reescribirlo. **Trata la pertenencia al grupo `docker` de `server-casa` como el permiso de elegir la autoridad de los nodos**, y no la amplíes sin decidirlo.
4. El balanceador recarga la raíz cada 60 s y conserva la que tenía si la lectura falla: una raíz borrada no abre nada.

## Requisitos

- Docker con **Compose ≥ 2.20** (probado con 2.27). En `server-casa`, Tailscale ya unido: `tailscale ip -4` da `VIGIA_EDGE_BIND`.
- Los nombres `app.<dominio>` y `nodes.<dominio>` (`VIGIA_DOMAIN`, por omisión `vigia.test`) resuelven a esa IP en **cada cliente**: MagicDNS de Tailscale o una línea en `/etc/hosts` (`100.64.0.1 app.vigia.test nodes.vigia.test`). El nodo de planta también los necesita (Q9: `time_sources` y resolución por la tailnet).
- La imagen de `backend/Dockerfile` construida en esa máquina. Necesita la clave de despliegue de solo lectura de `vigia-contracts` en el agente SSH de quien construye (`server-casa` no guarda ninguna: constrúyela en el PC y súbela, o usa un registro local):

  ```bash
  docker buildx build --ssh default --load -t vigia-platform:integration -f backend/Dockerfile .
  ```

- Opcional pero recomendable: el respaldo local de contraseñas filtradas (ver «Lo que este entorno no hace»).

## Primera instalación

```bash
cp local/integration/.env.example local/integration/.env     # edita VIGIA_EDGE_BIND y el dominio
docker compose -f compose.integration.yml --env-file local/integration/.env up -d --wait
local/integration/bootstrap.sh --first-install
```

`bootstrap.sh` (todos los guiones de esta carpeta leen `local/integration/.env`; con `VIGIA_INTEGRATION_ENV` otro archivo):

1. levanta la infraestructura y aplica las migraciones (`vigia-migrate`);
2. corre `vigia-admin bootstrap`: organización proveedora, cinco claves de firma y la raíz de `vigia-node-ca` en `ca/root.pem`. Con **`--first-install`** crea la clave de la autoridad (volumen `autoridad`, `0600`);
3. `seed.py site`: la organización cliente `INT-CLI`, la planta `INT-PLANTA` con dos zonas (catálogo v1 publicado, compuertas de montaje aprobadas y la de uso en la primera), los umbrales de flota y **un usuario por rol** con contraseña y segundo factor (TOTP) de prueba;
4. copia las credenciales de esas personas a `~/.local/state/vigia-integration/credentials.json` (`0600`, fuera del árbol; `VIGIA_INTEGRATION_CREDENTIALS` cambia la ruta). Sin secretos en el repositorio, en la salida ni en el archivo de composición;
5. fuerza una copia del estado de LocalStack y levanta el perfil `app` (`vigia-api` ×2, `vigia-worker` y el balanceador).

Es **idempotente**: repetirlo da la misma salida (una prueba lo exige) y no crea nada nuevo. La salida es solo el resumen (`proveedora`, `raiz_de_nodos`, …, `BOOTSTRAP_OK`).

### La marca `--first-install` (A-89)

Solo la primera instalación la lleva. **La receta tras un reinicio, un fallo o una restauración es `bootstrap.sh` sin la marca**, que hace `vigia-admin bootstrap --resume --publish-root`: reutiliza la clave de la autoridad o **falla**, y nunca publica otra autoridad. Si falta la clave (un volumen `autoridad` mal montado o perdido), la orden termina con error y no escribe nada, ni siquiera con el depósito vacío:

```text
la clave de la autoridad no existe. En una primera instalación usa bootstrap --first-install; si ya había nodos, comprueba el volumen de la clave
```

Volver a crearla (`--first-install`) es una decisión deliberada: los nodos ya dados de alta quedarían huérfanos y habría que registrarlos de nuevo. `test_compose_integration.py` borra la clave y comprueba que la receta falla sin publicar nada.

## Día a día

```bash
alias vigia-int='docker compose -f compose.integration.yml --env-file local/integration/.env --profile app'
vigia-int up -d --wait          # arranque (tras reiniciar el servidor: Docker los levanta con restart: unless-stopped)
vigia-int ps
vigia-int stop                  # parada
vigia-int down                  # parada y borrado de contenedores; los volúmenes se conservan
vigia-int down --volumes        # SOLO para empezar de cero: borra la base, la clave de la autoridad y los secretos
local/integration/admin.sh rotate-key catalog --operator <id>    # vigia-admin; fuerza la copia de LocalStack
```

Dar de alta un nodo: `docker compose ... run --rm -T vigia-admin python /integration/seed.py issue-code` declara el nodo en una zona libre (crea la siguiente zona si no queda ninguna) y escribe una línea JSON con el `enrollment_code` de un solo uso; el nodo se da de alta con él por `https://app.<dominio>/api/nodes/enrollment` y late por `https://nodes.<dominio>/api/nodes/heartbeats`. (La guía de alta por SCR-07 es VIG-408.)

## Reiniciar sin perder las credenciales de nodo

LocalStack comunitario guarda todo en memoria: al reiniciar pierde los depósitos de S3, los secretos de Secrets Manager y la clave KMS `vigia-secrets`. La persistencia nativa (`PERSISTENCE=1`) es de pago y esta edición la ignora (comprobado con la 4.14.0). Perder eso rompería más que las credenciales de nodo: sin la clave KMS nadie descifra el segundo factor de las personas, y sin los secretos de firma `vigia-api` no arranca.

Se resuelve sin pago y sin AWS real, dentro de LocalStack (`local/integration/localstack/state.py`):

| Qué | Dónde vive | Cómo vuelve tras un reinicio |
|---|---|---|
| Clave de la autoridad de nodos | volumen `autoridad`, `0600` (firmante por archivo, VIG-382) | no pasa por LocalStack |
| Clave KMS `vigia-secrets` (`alias/vigia-secrets`) | identificador (UUID) y material de 32 bytes, generados al azar en el volumen `estado` | se recrea con **el mismo identificador y el mismo material** (`_custom_id_` y `_custom_key_material_`) y su alias: el texto cifrado anterior se sigue descifrando |
| Secretos (base, clave del hash de origen, claves de firma, invitación) | copia `estado/localstack-snapshot.json` (`0600`) cada 10 s | se recrean con **el mismo ARN y la misma versión** (la base guarda el ARN de cada clave de firma) |
| `ca/root.pem` | la misma copia | se vuelve a escribir |
| Depósitos y centinela de salud | se recrean | — |

El contenedor no queda sano hasta terminar `restore`, y no guarda nada hasta entonces (un arranque que falló no sustituye una copia buena por otra vacía).

**Qué se pierde:** los objetos de los depósitos que no son `vigia-edge` (clips de integración y evidencias: aceptado), las versiones anteriores de un secreto, y lo escrito en los 10 s anteriores a una caída o un reinicio (`bootstrap.sh` y `admin.sh` fuerzan una copia al terminar, y los secretos solo cambian con esas órdenes). No hay gancho de parada: LocalStack ya no atiende la API cuando lo ejecuta (probado, 503). La copia no sustituye al respaldo de los volúmenes:

```bash
# Respalda la clave de la autoridad de nodos y los secretos (no van en pg_dump). Guárdalo fuera del servidor.
docker run --rm -v vigia-integration_autoridad:/ca:ro -v vigia-integration_estado:/state:ro -v "$PWD":/out alpine \
  sh -c 'umask 077 && tar czf /out/vigia-integracion-secretos.tgz /ca /state'
```

## Copia y restauración de la base

```bash
local/integration/backup.sh                       # escribe ~/vigia-integration-backups/vigia-<fecha UTC>-<pid>.dump (0600)
local/integration/restore.sh --yes <copia.dump>   # SUSTITUYE la base; para y vuelve a levantar el perfil app
```

Copia diaria por `cron` en el host (retiene 14 días; `VIGIA_BACKUP_DIR` y `VIGIA_BACKUP_KEEP_DAYS` lo cambian):

```cron
0 3 * * * cd /ruta/a/vigia-platform && local/integration/backup.sh >> ~/vigia-integration-backups/backup.log 2>&1
```

`backup.sh` escribe cada copia en un temporal propio y la publica solo tras comprobar que `pg_restore --list` la lee (dos copias a la vez no se pisan). `restore.sh` corta las conexiones y usa `pg_restore --create --clean --if-exists`: borra y recrea la base con los permisos de base que dejó la migración `nuc_0001` (con las tablas particionadas, `--clean` sobre la base viva falla). Los roles son del servidor, no de la base: en un servidor nuevo, `up -d --wait` y `docker compose ... run --rm vigia-migrate` los crean antes del primer `restore.sh`. La copia **no** lleva la clave de la autoridad ni los secretos: respáldalos aparte (arriba).

## Confiar en la raíz del balanceador desde un navegador de planta

El certificado de servidor de `app.` y `nodes.` lo crea una vez `vigia-mtls-edge` (`init-cert`, con los nombres de `VIGIA_DOMAIN` y la IP de `VIGIA_EDGE_BIND`) y persiste en el volumen `borde`. La raíz que firma ese certificado no es la de los nodos: es `edge-ca.pem`, y hay que repartirla a los navegadores y a los nodos:

```bash
docker compose -f compose.integration.yml --env-file local/integration/.env --profile app cp vigia-mtls-edge:/edge/edge-ca.pem ./edge-ca.pem
```

Impórtala como autoridad de confianza en el navegador de la persona (Firefox: «Autoridades» en Certificados; Chrome/Edge: almacén de raíces del sistema) y entrega el mismo archivo al nodo para que verifique `nodes.`. Su clave no se guarda: para renovar el certificado de servidor (cambió la IP o el dominio) borra el volumen `borde` (`down`, `docker volume rm vigia-integration_borde`), vuelve a levantar y reparte la raíz nueva.

## Lo que este entorno no hace

- **Clips por almacén desde el nodo.** LocalStack no publica ningún puerto, y `ClipUploadGrant.upload_url` debe ser `https` y alcanzable por el nodo: con esta composición las concesiones de subida de clips no son utilizables desde fuera. Las pruebas de latido y alta sí. Abrir un `https` de almacén en la tailnet es otro puerto publicado y una decisión aparte (VIG-407 / dueño).
- **Aplicación de página única.** La imagen la lleva (`/app/static`, la construye `backend/Dockerfile`). La verificación de VIG-393 se hizo con la de pruebas (`backend/tests/fixtures/static`): `npm ci` de `@vigia/contracts` por git+ssh falló dentro del `docker build` en el PC de la torre.
- **Contraseñas filtradas.** `vigia-api` no arranca sin el respaldo local (`VIGIA_BREACH_LIST_PATH`) y la imagen no lo trae: el `Dockerfile` no ejecuta `tools/build_pwned_top100k.py`. `init` escribe una lista **sintética de tres entradas** (la de las pruebas), válida para datos sintéticos y que **no protege a personas reales**. Para una lista real, genérala en una máquina con red (`uv run python tools/build_pwned_top100k.py --source <lista> --format plaintext --output local/integration/pwned-top100k.txt`) y repite `up`: `init` la valida y la usa.
- **Lista de revocación.** `vigia-node-trust` (almacén de confianza de ELBv2) no existe en LocalStack comunitario; `VIGIA_NODE_TRUST_STORE_ARN` no se define y el worker no publica `ca/crl.pem`. La revocación la decide la aplicación por credencial.
- **Colector de OpenTelemetry.** No va en esta composición (es opcional en la ficha); la telemetría se queda en el registro de los contenedores.
- **Producción.** `VIGIA_NODE_CA_BACKEND=file` solo se admite en `local` e `integration` (`node_ca_backend_forbidden` en cualquier otro). Aquí no hay nada que se parezca a un despliegue de producción.
