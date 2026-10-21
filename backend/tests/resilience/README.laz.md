# Resiliencia de U-04 (LC-LAZ-42, TASK-329)

Archivos para el trabajo `resiliencia` de `nightly.yml` (la edición del workflow corresponde a
TASK-333/VIG-249):

- `test_fs_laz_01.py` a `test_fs_laz_06.py`;
- `test_dr_laz_01_02.py`.

Cada archivo tiene una variante pequeña que corre con `--hypothesis-profile=ci` y otra con la
marca `nightly`, seleccionable con `--hypothesis-profile=nightly -m nightly`. Para reconstrucción
se mutan 3/100 filas; el ensayo anual ingresa 13/365 muestras distribuidas en 365 días calendario
del reloj inyectado (renovando el certificado a mitad de año). Los otros escenarios conservan los
tiempos de fallo completos en ambos perfiles; la exportación abarca 31 días, con 6/16 MiB por
artefacto sintético y partes de 5 MiB (hay partes subidas de verdad antes de morir). La duración
frente a 1800 segundos de DR-LAZ-02 es únicamente tendencia A-65.

`harness.scenario` deja siempre el informe (también en rojo) en `reports/resilience` o
`VIGIA_RESILIENCE_REPORT_DIR`, con la semilla de la sesión y los resultados observados. Se reutiliza
la recolección de artefactos con retención de 90 días de los escenarios FS-GOB; no se modifica el
workflow en esta tarea. El resumen imprime la semilla necesaria para reproducir cada escenario.

`ProcessGroup.start_laz_case` configura exclusivamente `tests.resilience.worker_process` mediante
`VIGIA_TEST_LAZ_CASE`. Los casos del proveedor y del correo construyen los servicios reales sobre
PostgreSQL en el hijo y comparten un reloj simulado con los dobles; los únicos puertos externos
guionizados son el proveedor y el correo. No se añaden ganchos a producción. El caso de exportación
usa el worker real, arrendamientos PostgreSQL, `ExportStream` y S3 de LocalStack; el contenido del
compositor es sintético, no una prueba de completitud del paquete funcional. SIGKILL se emite tras
persistir el 40/80 %; otro proceso solo puede retomar después del límite de 60 segundos. Se abortan
las subidas anteriores, se vuelve a empezar por el documento y se compara el ZIP con una ejecución
sin interrupción del mismo trabajo fijado.

La entrega selectiva omite solo la aplicación del consumidor: no borra eventos ni registros.
Una entrega aún abierta también puede disparar la verificación conservadora de VIG-269; esta suite
no añade una lectura privilegiada de `shared.outbox_delivery`. Los ensayos no alteran las reglas
RLS ni los permisos de producción.
