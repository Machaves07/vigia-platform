/**
 * «Siguiente paso» de cada estado del hallazgo (sistema visual v1, principio 2; VIG-365): una sola
 * acción principal, la que permite el estado que da el servidor (BR-APP-43), y las demás como
 * secundarias. No es una regla propia: `tests/components/next_step.test.tsx` contrasta cada acceso
 * por igualdad con la tabla de transiciones de U-04 (`lifecycle` de
 * `backend/openapi/u05-fixtures.json`):
 *
 * - `classify`: primera clasificación (T03 y T04, desde `received`);
 * - `review_queue`: resolución en la cola (T05 y T06, desde `in_review`);
 * - `action`: comprometer la acción (T07);
 * - `closure`: evidencia y cierre firmado (T09);
 * - `unclosable`: declarar que no hay alternativa segura (T10);
 * - `unclosable_review`: revisar la declaración en la fecha comprometida (T11 y T12);
 * - `reopening`: reabrir (T13 y T14);
 * - `reclassify`: reclasificar con motivo reforzado (T15 a T18 y T20).
 *
 * Omisiones deliberadas (decisión del dueño en VIG-365): el panel ofrece lo que pide cada estado,
 * no todo lo que la tabla admite. En lazo abierto, comprometer la acción y reclasificar; con acción
 * o vencida, evidencia y cierre y declarar sin alternativa segura; un incerrable, solo su revisión;
 * un lazo cerrado, solo la reapertura. Lo demás (reclasificar con la acción en curso, reabrir un
 * incerrable) sigue en «Clasificación del hallazgo» y en el lazo. El servidor decide al registrar:
 * ninguna escritura se deshabilita aquí.
 */
export type StepAccess =
  | 'classify'
  | 'review_queue'
  | 'action'
  | 'closure'
  | 'unclosable'
  | 'unclosable_review'
  | 'reopening'
  | 'reclassify';

export interface NextStep {
  /** La acción principal del estado; `null` si no queda nada pendiente. */
  readonly primary: StepAccess | null;
  /** Las demás que admite el estado, en «Más acciones». */
  readonly secondary: readonly StepAccess[];
}

export const NEXT_STEP: Readonly<Record<string, NextStep>> = {
  received: { primary: 'classify', secondary: [] },
  in_review: { primary: 'review_queue', secondary: [] },
  open: { primary: 'action', secondary: ['reclassify'] },
  action_assigned: { primary: 'closure', secondary: ['unclosable'] },
  overdue: { primary: 'closure', secondary: ['unclosable'] },
  unclosable: { primary: 'unclosable_review', secondary: [] },
  closed: { primary: 'reopening', secondary: [] },
  suspended_by_reclassification: { primary: 'reclassify', secondary: [] },
  discarded: { primary: null, secondary: ['reclassify'] },
};

const NONE: NextStep = { primary: null, secondary: [] };

/** `true` si `state` está en la lista cerrada de `finding_lifecycle_state`. */
export function isKnownState(state: string): boolean {
  return Object.hasOwn(NEXT_STEP, state);
}

/** El siguiente paso del estado; ninguno si el estado no está en la lista cerrada. */
export function nextStep(state: string): NextStep {
  return Object.hasOwn(NEXT_STEP, state) ? (NEXT_STEP[state] ?? NONE) : NONE;
}
