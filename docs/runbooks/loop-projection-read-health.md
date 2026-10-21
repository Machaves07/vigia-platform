# Salud de lectura de la proyección del lazo (VIG-273, DR-LAZ-01)

La primera lectura de la bandeja compara `ingestion_frontier` de cada planta consultada con las
secuencias de los eventos `finding_received` y `detection_for_review_received`, publicados en la
misma transacción que sus registros del expediente. No usa `last_applied_ledger_sequence`: ese
máximo compartido puede saltar por encima de una ingesta pendiente cuando otro consumidor o una
escritura de U-04 aplica un registro posterior. No lee el expediente ni las cargas de los eventos,
no invoca puertos de otras unidades y mantiene una sola sentencia y un mismo snapshot para
salud, filas y declaraciones.

Si existe una secuencia mayor que `ingestion_frontier` (cero si falta el checkpoint),
la planta se declara `projection_diverged` y **no se sirve ninguna de sus filas**. Las marcas
persistidas `rebuilding` y `diverged` también impiden servir filas; `rebuilding` tiene prioridad
sobre el atraso. Las plantas sanas siguen disponibles, y las declaraciones de zona se conservan.
La declaración derivada no escribe salud, no incrementa el contador de incidentes y no dispara
una reconstrucción desde una petición de lectura.

## Frontera comprobada y acceso indexado

`laz_0040` añade una frontera inicialmente **cero**, sin confiar en máximos heredados. Después
de aplicar una ingesta, en la misma transacción y bajo el candado ya existente del checkpoint,
`LoopProjectionStore` comprueba el delta hasta el máximo compartido. Cada hallazgo debe figurar
en `episode.segment_record_ids` (incluidos los tramos tardíos); cada detección debe tener su fila
`finding_state`, también si ya está resuelta. El primer hueco detiene la frontera justo antes de
su secuencia. Si no hay huecos, certifica el delta entero. Ni una escritura de U-04 ni un evento de
enriquecimiento avanzan esta frontera por sí solos. Una detección o un hallazgo entregados fuera
de orden tampoco tapan el hueco de otro. La publicación bajo el candado de cadena impide que una
ingesta anterior se confirme después de una secuencia posterior ya certificada.

La reconstrucción certifica su prefijo completo **al intercambiar las filas**, en esa misma
transacción. Una copia física conserva juntas las filas y la frontera que las certifica.

El índice parcial `outbox_event_loop_ingestion (organization_id, plant_id, ledger_sequence)`
permite a cada lectura obtener solo la última secuencia de ingesta (barrido inverso con `LIMIT 1`)
y compararla fuera del subplan con la frontera. Así RLS no desplaza la comparación correlacionada
a un filtro que recorra todo el índice. No recorre la historia del outbox ni siquiera cuando toda
ella es de ingesta. El avance comprueba únicamente el delta no certificado con ese índice y las
claves de `episode` y `finding_state`. En la vía de escritura, el identificador del episodio se
obtiene del registro original por su clave (el evento no lo contiene), y la pertenencia del tramo
se comprueba en esa única fila. No se añade un GIN de arrays: su operador no es leakproof y RLS
impide usarlo como acceso indexado. Estas lecturas internas del expediente ocurren al aplicar la
proyección, **nunca en la bandeja**.
La primera aplicación tras migrar puede comprobar toda la historia de ingesta de su planta una
vez; si existe un hueco, las comprobaciones posteriores empiezan de nuevo en él. Un atraso grande
se recupera por reconstrucción, no por un barrido de la historia desde cada lectura.

La imagen N-1 sigue escribiendo y ejecutando sus tareas sobre el esquema aditivo. No conoce ni
avanza la frontera nueva; al volver a la imagen nueva, una reconstrucción certifica la historia.
Hasta entonces se aplica fallo cerrado si queda alguna ingesta sin certificar; no se presume
salud copiando el máximo viejo.

## Retraso incremental normal

Sin una señal externa, una copia física atrasada es indistinguible de un retraso incremental.
La política es conservadora: **no hay ventana de gracia**, ni siquiera durante la entrega normal.
Se declara indisponibilidad de esa partición hasta que la entrega alcance la marca o la
reconstrucción por `ProjectionRebuilder.rebuild_plant` termine. Tras restaurar, la reconstrucción
es el camino que recupera el estado desde el expediente; no hace falta esperar al worker diario
para proteger la primera lectura. No se presenta como sana una página parcial durante esa espera.

## Límites y aislamiento

No se cambia RLS, ningún rol ni permiso. `outbox_delivery` no se consulta: su limitación bajo
`vigia_app`, documentada por VIG-269, no se resuelve ni se elude aquí. La salud se calcula solo
para plantas con zonas consultadas dentro de `findings.read`, nunca se declara una planta ajena.
Si RLS oculta el checkpoint (por ejemplo bajo concesión de proveedor, A-69), la ausencia no es
prueba de salud: ante eventos de ingesta visibles se aplica el mismo fallo cerrado.

Esta guarda detecta ingestas aún no certificadas, incluidos los huecos tapados por un máximo
compartido posterior. No demuestra equivalencia completa: no detecta corrupción o borrado de filas
**posterior a su certificación**, ni eventos de enriquecimiento sin secuencia de expediente. La
verificación diaria y la reconstrucción siguen siendo necesarias para esos casos. Las escrituras
del lazo de U-04 actualizan su proyección en la misma transacción; esta guarda se ocupa de la
frontera asíncrona de la ingesta.

Regresión reproducible con copia física PostgreSQL 16 y lectura `vigia_app` + `ScopeContext`:

```text
cd backend
uv run pytest -q --hypothesis-profile=ci --hypothesis-seed=3407339539 tests/resilience/test_dr_laz_01_02.py -k restored
```

La prueba no modifica marcas de salud. Tras reconstruir, contrasta el digest con una ejecución
incremental independiente del origen y comprueba que aparecen los hallazgos y, en la variante
cruzada, la detección ya entregada a `review_queue`.
