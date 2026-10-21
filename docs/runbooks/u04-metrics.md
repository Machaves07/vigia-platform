# Métricas operativas de U-04

`loop.observability.metrics.LOOP_CATALOG` conserva los nombres, tipos y atributos publicados.
El colector mantiene `service` y `environment`; nunca publica usuario, destinatario, texto libre
ni dimensión de zona. Los hallazgos se agregan por organización y planta, y el detalle por zona
se consulta en la bandeja (NFR-GOB-13).

## Fuentes

- El monitor de `vigia-worker`, con la misma cadencia que `OutboxAgeMonitor`, invoca
  `LoopWorkerTelemetry.report_once`: descubre organizaciones activas y abre transacciones
  separadas con `ScopeContext` de sistema U-04 y RLS. Cuenta sin clasificar (`received`,
  `in_review`), su antigüedad desde `occurred_at_exact` y vencidos según el panel de plazos
  (`overdue_since`, o el plazo sin declaración). Incluye plantas vacías y consumidores sin
  pendientes con cero. No depende de tráfico ni de los filtros/páginas de una consulta HTTP.
- Los pendientes incluyen `pending` y `retrying`, aunque todavía no venza el próximo intento;
  se suman sobre todas las organizaciones activas para cada consumidor de U-04. Una lectura
  incompleta no publica un total global parcial. Un fallo de muestreo no se representa como cero.
- El despachador observa la entrada en cola muerta **después de confirmar** la transacción;
  el planificador observa duración/resultado por iteración y la edad del último éxito persistido.
  Las tareas sin éxito aún no tienen punto de antigüedad (la alarma conserva `MISSING`).
- `export_requests_total` cuenta observaciones de transición: admisión `requested`, intento
  adquirido `in_progress`, y finalización confirmada `ready` o `failed`. Una reentrega ya
  terminada no cuenta otro intento. `expired` es un estado de lectura derivado del reloj,
  no una nueva solicitud/transición persistida. La duración usa el reloj monótono inyectado,
  desde adquirir hasta cerrar/liberar el intento; un intento interrumpido sigue `in_progress`.
  El tamaño solo se observa tras confirmar un paquete `ready`.
- `export_stuck_or_failed` se recalcula por organización: solicitud abierta con más de 60 min
  desde la admisión, tercer intento tras dos fallos,
  o segundo intento fallido/liberado o con arrendamiento vencido. A los 60 min exactos o durante
  un segundo intento todavía vigente no alarma por esos motivos. Una composición recuperada
  y cerrada `ready` o `failed` retira la condición: los fallos terminales los observa
  `export_failed_total` mediante la alarma `export-failed`, no este gauge. Así, un fallo histórico
  no enclava `export-stuck` ni impide notificar un atasco posterior, sin borrar ninguna fila.
- El correo observa `sent` en SES, `bounced`/`complained` al procesar sus avisos, `digested`
  por lote de agrupación/resumen generado y `degraded_app_only` por aviso degradado. Estos son
  contadores operativos de observaciones, no una garantía transaccional/exactamente una vez:
  una reentrega del proveedor o un intento que luego se revierte puede volver a contar.
  Las unidades dependen del estado:

  | `email_state` | Unidad de cada incremento |
  |---|---|
  | `sent` | Un mensaje marcado como enviado por SES |
  | `bounced` / `complained` | Un aviso de rebote / queja procesado del proveedor |
  | `digested` | Un lote de agrupación/resumen generado, aunque contenga varios avisos |
  | `degraded_app_only` | Un aviso degradado; un lote con N candidatos incrementa N |

  «Correo por estado» separa esas unidades en series distintas: no sumar los estados como si
  fueran un total de mensajes ni calcular con ellos tasas de entrega o degradación.
- Cada validador observa su veredicto al ejecutar su interfaz de validación; las rutas exentas
  no cuentan. Ni el término ni su `term_id`, el texto o el patrón coincidente son dimensiones.

## Paneles y alarmas

Los cinco paneles siguen en `vigia-pilot`. Estado del lazo añade sin clasificar, antigüedad y
vencidos por planta y pendientes por consumidor; Trabajos diferidos añade estados, duración y
tamaño; Correo añade los cinco estados. Asistente y Salud de la proyección conservan sus fuentes;
las tareas de U-04 leen sus métricas canónicas.

`loop-dead-letter`, `export-stuck` y `periodic-task-stale-*` vigilan las métricas canónicas;
`export-failed` conserva el contador del hecho único ya existente. La tasa de rebotes sigue en
SES: el contador de observaciones del backend no es su denominador. Los umbrales y ventanas
no cambian: `export-stuck` ahora compara el flag `>= 1` en dos evaluaciones, en vez de comparar
la edad sustituta con 3600 s.

Los gauges son muestras periódicas, no decisiones de negocio ni una auditoría. Se validan con
datos sintéticos e `InMemoryMetricReader`; no se afirma un despliegue real en AWS (A-47).
