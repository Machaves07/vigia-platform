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
   Interpretación de «periodo siguiente» de BR-LAZ-113: el mes calendario siguiente, no una
   ventana móvil desde cada cierre; los episodios del mismo mes del cierre no cuentan. Pendiente
   de confirmación del dueño; no se cambia la definición en esta ronda.
- Exposición: consume `ExposureService.indicator`, conservando estado parcial y motivo. Si el
  puerto no dispone de sus insumos, no calculable con motivo, nunca cero ni una cifra parcial oculta.

El pliegue histórico evita contar estados o reclasificaciones posteriores al periodo. Los
incerrables quedan aparte en `excluded_unclosable_count`. No se lee auditoría para la adopción.

## Caché y recuperación

Una sola tabla existente, sin migración. La huella incluye secuencia de la cadena de planta,
conjunto canónico de zonas (la zona de la instantánea), periodo, contador operativo y punto/estado
de la proyección que consume exposición. La huella se lee antes de los insumos: una escritura
concurrente obliga al siguiente lector a invalidar, nunca deja una huella nueva para datos viejos.
La validez de 15 minutos y la invalidación por huella solo se aplican al mes en curso. Al cerrar,
la tarea sustituye el recálculo por el definitivo, incluso con la misma huella. Una vez escrito,
el definitivo se sirve antes de leer cabeza o insumos: el avance posterior de la cadena no lo
invalida. El upsert no reemplaza un definitivo ni siquiera por otro definitivo con distinta huella.

Las recepciones del mes se acotan por `chain_sequence <= head` y `received_at < period_end`,
no por `received_at <= Clock.now()`: el desfase entre los relojes de base y aplicación no omite
una recepción que ya contiene la huella. Los hechos fechados por la aplicación conservan su
corte histórico. Un retroceso de `Clock` también invalida
una caché con marca futura. Su comparación al guardar usa el mismo reloj, leído al guardar,
no la marca antigua de un cálculo lento (que no debe sobrescribir una caché más reciente).

La clave única existente y `ON CONFLICT DO UPDATE ... WHERE` excluyen materializaciones repetidas
de una huella/ventana, sin candados adicionales ni escrituras de cadena. Un recálculo iniciado
antes del cierre nunca pisa un definitivo, aunque llegue después con otra huella. Una caída
antes de COMMIT revierte; después de COMMIT, el reintento reutiliza lo confirmado. La tarea diaria
recupera todos los meses cerrados desde el alta de cada zona, con su calendario local.

`monthly` consume el puerto en el manifiesto firmado, el registro v1 (sus campos compatibles)
y `metric_snapshot_id`; el manifiesto conserva además motivos y el subconjunto de vencidos.

## Bordes y pendientes menores

- BR-LAZ-111: el generador incluye cierres con acción reemplazada y evidencia citada no conocida;
  ninguno cuenta. La guarda de incerrable es redundante con el pliegue del dominio: `Close`
  desde `unclosable` no es una transición válida y falla antes de devolver métricas. No se
  afirma una mutación detectada para esa redundancia.
- La tarea sigue fallando de forma visible ante una zona inconsistente: el ejecutor registra
  el fallo y reintenta; los definitivos ya confirmados se reutilizan. Una zona defectuosa puede
  impedir llegar a las siguientes de la organización. Aislar fallos por zona queda como pendiente
  menor explícito (sin ocultar errores ni ampliar aquí el catálogo de eventos).
- Las pruebas de integración inyectan el mismo `Clock` en la marca de recepción de su base
  desechable, sin reescribir filas ni quitar hashes/RLS. Reinician el reloj por prueba y ejecutan
  caché/cierre tanto a mitad de mes como a una hora del fin de mes local.
