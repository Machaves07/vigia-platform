/**
 * Flujo por pasos puro (LC-APP-10, BR-APP-45, PR-APP-13).
 *
 * Un paso por etapa; `position` va de 0 a `stepCount`, y `position === stepCount` es el resumen.
 * - Solo se completa o invalida el paso en pantalla; avanzar exige el paso actual completo y
 *   saltar hacia delante exige completos todos los anteriores al destino.
 * - «Enviar» solo desde el resumen y con todos los pasos completos.
 * - «Guardar sin terminar» solo existe con `serverProgressRef` (estado «en curso» del servidor:
 *   causa raíz en U-04, walk-test en U-03); reanudar desde el servidor parte de esa referencia.
 * - Mientras hay un envío o un guardado pendiente, el flujo no cambia hasta su resultado.
 *
 * Invariante: todo paso anterior a `position` está completo. El flujo no depende del tiempo.
 */

type WizardPending = 'submit' | 'save_partial';

export interface StepWizardState {
  readonly stepCount: number;
  readonly position: number;
  readonly completed: readonly boolean[];
  readonly serverProgressRef: string | null;
  readonly pending: WizardPending | null;
  readonly submitted: boolean;
}

export type StepWizardEvent =
  | { readonly type: 'complete_step'; readonly index: number }
  | { readonly type: 'invalidate_step'; readonly index: number }
  | { readonly type: 'next' }
  | { readonly type: 'back' }
  | { readonly type: 'go_to'; readonly index: number }
  | { readonly type: 'submit' }
  | { readonly type: 'save_partial' }
  /** El servidor creó o confirmó el estado «en curso». */
  | { readonly type: 'server_progress'; readonly ref: string }
  /** Reanudación: el servidor guarda completos los pasos `0 … completedSteps − 1`. */
  | { readonly type: 'resume'; readonly ref: string; readonly completedSteps: number }
  /** Resultado de la operación pendiente. */
  | { readonly type: 'settled'; readonly ok: boolean };

function isRef(ref: string): boolean {
  return ref.trim() !== '';
}

export function createStepWizard(
  stepCount: number,
  serverProgressRef: string | null = null,
): StepWizardState {
  if (!Number.isSafeInteger(stepCount) || stepCount < 1) {
    throw new RangeError('stepCount debe ser un entero positivo.');
  }
  return {
    stepCount,
    position: 0,
    completed: Array.from({ length: stepCount }, () => false),
    serverProgressRef:
      serverProgressRef !== null && isRef(serverProgressRef) ? serverProgressRef : null,
    pending: null,
    submitted: false,
  };
}

export function isSummary(state: StepWizardState): boolean {
  return state.position === state.stepCount;
}

function allCompleteBefore(state: StepWizardState, index: number): boolean {
  return state.completed.slice(0, index).every(Boolean);
}

function isIdle(state: StepWizardState): boolean {
  return state.pending === null && !state.submitted;
}

export function canAdvance(state: StepWizardState): boolean {
  return isIdle(state) && !isSummary(state) && state.completed[state.position] === true;
}

export function canGoTo(state: StepWizardState, index: number): boolean {
  return (
    isIdle(state) &&
    Number.isSafeInteger(index) &&
    index >= 0 &&
    index <= state.stepCount &&
    allCompleteBefore(state, index)
  );
}

export function canSubmit(state: StepWizardState): boolean {
  return isIdle(state) && isSummary(state) && allCompleteBefore(state, state.stepCount);
}

export function canSavePartial(state: StepWizardState): boolean {
  return isIdle(state) && state.serverProgressRef !== null;
}

function setCompleted(state: StepWizardState, index: number, value: boolean): StepWizardState {
  if (!isIdle(state) || index !== state.position || isSummary(state)) return state;
  if (state.completed[index] === value) return state;
  const completed = state.completed.slice();
  completed[index] = value;
  return { ...state, completed };
}

export function stepWizardReducer(state: StepWizardState, event: StepWizardEvent): StepWizardState {
  switch (event.type) {
    case 'complete_step':
      return setCompleted(state, event.index, true);
    case 'invalidate_step':
      return setCompleted(state, event.index, false);
    case 'next':
      return canAdvance(state) ? { ...state, position: state.position + 1 } : state;
    case 'back':
      return isIdle(state) && state.position > 0
        ? { ...state, position: state.position - 1 }
        : state;
    case 'go_to':
      return canGoTo(state, event.index) ? { ...state, position: event.index } : state;
    case 'submit':
      return canSubmit(state) ? { ...state, pending: 'submit' } : state;
    case 'save_partial':
      return canSavePartial(state) ? { ...state, pending: 'save_partial' } : state;
    case 'server_progress':
      return isRef(event.ref) && !state.submitted
        ? { ...state, serverProgressRef: event.ref }
        : state;
    case 'resume': {
      if (!isIdle(state) || !isRef(event.ref) || !Number.isSafeInteger(event.completedSteps)) {
        return state;
      }
      const done = Math.min(Math.max(event.completedSteps, 0), state.stepCount);
      return {
        ...state,
        serverProgressRef: event.ref,
        completed: state.completed.map((_, index) => index < done),
        position: done,
      };
    }
    case 'settled':
      if (state.pending === null) return state;
      return {
        ...state,
        pending: null,
        submitted: state.pending === 'submit' && event.ok,
      };
  }
}
