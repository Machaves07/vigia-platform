/**
 * Los valores de proceso de un tramo (A-129, TASK-595): `RecordOut.process_context` del detalle
 * del hallazgo, `EpisodePhotoOut.process_context` de SCR-06 y su resumen sin serie en el hito de
 * detección de la ficha de vida (`TimelineMilestoneOut.process`). Los pinta `ProcessValues`
 * (TASK-596) desde la lectura que cada pantalla ya hace: no hay lectura propia.
 */
import type { components } from '@/client/schema';

/** El bloque del tramo: ventana relativa al inicio del tramo y una entrada por variable. */
export type ProcessContext = components['schemas']['ProcessContextOut'];
/** Una variable con su valor en el instante del hecho, mínimo, máximo, serie y cambios. */
export type ProcessVariable = components['schemas']['ProcessVariableOut'];
/** Una variable en el hito de detección: el valor en el instante del hecho, sin la serie. */
export type ProcessVariableSummary = components['schemas']['ProcessVariableSummaryOut'];
