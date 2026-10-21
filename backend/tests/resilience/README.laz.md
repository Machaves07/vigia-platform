# Resiliencia de U-04 (LC-LAZ-42, TASK-329 y TASK-352)

Archivos para el trabajo `resiliencia` de `nightly.yml` (la edición del workflow corresponde a
TASK-333/VIG-249; hoy el trabajo ya corre `tests/resilience` entero con el perfil `nightly`):

- `test_fs_laz_01.py` a `test_fs_laz_12.py`;
- `test_dr_laz_01_02.py` y `test_dr_laz_03_04.py`;
- apoyo: `laz_support.py` (dobles y exportador en memoria de las métricas de U-04) y
  `laz_consumers.py` (los seis consumidores sobre el despachador real).

## FS-LAZ-07 a 12 y DR-LAZ-03/04 (TASK-352)

- Tiempos del reloj inyectado, nunca de pared: el `sleep` del asistente avanza el reloj simulado
  (60 s por intento, 5 y 20 s de retroceso, tope duro de 120 s); los ocho intentos de U-02 hasta
  la cola muerta se recorren avanzando el reloj 661 s por ronda del despachador.
- Métricas leídas de un exportador en memoria (`LoopMetricsExport`, `metrics_with_reader`): margen
  del proveedor, presupuesto de fichas, solicitudes y duración del asistente y
  `dead_letter_created_total`. Las alarmas que evalúa CloudWatch se reproducen con sus umbrales y
  su número de evaluaciones de `infra/stacks/loop.py` (`consecutive_breaches`). La tasa de
  rebotes de FS-LAZ-12 se calcula sobre `email_message`: `email_messages_total` aún no tiene
  emisor (VIG-270).
- `laz_consumers.consumer_case(nombre)` registra el manejador real de cada consumidor con su
  función de registro de producción, envuelto en `Outage` (fallo no declarado, sin efecto, que
  consume intentos), y reprocesa con la orden real `vigia-admin replay-dead-letter`.
- DR-LAZ-04: la versión anterior se restaura retirando las versiones posteriores (LocalStack no
  copia una versión sobre su misma clave); sin versión anterior, la lectura se declara ilegible.
  La declaración estructurada «no recuperable» en el hallazgo y el manifiesto es VIG-274.
- Perfil `nightly`: más rondas (FS-LAZ-07, 10 y 11), más eventos por partición (FS-LAZ-08, 09 y
  DR-LAZ-03) y más destinatarios limpios frente al tope (FS-LAZ-12).

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
