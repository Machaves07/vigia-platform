# Métricas del programa (TASK-321)

`ProgramMetricsPort.snapshot(context, zone_id, period)` sirve la misma instantánea que
SUP-U05-30. El llamador del puerto puede ser una tarea de sistema de su organización; una sesión
necesita `metrics.read` sobre la zona. La ruta acepta exactamente `period` y uno de `zone_id` o
`plant_id`. La planta devuelve sus zonas declaradas en ese periodo, no una dimensión personal.

- Cierres de ingeniería: registros de cierre del periodo, de hallazgos del sistema, con acción
  vigente de ingeniería y evidencia registrada. No se confunde el estado incerrable con un cierre.
- Activación: compuerta de **uso** aprobada al corte / zona declarada (0/1 o 1/1 por instantánea).
- Adopción: consumo del contador definido por BR-LAZ-134 y VIG-216: suma de actividades del lazo
  por zona y día local. Es un **conteo**, no una tasa de personas ni una razón inventada. El
  antiguo `ratio` del sustituto de `monthly` no era una definición aritmética normativa.
- Clasificación en plazo: cohorte por primera recepción, booleano derivado de la primera
  clasificación y el calendario/plazo vigentes cuando nació el hallazgo. Se reconstruye desde
  la historia de configuración, no desde sus valores actuales. Sin cohorte, no calculable.
- Vencidos: estado al corte, con subconjunto estrictamente mayor de 30 días de calendario.
- Reincidencia: episodios del periodo tras un cierre de acción de la misma zona y familia en el
  mes anterior. Un episodio cuenta una vez aunque coincida con varios cierres o tenga varios tramos.
- Exposición: consume `ExposureService.indicator`, conservando estado parcial y motivo. Si el
  puerto no dispone de sus insumos, no calculable con motivo, nunca cero ni una cifra parcial oculta.

El pliegue histórico evita contar estados o reclasificaciones posteriores al periodo. Los
incerrables quedan aparte en `excluded_unclosable_count`. No se lee auditoría para la adopción.

## Caché y recuperación

Una sola tabla existente, sin migración. La huella incluye secuencia de la cadena de planta,
conjunto canónico de zonas (la zona de la instantánea), periodo, contador operativo y punto/estado
de la proyección que consume exposición. La huella se lee antes de los insumos: una escritura
concurrente obliga al siguiente lector a invalidar, nunca deja una huella nueva para datos viejos.
La validez de 15 minutos depende solo de `Clock`; al cerrar, la tarea sustituye el recálculo por
el definitivo, incluso con la misma huella.

La clave única existente y `ON CONFLICT DO UPDATE ... WHERE` excluyen materializaciones repetidas
de una huella/ventana, sin candados adicionales ni escrituras de cadena. Un recálculo iniciado
antes del cierre nunca pisa un definitivo, aunque llegue después con otra huella. Una caída
antes de COMMIT revierte; después de COMMIT, el reintento reutiliza lo confirmado. La tarea diaria
recupera todos los meses cerrados desde el alta de cada zona, con su calendario local.

`monthly` consume el puerto en el manifiesto firmado, el registro v1 (sus campos compatibles)
y `metric_snapshot_id`; el manifiesto conserva además motivos y el subconjunto de vencidos.
