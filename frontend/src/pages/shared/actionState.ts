/**
 * El estado que se muestra de una acción correctiva, compartido por el lazo (SCR-09, RV-13) y el
 * detalle del hallazgo (SCR-08, RV-12; TASK-584).
 *
 * El cierre firmado (T09) cita la acción vigente y no reescribe su fila, que sigue `assigned` u
 * `overdue` (U-04, VIG-233; diagnóstico de VIG-363): con el hallazgo `closed`, la acción vigente
 * se presenta cerrada con el hallazgo. El servidor declara el vencimiento; `counts_as_overdue`
 * solo indica si entra en su cómputo, y nada se deduce de `due_at`, que puede venir agregado por
 * privacidad en zonas unipersonales (BR-LAZ-06).
 *
 * O-16 (recorrido de cierre M10): cada acción nombra a su responsable con el nombre visible que da
 * el servidor (`assignee_display_name`), nunca con su identificador; sin nombre (la cuenta no es
 * visible), «Responsable no disponible».
 */
import { text } from '@/texts';

/** Los cuatro estados de `corrective_action.state` que tienen etiqueta en cada pantalla. */
export const ACTION_STATES: readonly string[] = ['assigned', 'overdue', 'closed', 'superseded'];

/** Estados de la fila de la acción vigente (no sustituida) mientras el lazo sigue abierto. */
const LIVE_ACTION_STATES: readonly string[] = ['assigned', 'overdue'];

export function shownActionState(state: string, lifecycleState: string): string {
  if (lifecycleState === 'closed' && LIVE_ACTION_STATES.includes(state)) return 'closed';
  return state;
}

function named(name: string | null): name is string {
  return name !== null && name.trim() !== '';
}

/** El nombre del responsable que se muestra; sin nombre, «Responsable no disponible». */
export function assigneeName(name: string | null): string {
  return named(name) ? name : text('finding_journey.assignee_unavailable');
}

/** «Responsable: <nombre>» con la etiqueta del catálogo; sin nombre, «Responsable no disponible». */
export function assigneeLine(name: string | null): string {
  return named(name)
    ? `${text('finding_journey.assignee')}: ${name}`
    : text('finding_journey.assignee_unavailable');
}

/**
 * La acción vigente del lazo: la última asignada (orden del servidor) que no está sustituida ni
 * cerrada; `null` si no hay ninguna. Una reapertura deja la acción anterior `assigned` (el cierre
 * no reescribe su fila) y asigna otra después: la vigente es la última.
 */
function currentAction<T extends { readonly state: string }>(actions: readonly T[]): T | null {
  for (let index = actions.length - 1; index >= 0; index -= 1) {
    const action = actions[index];
    if (action !== undefined && LIVE_ACTION_STATES.includes(action.state)) return action;
  }
  return null;
}

/** El responsable de la acción vigente para la tarjeta «Siguiente paso»; `null` sin acción. */
export function currentAssignee(
  actions: readonly { readonly state: string; readonly assignee_display_name: string | null }[],
): { readonly name: string | null } | null {
  const current = currentAction(actions);
  return current === null ? null : { name: current.assignee_display_name };
}
