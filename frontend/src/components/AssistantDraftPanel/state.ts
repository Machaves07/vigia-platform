/**
 * Estado puro de UI-22 `AssistantDraftPanel` (BR-APP-34, 49; PR-APP-14; `AssistantRequest` de
 * `domain-entities.md` §2.19).
 *
 * - Pedir crea la solicitud; mientras está `pending` la interfaz consulta cada 5 s, con un tope
 *   de 2 min `[objetivo propio]` desde que el servidor la aceptó.
 * - `ready` (con borrador), `unavailable`, `rejected_by_validators`, cualquier fallo o el tope
 *   detienen la consulta. Ninguno de esos estados toca el flujo guiado: el panel nunca lo
 *   bloquea ni lo oculta.
 * - El rechazo se lee con la forma del diseño (`rejection: {code, term?, suggestion?}`) por
 *   lectura tolerante (U-04 lo da en `ApiErrorBody.rejection` desde VIG-344; una respuesta sin
 *   él, de una versión anterior, deja solo el código de `detail_code`) y la pantalla no inventa el
 *   término ni la sugerencia.
 *
 * Ningún estado depende del reloj del sistema: el instante llega en cada evento.
 */

/** Intervalo entre consultas (BR-APP-49). */
export const POLL_INTERVAL_MS = 5_000;
/** Tope de la consulta desde que el servidor aceptó la solicitud (BR-APP-49). */
export const POLL_LIMIT_MS = 120_000;

interface AssistantCitation {
  readonly kind: string;
  readonly id: string;
  readonly quoted_fact_es: string;
}

export interface AssistantDraft {
  readonly draft_text: string;
  readonly citations: readonly AssistantCitation[];
  readonly insufficient_evidence_declared: boolean;
  readonly proposed_cause_category: string | null;
  readonly model: string;
  readonly model_version: string;
}

/** Lo que el panel lee de `AssistantRequestOut` (SUP-U05-19 y 20). */
export interface AssistantRequestView {
  readonly request_id: string;
  readonly assistant_state: string;
  readonly unavailable_cause: string | null;
  readonly draft: AssistantDraft | null;
}

export type RejectionCode = 'blocked_vocabulary_term' | 'person_attribution_detected';

/** Rechazo de los validadores de U-04; sin término ni sugerencia si la respuesta no los trae. */
export interface AssistantRejection {
  readonly code: RejectionCode | null;
  readonly term: string | null;
  readonly suggestion: string | null;
}

/** Cómo terminó una petición fallida, según el mapa de errores de la pantalla. */
export type AssistantFailure =
  | { readonly kind: 'rejected'; readonly detailCode: string | null; readonly body: unknown }
  | { readonly kind: 'unavailable'; readonly cause: string | null }
  | { readonly kind: 'aborted' };

export type AssistantPanelState =
  | { readonly status: 'idle' }
  | { readonly status: 'requesting' }
  | { readonly status: 'polling'; readonly requestId: string; readonly startedAt: number }
  | { readonly status: 'ready'; readonly requestId: string; readonly draft: AssistantDraft }
  | {
      readonly status: 'unavailable';
      readonly cause: string | null;
      /** La consulta superó el tope de 2 min sin respuesta final. */
      readonly expired: boolean;
    }
  | { readonly status: 'rejected'; readonly rejection: AssistantRejection };

export type AssistantEvent =
  | { readonly type: 'request' }
  /** Respuesta 202 de la solicitud. */
  | { readonly type: 'accepted'; readonly response: AssistantRequestView; readonly now: number }
  /** Respuesta 200 de una consulta de estado. */
  | { readonly type: 'status'; readonly response: AssistantRequestView; readonly now: number }
  | { readonly type: 'failed'; readonly failure: AssistantFailure }
  | { readonly type: 'tick'; readonly now: number };

export const idleAssistant: AssistantPanelState = { status: 'idle' };

const MAX_REJECTION_TEXT = 200;

function record(value: unknown): Readonly<Record<string, unknown>> | undefined {
  return typeof value === 'object' && value !== null && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : undefined;
}

function rejectionText(value: unknown): string | null {
  return typeof value === 'string' && value.trim() !== '' && value.length <= MAX_REJECTION_TEXT
    ? value
    : null;
}

/** Código cerrado del rechazo, con o sin el prefijo `loop_` del `detail_code` (pendiente nº 33). */
export function rejectionCode(value: unknown): RejectionCode | null {
  if (typeof value !== 'string') return null;
  const code = value.startsWith('loop_') ? value.slice('loop_'.length) : value;
  return code === 'blocked_vocabulary_term' || code === 'person_attribution_detected' ? code : null;
}

/**
 * El rechazo de una respuesta (`rejection` del diseño en el cuerpo) con el `detail_code` como
 * respaldo del código. El término y la sugerencia se copian tal cual llegan, o quedan en `null`.
 */
export function rejectionOf(body: unknown, detailCode: string | null): AssistantRejection {
  const rejection = record(record(body)?.rejection);
  return {
    code: rejectionCode(rejection?.code) ?? rejectionCode(detailCode),
    term: rejectionText(rejection?.term),
    suggestion: rejectionText(rejection?.suggestion),
  };
}

/** Se puede pedir (otra vez) mientras no haya una solicitud en curso. */
export function canRequest(state: AssistantPanelState): boolean {
  return state.status !== 'requesting' && state.status !== 'polling';
}

function expired(state: AssistantPanelState, now: number): boolean {
  return state.status === 'polling' && now - state.startedAt >= POLL_LIMIT_MS;
}

/** El estado al que lleva una respuesta del servidor. */
function fromResponse(response: AssistantRequestView, now: number): AssistantPanelState {
  switch (response.assistant_state) {
    case 'pending':
      return { status: 'polling', requestId: response.request_id, startedAt: now };
    case 'ready':
      return response.draft === null
        ? { status: 'unavailable', cause: null, expired: false }
        : { status: 'ready', requestId: response.request_id, draft: response.draft };
    case 'rejected_by_validators':
      return { status: 'rejected', rejection: rejectionOf(response, null) };
    case 'unavailable':
      return { status: 'unavailable', cause: response.unavailable_cause, expired: false };
    default:
      // Un estado desconocido de una versión más nueva no prolonga la consulta.
      return { status: 'unavailable', cause: null, expired: false };
  }
}

export function assistantReducer(
  state: AssistantPanelState,
  event: AssistantEvent,
): AssistantPanelState {
  switch (event.type) {
    case 'request':
      return canRequest(state) ? { status: 'requesting' } : state;
    case 'accepted':
      return state.status === 'requesting' ? fromResponse(event.response, event.now) : state;
    case 'status': {
      if (state.status !== 'polling' || event.response.request_id !== state.requestId) return state;
      if (expired(state, event.now)) return { status: 'unavailable', cause: null, expired: true };
      if (event.response.assistant_state === 'pending') return state;
      return fromResponse(event.response, event.now);
    }
    case 'failed': {
      if (state.status !== 'requesting' && state.status !== 'polling') return state;
      const { failure } = event;
      // Una consulta cancelada se repite en el siguiente ciclo; una solicitud nunca se cancela.
      if (failure.kind === 'aborted') {
        return state.status === 'polling'
          ? state
          : { status: 'unavailable', cause: null, expired: false };
      }
      if (failure.kind === 'rejected') {
        return { status: 'rejected', rejection: rejectionOf(failure.body, failure.detailCode) };
      }
      return { status: 'unavailable', cause: failure.cause, expired: false };
    }
    case 'tick':
      return expired(state, event.now)
        ? { status: 'unavailable', cause: null, expired: true }
        : state;
  }
}
