/**
 * La lectura de una señal por rol de un tramo del hallazgo (`signals[]` de `RecordOut`),
 * compartida por SCR-09 (la clasificación, TASK-587) y SCR-08 (el detalle, TASK-584), que la
 * pintan con `SignalReadings`. Viene del mismo detalle auditado: no hay lectura propia.
 */
import type { components } from '@/client/schema';

/** La señal tal como la devuelve `GET /loop/findings/{id}` (rol, valor, válida y hora de lectura). */
export type SignalReading =
  components['schemas']['vigia_platform__loop__adapters__http__findings_routes__SignalOut'];
