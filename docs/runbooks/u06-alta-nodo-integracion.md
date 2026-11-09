# U-06 · Alta de un nodo contra integración

VIG-408 / TASK-548 · RB-BOR-01 y RB-BOR-11, lado plataforma. A-76, A-88 y A-89
mandan sobre el diseño histórico. **No autoriza ejecutar en planta ni en server-casa**:
la torre hace el ensayo en el PC, con datos sintéticos; el dueño ejecuta TASK-562.
El prototipo sigue intacto, sin ocupar su serie, voz ni baliza. Un latido no afirma
que una zona esté despejada (P2); el humo tampoco acredita privacidad ni rendimiento.

## Preparación y responsabilidades

- Instalador: concesión vigente con `commissioning.run`, `fleet.read` y `fleet.manage`.
  Administradora: planta, zonas y catálogo. Operadora de plataforma: `vigia-admin`.
  La operadora debe haber completado su invitación y estar activa; `bootstrap` deja su
  invitación pendiente, no una sesión administrativa utilizable. No activarla por SQL
  en un destino real: esa preparación directa es solo del arnés sintético de pruebas.
- [Composición de VIG-393](../../local/integration/README.md): imagen construida,
  Docker/Compose, DNS de `app.` y `nodes.` resolviendo a la IP de la tailnet en
  **cada** equipo; en el PC se usa bucle local y un proyecto Compose exclusivo.
  La URL lleva puertos: app. `8443`, nodes. `8444`, salvo configuración explícita.
- Instalador, servicio y configuración del nodo pertenecen a **vigia-nodo**, no a
  este repositorio ([rotación forzada](u03-6.1-forced-credential-rotation.md)). Las opciones
  siguientes se contrastaron con `src/vigia_nodo/app/cli/{enroll,framework,admit_camera,
  capture_baseline,blur_check}.py` del clon `nodo-ref394` (`815e8f1`). Verificar también `vigia-nodo
  <orden> --help` en la versión instalada. No existe `--enroll-url` en el nodo del kit.
- No adjuntar códigos, claves, cookies, tokens, credenciales ni URL prefirmadas al acta.
  Entregar el código por canal reservado, sin historial de terminal ni `set -x`.
  Las variables de los ejemplos son valores que aporta el operador, **no secretos de ejemplo**.

## Recorrido numerado

### 1. Arrancar el destino sintético y conservar la autoridad

**Comandos reales:** `local/integration/bootstrap.sh --first-install`, solo en primera
instalación deliberada; después, `local/integration/bootstrap.sh` **sin** esa marca.
Seguir [primera instalación y reinicio](../../local/integration/README.md).
No generar otra CA para arreglar un fallo de reinicio: comprobar el volumen `autoridad`
y restaurarlo (A-89). El grupo Docker puede elegir la autoridad: no ampliar ese acceso.
Para el ensayo usar `COMPOSE_PROJECT_NAME=vigia-408-ensayo`, IP `127.0.0.1`, puertos y
subred libres; nunca parar proyectos ajenos.

En el PC, con `INTEGRATION_CONFIG` apuntando al archivo de configuración **sintético**
del ensayo (fuera del árbol), mantener este mismo contexto para todos los comandos:

```bash
export COMPOSE_PROJECT_NAME=vigia-408-ensayo
export VIGIA_INTEGRATION_ENV="$INTEGRATION_CONFIG"
vigia_int() {
  docker compose -f compose.integration.yml --env-file "$VIGIA_INTEGRATION_ENV" \
    --profile app --profile tools "$@"
}
```

Los scripts `bootstrap.sh` y `admin.sh` respetan estas variables. No adoptar el nombre
del proyecto de otro operador ni copiar el contexto del ensayo a un destino persistente.

### 2. Preparar Pope una vez, sin duplicar la semilla

**Comando real**, desde `backend/`: [seed_pope.py](../../backend/tools/seed_pope.py)
y su [procedencia](../../backend/tools/pope/SOURCE.md).

```bash
uv run python -m tools.seed_pope --dry-run
uv run python -m tools.seed_pope --base-url "$APP_ORIGIN" --plant "$PLANT_ID" \
  --concession "$CONCESSION_ID" --ca-bundle "$EDGE_CA"
```

Las dos sesiones verificadas se entregan por el mecanismo de VIG-394, fuera del árbol;
no leer ni imprimir archivos de credenciales para ejecutar este runbook. La planta ya debe
existir. La semilla prepara POPE, POPE-FOSO, cámaras, catálogo, montaje, nodo y código.
Su evento `enrollment_code` es entrega reservada de una sola vez; **no guardar su salida
como Evidence**. El evento `seeded` solo contiene `event`, `message_es`, `writes`, `node_id`,
`zone_ids`. Repetir la semilla no reemite códigos ni altera decisiones humanas.

**Límite medido en la composición actual:** tampoco es alcanzable desde el PC la URL de
subida del documento de montaje: `PUT` hacia el anfitrión interno `localstack` termina en
`ConnectError`. La semilla queda incompleta antes de emitir el código; no dar Pope por
preparada ni sustituir su acta por SQL. Reanudar por VIG-394 solo después de resolver la
publicación del almacén. Para aislar el transporte, el ensayo automatizado de esta tarea
usa entonces `seed.py issue-code` de VIG-393 sobre su segunda zona sintética (commissioning),
**no** como sustituto del recorrido Pope. Este diagnóstico parcial no cumple el criterio 2.

### 3. Verificar catálogo y compuertas antes del alta

**SCR-04**, [catálogo](../../frontend/src/pages/scr04/catalog/CatalogView.tsx) y
[parámetros](../../frontend/src/pages/scr04/parameters/ParameterView.tsx): comparar cámaras,
señales, cobertura y versiones de ambas zonas. **SCR-05**,
[compuertas de zona](../../frontend/src/pages/scr05/gates/ZoneGates.tsx): montaje aprobado,
uso pendiente, modo `commissioning`. No aprobar uso para hacer pasar el humo. La semilla
no sustituye actas ni consentimientos de planta. El catálogo Pope copiado es v1.1.0;
no acredita las reglas pendientes de la ampliación del contrato.

### 4. Declarar o localizar el nodo y copiar su identidad

**SCR-07**, `/fleet/enrollment`, [DeclareNode](../../frontend/src/pages/scr07/enrollment/DeclareNode.tsx):
si se usó la semilla, localizar su nodo existente, no declarar otro sobre las mismas zonas.
Para un nodo distinto: elegir planta, código legible y zonas; guardar `node_id`,
`organization_id`, `plant_id` y asignaciones. El `node_id` va en `config.toml` del nodo;
no inventarlo ni usar el código legible como UUID. La identidad la crea la plataforma.

### 5. Obtener el código y contrastar todas las huellas

**SCR-07**, `/fleet/nodes/{node_id}/enrollment`,
[EnrollmentCode y AttemptsTable](../../frontend/src/pages/scr07/enrollment/EnrollmentCode.tsx):
usar el código reservado de la semilla o emitir uno nuevo explícitamente si se perdió.
Se muestra una sola vez; vence a las 24 h y tiene un solo uso. Conservar por canal
reservado el código y las **todas** las `node_ca_root_sha256[]` (una o dos en rotación).
Revisar intentos: instante, resultado, huella de hardware y versiones. No emitir códigos
repetidamente para diagnosticar red. Uno nuevo sustituye al anterior activo.

### 6. Distribuir confianza y configurar fuentes de tiempo

**Comando real:** `vigia_int cp vigia-mtls-edge:/edge/edge-ca.pem "$EDGE_CA"`
según [confianza del balanceador](../../local/integration/README.md), y
`openssl x509 -in <certificado> -noout -fingerprint -sha256` por certificado del paquete.
Hay **dos raíces distintas**:

| Raíz | Para qué | Distribución |
|---|---|---|
| `edge-ca.pem` | TLS de app. y nodes. | Importar como CA en navegador/sistema de los equipos y en el sistema/contenedor del nodo; comprobar origen y huella por canal confiable. |
| `root.pem` (CA de nodos) | Certificados de cliente y de la vista local | Entregar paquete público por el canal del instalador; comparar **cada** huella con SCR-07; instalar también en los navegadores que abren la vista. |

No confundir la huella del archivo PEM con la del certificado DER; no distribuir claves
privadas. No aceptar avisos del navegador ni usar `curl -k`. El `--ca-file` de `vigia-nodo
enroll` es **root.pem de nodos**: conserva además las raíces web del sistema, donde se
instaló edge-ca. El `--ca-file` del **humo** es la confianza TLS de app./nodes./almacén
(edge-ca en la composición); las credenciales del contrato validan la respuesta del alta.
El humo añade la CA recibida por ese canal autenticado para el GET de la vista.

**Operadora**, [vigia-admin node-configuration](../../backend/src/vigia_platform/fleet/application/admin_node_configuration.py):

```bash
local/integration/admin.sh node-configuration show --node "$NODE_ID" --operator "$OPERATOR_ID"
local/integration/admin.sh node-configuration set --node "$NODE_ID" --operator "$OPERATOR_ID" \
  --time-source "$NTP_HOST" --heartbeat-interval 60
local/integration/admin.sh node-configuration show --node "$NODE_ID" --operator "$OPERATOR_ID"
```

Repetir `--time-source` para sustituir la lista entera (1–8 fuentes alcanzables desde planta,
sin suponer que `pool.ntp.org` funciona). Verificar DNS, ruta y servicio NTP, no solo la fila.
Hacerlo **antes del alta**: `time_sources` llega en configuración inicial. Si `set` devuelve
`temporarily_unavailable`, **confirmar con `show` antes de repetir**: un `set` repetido deja
otra entrada de auditoría (A-88). La auditoría acredita el intento autorizado; la fila manda
para el valor vigente. El intervalo es 15–600 s; el mudo se deriva como cinco intervalos.

### 7. Dar de alta: elegir humo sintético o instalador, nunca ambos con el mismo código

**Humo en el PC**, desde `backend/`:

```bash
uv run python -m tools.node_enrollment_smoke --base-url "$APP_ORIGIN" --code "$CODE" \
  --node-id "$NODE_ID" --organization-id "$ORGANIZATION_ID" --plant-id "$PLANT_ID" \
  --ca-file "$EDGE_CA"
```

Se usan **NodeCredentials + SyncClient de vigia_contracts** contra la API real, siguiendo
el nodo propio de VIG-393; **no** `vigia_contracts.conformance.simulated_node`, cuyo
`--target`/`--token-file` requiere StubAdmin y no sirve para este alta. El humo crea
credenciales y cola en un directorio temporal que elimina al terminar; no es un nodo
persistente ni un reemplazo del instalador. Consume el código, envía tres latidos sin
esperar intervalos, un evento de cámara y un clip sintético de 10 s, `purpose=verification`,
subida y confirmación. Solo 0 significa recorrido completo. No genera vídeo reproducible
ni verifica el difuminado. No sirve para una zona real ni prueba capacidad de Modbus:
el lector se declara no disponible; su etiqueta de contrato no acredita un dispositivo.

**Nodo real, solo dueño/TASK-562**, servicio detenido por su instalador antes del alta:

```bash
vigia-nodo enroll --config "$NODE_CONFIG" --url "$APP_ORIGIN/api/nodes/enrollment" \
  --ca-file "$NODE_ROOTS" --organization-id "$ORGANIZATION_ID" --plant-id "$PLANT_ID"
```

Pide código sin eco (también admite `--code`, no recomendado en argv); comparar todas las
huellas y confirmar `si`. Las claves se generan en el equipo y nunca se copian a plataforma.
Después arrancar el servicio con el instalador de vigia-nodo. No copiar aquí un comando
systemd del diseño histórico: el nombre de la unidad lo decide esa instalación.

### 8. Comprobar el primer latido y la cobertura

**Comando:** `vigia-nodo status --data-dir "$NODE_DATA" --json` con servicio en marcha.
**SCR-07**, [NodeDetail](../../frontend/src/pages/scr07/inventory/NodeDetail.tsx): nodo `enrolled`,
credencial activa, vencimiento y comunicación alcanzable, cámaras y tasas recibidas.
**SCR-03**, [CoverageView](../../frontend/src/pages/scr03/CoverageView.tsx): comprobar
observabilidad, causas, cobertura mínima y tiempos; no equiparar «alcanzable» a observable
o «seguro». Con el humo, contrastar `heartbeats_accepted=3`, no simplemente HTTP 200.

### 9. Admitir cámaras y capturar cada par cámara-zona

**Comandos reales de vigia-nodo**, con servicio en marcha (socket de control local):

```bash
vigia-nodo admit-camera --camera "$CAMERA_ID" --data-dir "$NODE_DATA" --secrets-dir "$NODE_SECRETS"
vigia-nodo capture-baseline --camera "$CAMERA_ID" --zone "$ZONE_ID" --data-dir "$NODE_DATA"
```

La credencial de cámara se pide sin eco; la admisión ensaya las cámaras, no se fuerza.
Para la línea base, la persona comprueba y confirma zona despejada con iluminación habitual.
Repetir por cada par (cam102 pertenece también a POPE-FOSO), y si cambia el encuadre.
**SCR-04** parámetros y **SCR-03** cobertura contrastan el resultado; el humo no captura
imágenes ni sustituye estos pasos de hardware, que no se ejecutan en el PC sin cámaras.

### 10. Confirmar el clip y preparar el walk-test

**Comando:** `vigia-nodo blur-check --camera "$CAMERA_ID" --zone "$ZONE_ID" --data-dir "$NODE_DATA"`.
**SCR-06**, [sesión](../../frontend/src/pages/scr06/session/SessionScreen.tsx),
[matriz](../../frontend/src/pages/scr06/matrix/MatrixScreen.tsx) y
[cierre](../../frontend/src/pages/scr06/close/CloseScreen.tsx): elegir el clip de verificación
confirmado antes del walk-test, comprobar difuminado y requisitos del acta. La lista real
es `GET /zones/{zone_id}/commissioning-clips`. Sin confirmación no hay evidencia válida;
el humo exige `verification_clip=confirmed`.

**Bloqueo conocido:** la composición actual no publica el almacén: `POST clip-uploads`
devuelve **503 `temporarily_unavailable`** en cada intento. El humo termina **3** con
`clip_storage_unpublished`, explicación VIG-407 / TASK-562, **sin reintentar**. No es éxito
parcial ni motivo para aprobar uso: detener este tramo y escalar al dueño. La API en proceso
con LocalStack tras TLS sí prueba subida y confirmación. No abrir un puerto ni cambiar ACL
para sortearlo dentro de esta tarea.

### 11. Abrir la vista desde los equipos autorizados

**SCR-16**, [entrada](../../frontend/src/pages/scr16/entry/index.tsx) y
[LiveViewLauncher](../../frontend/src/components/LiveViewLauncher/LiveViewLauncher.tsx):
con la URL local anunciada por el latido, leer la pantalla previa, pedir token y pulsar el
enlace. La URL es `https://<dirección-local>:8443/`; el token va en fragmento y desaparece
al usarlo/vencer. No pegarlo en logs, capturas ni Evidence. El navegador debe confiar en
la CA de nodos; comprobar acceso auditado y ausencia de avisos TLS.
Para un servidor **sintético** alcanzable, el humo admite `--live-view-url "$LIVE_URL"`:
anuncia exactamente esa dirección y hace GET sin token ni cookies; solo comprueba alcance,
**no** la sesión autenticada, imágenes ni el registro de acceso de SCR-16. Omitirlo cuando
no hay servidor; `not_requested` no acredita este paso.

### 12. Re-alta, revocación y retiro

**SCR-07**, [ciclo de vida](../../frontend/src/pages/scr07/lifecycle/index.tsx) y
[código e intentos](../../frontend/src/pages/scr07/enrollment/EnrollmentCode.tsx), siguiendo
[rotación forzada](u03-6.1-forced-credential-rotation.md):

- Mismo equipo: revocar con motivo y confirmación si corresponde, emitir código nuevo sobre
  el **mismo node_id**; servicio detenido, repetir `vigia-nodo enroll` del paso 7 con
  `--replace`. Conserva configuración y líneas base; exige la misma huella de hardware.
  Comprobar nueva credencial y primer latido. No reutilizar un código consumido.
- Equipo distinto: declarar en SCR-07 uno nuevo con `replaces_node_id`, que revoca el viejo
  y hereda zonas. No reutilizar identidad, claves ni huella del anterior. Volver a admitir
  cámaras y capturar líneas base según el instalador.
- Retiro: revocar, comprobar rechazo `node_revoked` en la siguiente petición; dar de baja
  con motivo desde SCR-07 solo después de revocado. No borrar expediente ni cola. La
  revocación de la credencial anterior no se deshace. En integración manda la primera capa
  de aplicación: no hay trust store ELB ni CRL publicada por LocalStack comunitario.
- Un código usado en un nodo aún `enrolled` se enmascara como `enrollment_code_invalid`
  por la API. Tras abrir re-alta autorizada, presentar el código **anterior consumido**
  produce `enrollment_code_used`, salida no cero del humo. No reinterpretar inválido como
  usado ni repetir altas automáticamente. El ensayo de integración comprueba este estado.

### 13. Registrar resultado y cerrar el ensayo del PC

**Comandos reales:** guardar solo el JSON seguro del humo y el checklist (sin la salida de
la semilla), `vigia_int down --volumes --remove-orphans`
con el **mismo proyecto exclusivo** del paso 1. Comprobar `docker ps` y `docker volume ls`
para ese proyecto y que no quedan procesos propios. No usar `down --volumes` en un destino
persistente ni parar contenedores ajenos. Resultado del ensayo: [Evidence](../evidence/VIG-408/EVIDENCE.md).

## Síntoma → dónde mirar

| Síntoma | Dónde mirar / acción conservadora |
|---|---|
| Latido mudo | SCR-07 detalle/alarma, DNS y puerto de nodes., certificado vigente, reloj, red/Tailscale, `vigia-nodo status`; mudo = cinco intervalos. No generar código a ciegas. |
| `simulated_adapter_in_productive` | SCR-05 compuerta de uso y modo; `status` y configuración del adaptador. Volver a commissioning por el procedimiento autorizado, no mentir sobre señales ni activar uso con simulación. |
| `orphan_clips_growing` | SCR-07 alarmas/cola, `status`, almacén y confirmaciones; buscar interrupción entre PUT, confirmación y referencia. No borrar registros para silenciarla. |
| 503 `temporarily_unavailable` en clip-uploads; humo 3 / `clip_storage_unpublished` | Limitación conocida de compose.integration.yml: almacén sin HTTPS público para el nodo. VIG-407 / TASK-562, decisión del dueño; sin bucle de reintentos ni aprobación de SCR-06. |
| Semilla Pope incompleta, `PUT` de documento a `localstack` no alcanzable | Mismo aislamiento del almacén: URL interna de documentos no alcanzable desde el PC. Parar el recorrido Pope y revisar publicación con el dueño; no fingir montaje aprobado. |
| `enrollment_code_used` / `enrollment_code_invalid` | SCR-07 intentos y estado; distinguir código usado, sustituido, vencido y nodo ya enrolled. Emitir otro solo por re-alta autorizada. |
| TLS o huellas distintas | Raíz edge-ca frente a root.pem, DNS/SAN, paquete completo y reloj; parar sin aceptar advertencias. A-89 si se perdió el volumen de CA. |
| `temporarily_unavailable` en node-configuration set | A-88: `show` antes de repetir; la auditoría del intento no es el valor vigente. |
| SCR-16 sin enlace o GET correcto sin vídeo | URL no anunciada/local no alcanzable, puerto 8443, confianza y token; el GET del humo no acredita una sesión de vista. |

## Checklist para Evidence

Marcar cada paso 1–13 con resultado real, destino, comando/pantalla y limitación. Separar
composición, API en proceso y tareas humanas de hardware; **no marcar recorrido completo**
si el clip sigue bloqueado, no hubo navegador o no se ejecutó un comando. No aprobar
`productive` como consecuencia del humo. Las dudas de publicación de clips y el ensayo
del nodo real pertenecen al dueño, no a este runbook.
