/**
 * El estado que se muestra de una acción correctiva, compartido por el lazo (SCR-09, RV-13) y el
 * detalle del hallazgo (SCR-08, RV-12; TASK-584).
 *
 * El cierre firmado (T09) cita la acción vigente y no reescribe su fila, que sigue `assigned` u
 * `overdue` (U-04, VIG-233; diagnóstico de VIG-363): con el hallazgo `closed`, la acción vigente
 * se presenta cerrada con el hallazgo. El servidor declara el vencimiento; `counts_as_overdue`
 * solo indica si entra en su cómputo, y nada se deduce de `due_at`, que puede venir agregado por
 * privacidad en zonas unipersonales (BR-LAZ-06).
 */

/** Los cuatro estados de `corrective_action.state` que tienen etiqueta en cada pantalla. */
export const ACTION_STATES: readonly string[] = ['assigned', 'overdue', 'closed', 'superseded'];

/** Estados de la fila de la acción vigente (no sustituida) mientras el lazo sigue abierto. */
const LIVE_ACTION_STATES: readonly string[] = ['assigned', 'overdue'];

export function shownActionState(state: string, lifecycleState: string): string {
  if (lifecycleState === 'closed' && LIVE_ACTION_STATES.includes(state)) return 'closed';
  return state;
}
