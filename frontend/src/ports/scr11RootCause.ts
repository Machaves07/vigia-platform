/**
 * Puerto de SCR-11 (causa raíz con asistente, SUP-U05-16 a 20 de U-04). Las cinco operaciones
 * exigen `root_cause.analyze` sobre la zona del análisis; ninguna lectura es auditada.
 *
 * - `start` crea un análisis nuevo en cada llamada: la pantalla solo lo pide por una acción
 *   explícita de la persona. `503 loop_standard_text_unavailable` degrada la sección
 *   (diferencia 8 de VIG-251).
 * - `sign` responde `409 loop_transition_not_allowed` si el análisis ya está firmado
 *   (diferencia 9).
 * - `requestAssistant` y `assistantStatus` son la solicitud asíncrona y su consulta; un rechazo
 *   de los validadores llega como `400 loop_assistant_output_rejected` (A-68).
 */
import type { components } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
import type { HttpError } from './errors/map';

export type Analysis =
  components['schemas']['vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut'];
export type ZoneContext = components['schemas']['ZoneContextOut'];
export type AnalysisStepBody = components['schemas']['AnalysisStepBody'];
export type AnalysisSignatureBody = components['schemas']['AnalysisSignatureBody'];
export type AssistantRequest = components['schemas']['AssistantRequestOut'];

export const rootCausePort = defineScreenPort('SCR-11', {
  start: {
    kind: 'write',
    status: 'fixed',
    operationId: 'start_loop_findings__finding_state_id__root_cause_analyses_post',
    permission: 'root_cause.analyze',
  },
  putStep: {
    kind: 'write',
    status: 'fixed',
    operationId: 'put_step_loop_root_cause_analyses__analysis_id__steps__step_index__put',
    permission: 'root_cause.analyze',
  },
  sign: {
    kind: 'write',
    status: 'fixed',
    operationId: 'signature_loop_root_cause_analyses__analysis_id__signature_post',
    permission: 'root_cause.analyze',
  },
  requestAssistant: {
    kind: 'write',
    status: 'fixed',
    operationId: 'request_assistant_loop_root_cause_analyses__analysis_id__assistant_requests_post',
    permission: 'root_cause.analyze',
  },
  assistantStatus: {
    kind: 'read',
    status: 'fixed',
    operationId: 'assistant_status_loop_assistant_requests__request_id__get',
    permission: 'root_cause.analyze',
  },
  // GET /hierarchy no requiere clave de permiso: basta la sesión. Da la zona horaria de la planta.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
});

/** Zona horaria IANA de la planta de `zoneId`, o `null` si la jerarquía no la incluye. */
export function plantTimeZone(
  hierarchy: components['schemas']['HierarchyView'] | undefined,
  zoneId: string,
): string | null {
  const plant = hierarchy?.plants.find((p) => p.zones.some((z) => z.zone_id === zoneId));
  return plant?.timezone ?? null;
}

export function rootCauseClock() {
  return currentTransport().clock;
}

/** La respuesta HTTP de un fallo del puerto, si la hubo. */
export function httpErrorOf(error: unknown): HttpError | null {
  return isPortError(error) && error.failure.kind === 'http' ? error.failure.error : null;
}

export type { HttpError };

/**
 * El error que la pantalla presenta como `Notice` (UI-19): la respuesta HTTP si la hubo; si no,
 * el código local con el mismo comportamiento del mapa cerrado (BR-APP-71).
 */
export function noticeError(error: unknown): HttpError {
  const http = httpErrorOf(error);
  if (http !== null) return http;
  if (!isPortError(error)) return { code: 'internal_error', status: 0 };
  switch (error.failure.kind) {
    case 'version_outdated':
      return { code: 'version_outdated', status: 0 };
    case 'invalid_input':
      // El esquema espejo rechazó el cuerpo antes de enviarlo (BR-APP-64): aviso anclado genérico.
      return { code: 'invalid_request', status: 0 };
    default:
      return { code: 'temporarily_unavailable', status: 0 };
  }
}

/** Cómo terminó una petición fallida al asistente (la forma que espera UI-22). */
export type AssistantOutcome =
  | { readonly kind: 'rejected'; readonly detailCode: string | null; readonly body: unknown }
  | { readonly kind: 'unavailable'; readonly cause: string | null }
  | { readonly kind: 'aborted' };

const ASSISTANT_REJECTIONS: ReadonlySet<string> = new Set([
  'loop_assistant_output_rejected',
  'loop_blocked_vocabulary_term',
  'loop_person_attribution_detected',
]);

/**
 * Un rechazo de los validadores (`400 loop_assistant_output_rejected`, A-68) detiene la consulta
 * como `rejected_by_validators`; todo otro fallo (503 `loop_assistant_unavailable`, 429, sin
 * conexión, `not_found`) deja el asistente «no disponible» y la plantilla guiada sigue.
 */
export function assistantFailure(error: unknown): AssistantOutcome {
  if (isPortError(error) && error.failure.kind === 'aborted') return { kind: 'aborted' };
  const http = httpErrorOf(error);
  const detail = http?.detail_code ?? null;
  if (http !== null && detail !== null && ASSISTANT_REJECTIONS.has(detail)) {
    return { kind: 'rejected', detailCode: detail, body: http.body };
  }
  return { kind: 'unavailable', cause: null };
}

const CORRELATION = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;

/** `correlation_id` del cuerpo de error (`format: uuid`); cualquier otra cosa no se muestra. */
export function correlationIdOf(error: unknown): string | undefined {
  const body = httpErrorOf(error)?.body;
  const value =
    typeof body === 'object' && body !== null && !Array.isArray(body)
      ? (body as Record<string, unknown>).correlation_id
      : undefined;
  return typeof value === 'string' && CORRELATION.test(value) ? value : undefined;
}

export type RootCauseFailure =
  'standard_text_unavailable' | 'unavailable' | 'not_available_in_scope' | 'error';

/** Estado común de pantalla ante un fallo de `start`. */
export function rootCauseFailure(error: unknown): RootCauseFailure {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind !== 'http') return 'error';
  if (failure.error.detail_code === 'loop_standard_text_unavailable')
    return 'standard_text_unavailable';
  if (failure.behaviour.kind === 'not_available_in_scope') return 'not_available_in_scope';
  if (failure.behaviour.kind === 'degraded_section' || failure.behaviour.kind === 'transient')
    return 'unavailable';
  return 'error';
}
