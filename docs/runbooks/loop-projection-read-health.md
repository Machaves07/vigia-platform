# Salud de lectura de la proyección del lazo (VIG-273, DR-LAZ-01)

La primera lectura de la bandeja compara el checkpoint de cada planta consultada con las
secuencias de los eventos `finding_received` y `detection_for_review_received`, publicados en la
misma transacción que sus registros del expediente. No lee el expediente ni las cargas de los
eventos, no invoca puertos de otras unidades y mantiene una sola sentencia y un mismo snapshot
para salud, filas y declaraciones.

Si existe una secuencia mayor que `last_applied_ledger_sequence` (cero si falta el checkpoint),
la planta se declara `projection_diverged` y **no se sirve ninguna de sus filas**. Las marcas
persistidas `rebuilding` y `diverged` también impiden servir filas; `rebuilding` tiene prioridad
sobre el atraso. Las plantas sanas siguen disponibles, y las declaraciones de zona se conservan.
La declaración derivada no escribe salud, no incrementa el contador de incidentes y no dispara
una reconstrucción desde una petición de lectura.

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

Esta guarda prueba **atraso de secuencia**, no equivalencia completa: no detecta huecos anteriores
a una secuencia ya aplicada, eventos de enriquecimiento sin secuencia de expediente ni corrupción
de filas con checkpoint al día. La verificación diaria y la reconstrucción siguen siendo necesarias
para esos casos. Las escrituras del lazo de U-04 actualizan su proyección en la misma transacción;
esta guarda se ocupa de la frontera asíncrona de la ingesta.

Regresión reproducible con copia física PostgreSQL 16 y lectura `vigia_app` + `ScopeContext`:

```text
cd backend
uv run pytest -q --hypothesis-profile=ci --hypothesis-seed=3407339539 tests/resilience/test_dr_laz_01_02.py -k restored
```

La prueba no modifica marcas de salud. Tras reconstruir, contrasta el digest con una ejecución
incremental independiente del origen y comprueba que aparecen los dos hallazgos.
