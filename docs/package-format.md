# Formato del paquete que lee el verificador (`vigia-package`, versión 1)

Contrato entre la plataforma núcleo (U-02, TASK-116, LC-NUC-18) y la exportación de U-04: qué
escribe U-04 en cada exportación y en cada paquete mensual para que el cliente lo verifique con
`vigia_verify.py`, **sin red, sin acceso a la plataforma y sin ningún secreto del proveedor**
(BR-NUC-57, NFR-NUC-53).

- Verificador: `backend/tools/vigia_verify.py`, generado por `backend/tools/build_verifier.py` a
  partir de `vigia_platform.ledger.chain` (`pure_rfc8785`, `pure_ed25519`, `chain_walk`,
  `package_verifier`). Un solo archivo, Python 3.10 o superior, solo biblioteca estándar. U-04
  copia ese archivo **tal cual** en el paquete y publica su SHA-256 (el que imprime
  `build_verifier.py`) dentro del paquete y en `/.well-known/vigia-verifier` (TASK-137).
- Algoritmo: `business-logic-model.md` §5 y la docstring de `chain_walk.py`. El paso 2 del motor
  de la plataforma (TASK-118) usa el mismo `chain_walk`.

## 1. Contenedor

Un **directorio** o un archivo **`.zip`** con:

```text
manifest.json              manifiesto (§2)
chains/<nombre>.jsonl      una cadena por archivo, JSON Lines (§3); nombres libres
vigia_verify.py            (U-04) copia del verificador; el verificador no la lee
...                        cualquier otro archivo de U-04; se avisa como "no declarado"
```

Rutas de los archivos de cadena: relativas, con `/`, a lo sumo cuatro niveles, cada segmento
`[A-Za-z0-9][A-Za-z0-9_.-]{0,99}` (sin `..`, sin raíz, sin barra inversa). En un directorio, un
enlace simbólico que sale del paquete no se sigue. Todo en UTF-8.

## 2. `manifest.json`

```json
{
  "format": "vigia-package",
  "format_version": 1,
  "organization_id": "5d0f1c9e-7b1a-4f7e-9d7c-2b6f0a9e1c01",
  "chains": [
    {
      "kind": "ledger",
      "plant_id": "0b8e7c6d-5a4f-4e3d-8c2b-1a0f9e8d7c6b",
      "file": "chains/ledger-plant-0b8e7c6d.jsonl",
      "first_sequence": 1,
      "last_sequence": 1204,
      "last_hash": "<record_hash del último registro, 64 hex>"
    },
    {"kind": "ledger", "plant_id": null, "file": "chains/ledger-organization.jsonl", "...": "..."},
    {"kind": "audit", "plant_id": null, "file": "chains/audit.jsonl", "...": "..."}
  ],
  "checkpoint_keys": [
    {"key_id": "checkpoint-2026-09", "public_key": "<Ed25519, 32 bytes en base64 estándar>"}
  ]
}
```

| Campo | Regla |
|---|---|
| `format`, `format_version` | Exactamente `"vigia-package"` y `1`. Otra versión: el verificador se niega y pide usar el que viene en el paquete |
| `organization_id` | UUID en minúsculas |
| `chains` | Lista no vacía. Cada cadena, **exactamente** estas seis claves. `kind`: `ledger` (expediente) o `audit` (auditoría; `plant_id` nulo). `plant_id` nulo en la cadena de organización. Sin cadenas ni archivos repetidos |
| `first_sequence` | `1` si el paquete trae la cadena desde la génesis. Mayor que 1 si solo trae una parte: el enlace con el prefijo ausente se toma del propio paquete (el resumen lo dice); para comprobarlo hace falta un punto de control anterior |
| `last_sequence`, `last_hash` | La cabeza de la cadena al exportar (`ledger.chain_head`). El verificador exige que coincida con la recalculada: un registro de menos o de más al final rompe la cadena |
| `checkpoint_keys` | Todas las claves públicas de propósito `checkpoint`, **vigentes y retiradas** (BR-NUC-54, 55: nunca se retiran). `key_id` sin repetir |
| Otras claves de primer nivel | Se ignoran: U-04 puede añadir metadatos (`exported_at`, `period`, `verifier_sha256`...) sin cambiar la versión |

## 3. Archivos de cadena (JSON Lines)

Una entrada por línea, en orden de `chain_sequence` contiguo desde `first_sequence`; las líneas en
blanco se ignoran; se admite `\n` o `\r\n`; cada línea, como mucho 4 MiB. Cada entrada lleva
**exactamente** las claves de abajo, con los **valores textuales exactos de la base**: UUID en
minúsculas con guiones (`str(uuid)`), marcas en UTC con milisegundos y `Z`
(`AAAA-MM-DDTHH:MM:SS.mmmZ`, como `vigia_canonical_envelope`), hashes en hexadecimal en minúsculas.

### 3.1 Registro del expediente (`kind = ledger`)

```json
{"record_id": "...", "organization_id": "...", "plant_id": "... o null",
 "chain_sequence": 7, "record_type": "finding_received", "schema_version": 1,
 "actor": {"kind": "user", "id": "...", "display_name_snapshot": "...",
           "role_in_use": "coordinator_sst o null", "concession_id": "... o null", "unit": "U-03"},
 "scope": {"plant_id": "... o null", "zone_id": "... o null", "node_id": "... o null"},
 "correlation_id": "...", "received_at": "2026-09-29T14:03:07.125Z",
 "content": {"...": "el documento JSON de la columna content"},
 "content_hash": "...", "previous_hash": "...", "record_hash": "..."}
```

Son las columnas de `ledger.ledger_record` del sobre de BR-NUC-46 (con `actor_*` y `scope_*`
agrupados como en el sobre canónico), más `content`, `previous_hash` y `record_hash`. **No** van
`occurred_at`, `source_key` ni `content_json`: no están cubiertos por el hash.

`content` es el documento JSON cuyos bytes RFC 8785 son la columna `content`. Lo más sencillo y
exacto es **copiar los bytes de la columna** como valor de `content` en la línea (ya son JSON
válido). Si U-04 lo reescribe con otra biblioteca, el valor debe ser el mismo documento: el
verificador lo lee como RFC 8785 (todo número es un doble; un entero de magnitud mayor que
`2**53 - 1`, como `12345678901234567000`, se lee como doble) y lo vuelve a canonicalizar.

### 3.2 Entrada de auditoría (`kind = audit`)

```json
{"entry_id": "...", "organization_id": "...", "chain_sequence": 3,
 "actor": {"...": "igual que en el expediente"},
 "operation": "ledger_read", "scope": {"plant_id": "... o null", "zone_id": "... o null"},
 "resource_ref": {"kind": "ledger_record", "id": "..."},
 "filters": {"...": "el documento de la columna filters"},
 "filters_hash": "... o null", "result_count": 3, "outcome": "success",
 "correlation_id": "...", "occurred_at": "2026-09-29T14:03:07.125Z",
 "previous_hash": "...", "entry_hash": "..."}
```

`resource_ref` es `null` si `resource_kind` y `resource_id` son nulos (como en el sobre de
BR-NUC-60). `filters` y `filters_hash` son nulos a la vez.

### 3.3 Puntos de control

- Expediente: registro con `record_type = "checkpoint"`; auditoría: entrada con
  `operation = "checkpoint"`, con el contenido en **`filters`** (la única columna JSON de la
  auditoría cubierta por el hash; supuesto que TASK-117 debe respetar).
- Contenido, exactamente: `{"covered_sequence", "covered_hash", "taken_at", "key_id",
  "signature"}` (domain-entities §3.4). `covered_sequence` es la secuencia anterior y
  `covered_hash` su hash (o el de génesis si el punto de control es el primer registro).
- `signature`: Ed25519 (64 bytes, base64 estándar) sobre el canónico RFC 8785 de
  `{"covered_hash", "covered_sequence", "kind", "organization_id", "plant_id", "taken_at"}`, con
  `kind` `ledger` o `audit` y `plant_id` el de la cadena (nulo en las de organización y auditoría).

## 4. Qué comprueba el verificador

Por cada cadena, desde la génesis (`SHA-256("vigia:genesis:" + organization_id + ":" +
(plant_id | "organization"))`) o desde el enlace de la primera entrada:

1. forma de la entrada (claves y tipos exactos);
2. secuencia contigua;
3. organización y planta de la cadena;
4. `previous_hash` igual al hash anterior;
5. `content_hash = SHA-256(RFC 8785(content))` (en auditoría, `filters_hash`);
6. `record_hash = SHA-256(RFC 8785(sobre) ‖ previous_hash)` (en auditoría, `entry_hash`);
7. si hay un punto de control anterior de esa cadena, el registro de su secuencia es el mismo;
8. en cada punto de control: cobertura exacta del anterior, clave conocida y firma válida;
9. al final, la cabeza del manifiesto.

El primer fallo fija la **secuencia rota** (la esperada) y se nombra el registro encontrado, con
su línea. Resultado: `intact` o `broken`, en español en la salida estándar y en JSON con `--out`.

## 5. Punto de control de un paquete anterior

`--previous-checkpoint RUTA` (repetible) acepta:

- un archivo `.json` con la línea de un punto de control de un paquete anterior (o una lista de
  ellas), tal cual aparece en su JSON Lines;
- el paquete anterior entero (directorio o zip): se toma el último punto de control de cada cadena.

Cada uno se valida por sí solo (hashes y firma con las claves del paquete actual) y el paquete
actual debe contener **en esa secuencia el mismo registro** (mismo `record_hash`): si difiere, se
reescribió el prefijo (BR-NUC-57). Falla cerrado: una ruta ilegible, sin puntos de control, de una
cadena que no está en el paquete o con una secuencia que el paquete no contiene da `broken`.

## 6. Salida

| Código | Significado |
|---|---|
| 0 | Íntegro: todas las cadenas y todos los puntos de control anteriores coinciden |
| 1 | Roto, o el paquete (o un punto de control anterior) no se puede leer |
| 2 | Orden incorrecta (argumento desconocido, ruta inexistente, `--out` no escribible) |

El JSON de `--out` lleva `status`, `package_error`, una entrada por cadena (`status`,
`from_genesis`, secuencias, `entries`, `checkpoints_verified`, `last_checkpoint_sequence`,
`entries_after_last_checkpoint`, `broken` con `sequence`, `entry_id`, `reason`, `message` y
`line`), `previous_checkpoints`, `first_broken` y `unlisted_files`. Los `reason` son
identificadores en inglés; los `message`, su explicación en español.
