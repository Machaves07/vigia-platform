/**
 * Borrador de formulario en memoria (LC-APP-10, `FormDraft` de domain-entities §2.9, BR-APP-16,
 * 19 y 20).
 *
 * El borrador vive solo en la memoria de la pestaña: este módulo no toca `localStorage`,
 * `sessionStorage` ni IndexedDB, y nada de lo que guarda se persiste (BR-APP-19).
 * - `dirty` marca cambios no enviados y activa la confirmación al abandonar (`useLeaveGuard`).
 * - Con `locked_for_reauth` el borrador se conserva (`preserved_on_lock`) y se recupera al
 *   reautenticar en la misma ruta; la escritura pendiente no se reenvía sola (BR-APP-16).
 * - `server_progress_ref` es el estado «en curso» del servidor, cuando existe.
 */

type FormValue = string | number | boolean | null | readonly string[];

export interface FormDraft {
  readonly screenId: string;
  readonly formId: string;
  readonly stepIndex: number;
  readonly values: Readonly<Record<string, FormValue>>;
  readonly dirty: boolean;
  readonly serverProgressRef: string | null;
  readonly preservedOnLock: boolean;
}

export type FormDraftEvent =
  | { readonly type: 'set_value'; readonly field: string; readonly value: FormValue }
  | { readonly type: 'set_step'; readonly index: number }
  | { readonly type: 'server_progress'; readonly ref: string }
  /** El servidor aceptó el envío o el guardado sin terminar: no quedan cambios pendientes. */
  | { readonly type: 'saved' }
  /** La sesión pasó a `locked_for_reauth`. */
  | { readonly type: 'lock' }
  /** Reautenticación en la misma ruta: el borrador vuelve tal como estaba. */
  | { readonly type: 'restore' }
  /** Descartar (abandono confirmado, otra ruta tras reautenticar, cambio de concesión). */
  | { readonly type: 'discard' };

export function createFormDraft(
  screenId: string,
  formId: string,
  serverProgressRef: string | null = null,
): FormDraft {
  return {
    screenId,
    formId,
    stepIndex: 0,
    values: {},
    dirty: false,
    serverProgressRef,
    preservedOnLock: false,
  };
}

export function formDraftReducer(draft: FormDraft, event: FormDraftEvent): FormDraft {
  switch (event.type) {
    case 'set_value': {
      if (
        Object.is(draft.values[event.field], event.value) &&
        Object.hasOwn(draft.values, event.field)
      ) {
        return draft;
      }
      return { ...draft, values: { ...draft.values, [event.field]: event.value }, dirty: true };
    }
    case 'set_step':
      return Number.isSafeInteger(event.index) && event.index >= 0
        ? { ...draft, stepIndex: event.index }
        : draft;
    case 'server_progress':
      return event.ref.trim() === '' ? draft : { ...draft, serverProgressRef: event.ref };
    case 'saved':
      return { ...draft, dirty: false };
    case 'lock':
      return { ...draft, preservedOnLock: true };
    case 'restore':
      return { ...draft, preservedOnLock: false };
    case 'discard':
      return createFormDraft(draft.screenId, draft.formId);
  }
}
