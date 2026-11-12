# Fuente de la semilla Pope (VIG-394 / TASK-547)

- Repositorio: `Machaves07/vigia-nodo`.
- Commit: `a04e6f3795ab57fad4efe6a3d0a55dea37f4b7e0`.
- Ruta de origen: `tests/fixtures/pope/`.
- Copias byte a byte, sin transformar el JSON:

| Archivo | SHA-256 del original |
|---|---|
| `zone_catalog.json` | `05f15ddfebd858b5c9275fa66b1c925a055a0195cf89703024788bd9cf5fdb3e` |
| `zone_catalog.pope_foso.json` | `845f78b702b0caf11a6a0fa09bb6d98df576071798e350fcce5b79015fd4baa2` |

Su procedencia y límites están en `CATALOGO.md` y `README.md` de esa carpeta del repositorio
de origen. Son datos sintéticos; no se copian imágenes, encuadres reales ni registros del prototipo.
La gramática es v1.1.0: Q5 no distingue el estado del jumbo, el arranque no puede condicionarse
por resguardo y las puertas sin dato no se pueden representar como señales sin canal. Esta
semilla no corrige ni traduce esas reglas y no autoriza su comparación con el prototipo.

Cuando el catálogo Pope pase a la gramática v1.2.0 (A-77), se vuelve a copiar y se actualiza el hash. La plataforma ya acepta esa gramática: consume `vigia-contracts` v1.4.0 desde TASK-571.

## Operación

Desde `backend/`:

```sh
uv run python -m tools.seed_pope --dry-run --catalog tools/pope/zone_catalog.json
uv run python -m tools.seed_pope --base-url https://app.integracion.example.test \
  --catalog tools/pope/zone_catalog.json --plant <uuid> --concession <uuid>
```

La segunda orden requiere `VIGIA_POPE_ADMIN_SESSION` y `VIGIA_POPE_INSTALLER_SESSION`, valores
de las cookies de sesiones ya autenticadas (segundo factor verificado) de administradora e
instalador del entorno creado por `local/integration/bootstrap.sh`. No son las contraseñas del
bootstrap: el operador inicia sesión por el flujo normal. No se reciben secretos por argumentos
ni se leen archivos de credenciales. Para una CA local se admite `--ca-bundle <ruta>`; nunca se
desactiva TLS. No redirigir stdout ni usar `tee`: el código de alta aparece solo una vez allí.

La planta debe existir y estar concedida al instalador. Las cuatro admisiones se evalúan con
respuestas afirmativas **solo para esta integración sintética**. Se usa el `zone_code` original
(`POPE`, `POPE-FOSO`), no se inventan códigos con guiones bajos que la plataforma no admite.
`CAM100`, `CAM101`, `CAM102` se enlazan a referencias locales `cam100`, `cam101`, `cam102`;
no se leen ni almacenan direcciones RTSP. cam102 conserva su identidad en ambas zonas (A-57).

La API crea la primera versión mediante `/standards` con `zone_parameters`; los cambios de
parámetros posteriores van por `/cameras`, `/minimum-coverage`, `/signals`, `/thresholds` y
`/windows`. Los estándares no tienen `code` en esta API: su clave de reanudación es el título
declarado; las zonas y el nodo sí se buscan por `code`. Un estándar cambiado no se sobrescribe.
La comparación RFC 8785 reutiliza `rfc8785`, ya dependencia del proyecto. Excluye exclusivamente
`zone_id`, `plant_id`, `organization_id`, `version`, `issued_at` del catálogo y `standard_id`,
`version`, `declared_by`, `effective_from` de cada estándar: todos los asigna la plataforma.
No excluye ids de cámaras ni señales, políticas, texto, orden ni campos desconocidos.

El acta de alcance de SCR-05 usa un PNG opaco generado en memoria, sin personas; se sube por
la concesión HTTP normal, con suma y tamaño comprobados por la plataforma. El texto explicita
que **no acredita el montaje real de planta**. Solo aprueba montaje para integración: deja uso
pendiente y ambas zonas en `commissioning`. El walk-test y el acuerdo de uso quedan fuera.

## Reejecución y fallo cerrado

- Ejecutar **una semilla a la vez**. No se añade una garantía de exclusión entre operadores.
- La segunda ejecución no publica versiones, actas, documentos ni códigos nuevos. Las lecturas
  autenticadas siguen renovando sesiones y dejando la auditoría normal de la plataforma.
- La API no expone metadatos de códigos vigentes. Por prudencia, un nodo ya existente **nunca**
  obtiene automáticamente otro código, ni siquiera si venció o se interrumpió la primera ejecución
  antes de mostrarlo. Si hace falta, el operador lo reemite explícitamente por SCR-07 / la ruta
  normal de códigos de alta. No se añade almacenamiento local ni lectura directa de la base.
- Un rechazo HTTP, incluida cobertura insatisfacible (A-03), termina con salida 1 sin seguir
  sembrando. Las operaciones previas confirmadas permanecen: HTTP no ofrece una transacción
  global. Se puede reanudar por sus claves estables; no se borra ni revierte el expediente.
- Una compuerta revocada no se reaprueba y un uso ya decidido exige revisión humana.
- `--dry-run` solo lee y valida ambos JSON e imprime el plan: ni HTTP ni escrituras locales.

## Pruebas

El marcador del repositorio se llama `integration` (no `integracion`) y no está instalado xdist:

```sh
VIGIA_NODO_SRC=/ruta/al/clon/vigia-nodo uv run pytest \
  tests/integration/test_seed_pope.py -q -m integration --basetemp=/tmp/t394
```

Las pruebas usan testcontainers y la composición real de API en proceso, reutilizando el arnés
de conformidad. La ruta de consola `/zones/{id}/catalog` devuelve la carga; su sobre guardado
se consulta mediante `/catalog/versions/{version}`. Ambos se contrastan y el alta simulada
comprueba el mismo sobre en `initial_configuration`. No se cambia ninguna ruta de producto.
