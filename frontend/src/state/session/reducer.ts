/**
 * Reductor puro de `UiSession` (LC-APP-06, domain-entities §2.1, diagrama de IF-04). La interfaz
 * no conoce la cookie (BR-NUC-25): solo modela lo que le dicen las respuestas. Toda marca sale
 * del evento (`nowMs`, del `Clock` inyectado) o del servidor (`Principal`): el reductor no lee la
 * hora.
 *
 * - Ventana de inactividad: empieza en cada respuesta autenticada (`last_request_at`). Su
 *   vencimiento es `idle_expires_at` de `GET /me` o, tras cualquier otra respuesta,
 *   `respuesta + 30 min`, que es lo que el servidor acaba de fijar (BR-NUC-25).
 * - Aviso de inactividad 2 min antes del vencimiento (a los 28 min de la última respuesta),
 *   **una vez por ventana** (`idle_warning_issued`); «seguir trabajando» es `GET /me` (BR-APP-14).
 *   `sessionTransition` devuelve el aviso emitido junto al estado: otro `tick` en la misma
 *   ventana no lo repite.
 * - Aviso absoluto 10 min antes de `absolute_expires_at`; ninguna respuesta lo prolonga ni lo
 *   retira (BR-APP-15).
 * - `unauthenticated` o un vencimiento pasan a `locked_for_reauth`, `expired_idle` o
 *   `expired_absolute` guardando la ruta (BR-APP-16); reautenticar (`principal_loaded`) vuelve a
 *   `active` con la ruta, y con el borrador solo si es la misma persona.
 * - `privacy_notice_required` pasa a `privacy_notice_pending` guardando la ruta (IF-03).
 * - `logout` vuelve al estado inicial: nada sobrevive (BR-APP-17).
 */
import type { Principal } from './principal';

export type UiSessionStatus =
  | 'anonymous'
  | 'second_factor_pending'
  | 'privacy_notice_pending'
  | 'active'
  | 'expiring_soon'
  | 'expired_idle'
  | 'expired_absolute'
  | 'locked_for_reauth';

/** Ventana de inactividad del servidor (BR-NUC-25). */
export const IDLE_TIMEOUT_MS = 30 * 60_000;
/** Antelación del aviso de inactividad: a los 28 min (BR-APP-14) `[objetivo propio]`. */
export const IDLE_WARNING_BEFORE_MS = 2 * 60_000;
/** Antelación del aviso absoluto (BR-APP-15) `[objetivo propio]`. */
export const ABSOLUTE_WARNING_BEFORE_MS = 10 * 60_000;

export interface UiSession {
  readonly status: UiSessionStatus;
  readonly principal: Principal | null;
  /** Última respuesta autenticada (`Clock`). */
  readonly last_request_at: number | null;
  /** Fin de la ventana de inactividad en curso. */
  readonly idle_deadline_at: number | null;
  /** `absolute_expires_at` del servidor. */
  readonly absolute_deadline_at: number | null;
  /** Aviso de inactividad ya emitido en esta ventana (`warning_issued`). */
  readonly idle_warning_issued: boolean;
  /** Aviso absoluto ya emitido en esta sesión. */
  readonly absolute_warning_issued: boolean;
  /** Ruta a la que volver tras reautenticar o aceptar el aviso. */
  readonly return_route: string | null;
  /** Persona cuya sesión se bloqueó: solo ella recupera el borrador. */
  readonly locked_user_id: string | null;
  readonly second_factor_enrollment_required: boolean;
}

export type UiSessionEvent =
  /** El inicio de sesión pide el segundo factor (IF-01, paso 2b). */
  | { readonly type: 'second_factor_required'; readonly enrollment_required: boolean }
  /** `GET /me` respondió: inicio, reautenticación, aviso aceptado o «seguir trabajando». */
  | { readonly type: 'principal_loaded'; readonly principal: Principal; readonly nowMs: number }
  /** Cualquier otra respuesta autenticada: el servidor renovó la inactividad. */
  | { readonly type: 'response'; readonly nowMs: number }
  /** Paso del tiempo; `route` es la ruta actual, por si la sesión vence. */
  | { readonly type: 'tick'; readonly nowMs: number; readonly route: string }
  | { readonly type: 'unauthenticated'; readonly route: string }
  | { readonly type: 'privacy_notice_required'; readonly route: string }
  /** El enrutador ya volvió a `return_route`. */
  | { readonly type: 'return_route_consumed' }
  | { readonly type: 'logout' };

export const initialSession: UiSession = Object.freeze({
  status: 'anonymous',
  principal: null,
  last_request_at: null,
  idle_deadline_at: null,
  absolute_deadline_at: null,
  idle_warning_issued: false,
  absolute_warning_issued: false,
  return_route: null,
  locked_user_id: null,
  second_factor_enrollment_required: false,
});

/** Con sesión utilizable: las respuestas renuevan la ventana y corren los avisos. */
export function isAuthenticated(status: UiSessionStatus): boolean {
  return status === 'active' || status === 'expiring_soon';
}

/** Sesión perdida que se recupera reautenticando en la misma pestaña. */
export function needsReauth(status: UiSessionStatus): boolean {
  return (
    status === 'locked_for_reauth' || status === 'expired_idle' || status === 'expired_absolute'
  );
}

/** `active` o `expiring_soon` según los avisos vigentes. */
function withWarnings(session: UiSession): UiSession {
  const status =
    session.idle_warning_issued || session.absolute_warning_issued ? 'expiring_soon' : 'active';
  return session.status === status ? session : { ...session, status };
}

/** Pierde la sesión guardando la ruta (la primera, si ya había una). */
function lose(session: UiSession, status: UiSessionStatus, route: string): UiSession {
  return {
    ...session,
    status,
    return_route: session.return_route ?? route,
    locked_user_id: session.locked_user_id ?? session.principal?.user_id ?? null,
    idle_warning_issued: false,
  };
}

/** Siguiente instante en que el reductor cambiaría con un `tick`, o `null`. */
export function nextDeadline(session: UiSession): number | null {
  if (!isAuthenticated(session.status)) return null;
  const candidates: number[] = [];
  const idle = session.idle_deadline_at;
  const absolute = session.absolute_deadline_at;
  if (idle !== null) {
    candidates.push(idle);
    if (!session.idle_warning_issued) candidates.push(idle - IDLE_WARNING_BEFORE_MS);
  }
  if (absolute !== null) {
    candidates.push(absolute);
    if (!session.absolute_warning_issued) candidates.push(absolute - ABSOLUTE_WARNING_BEFORE_MS);
  }
  return candidates.length === 0 ? null : Math.min(...candidates);
}

function loaded(session: UiSession, principal: Principal, nowMs: number): UiSession {
  const sameUser = session.locked_user_id === null || session.locked_user_id === principal.user_id;
  // La misma sesión del servidor: misma persona y mismo vencimiento absoluto.
  const sameSession =
    session.principal?.user_id === principal.user_id &&
    session.absolute_deadline_at === principal.absolute_expires_at;
  return withWarnings({
    status: 'active',
    principal,
    last_request_at: nowMs,
    idle_deadline_at: principal.idle_expires_at,
    absolute_deadline_at: principal.absolute_expires_at,
    // Una ventana nueva solo si el servidor la prolongó.
    idle_warning_issued:
      sameSession &&
      session.idle_deadline_at !== null &&
      principal.idle_expires_at <= session.idle_deadline_at &&
      session.idle_warning_issued,
    absolute_warning_issued: sameSession && session.absolute_warning_issued,
    return_route: sameUser ? session.return_route : null,
    locked_user_id: null,
    second_factor_enrollment_required: false,
  });
}

/** Aviso que la interfaz muestra (UI-05, VIG-320). */
export type SessionNotice = 'idle_warning' | 'absolute_warning';

export interface SessionTransition {
  readonly session: UiSession;
  /** Avisos que emite este evento: cada uno, una sola vez por ventana o por sesión. */
  readonly notices: readonly SessionNotice[];
}

const NO_NOTICES: readonly SessionNotice[] = Object.freeze([]);

function tick(session: UiSession, nowMs: number, route: string): SessionTransition {
  if (!isAuthenticated(session.status)) return { session, notices: NO_NOTICES };
  const { idle_deadline_at: idle, absolute_deadline_at: absolute } = session;
  if (absolute !== null && nowMs >= absolute) {
    return { session: lose(session, 'expired_absolute', route), notices: NO_NOTICES };
  }
  if (idle !== null && nowMs >= idle) {
    return { session: lose(session, 'expired_idle', route), notices: NO_NOTICES };
  }
  let next = session;
  const notices: SessionNotice[] = [];
  if (!next.idle_warning_issued && idle !== null && nowMs >= idle - IDLE_WARNING_BEFORE_MS) {
    next = { ...next, idle_warning_issued: true };
    notices.push('idle_warning');
  }
  if (
    !next.absolute_warning_issued &&
    absolute !== null &&
    nowMs >= absolute - ABSOLUTE_WARNING_BEFORE_MS
  ) {
    next = { ...next, absolute_warning_issued: true };
    notices.push('absolute_warning');
  }
  return { session: withWarnings(next), notices };
}

/** El estado siguiente y los avisos que emite (solo un `tick` emite avisos). */
export function sessionTransition(session: UiSession, event: UiSessionEvent): SessionTransition {
  if (event.type === 'tick') return tick(session, event.nowMs, event.route);
  return { session: sessionReducer(session, event), notices: NO_NOTICES };
}

export function sessionReducer(session: UiSession, event: UiSessionEvent): UiSession {
  switch (event.type) {
    case 'second_factor_required':
      if (session.status !== 'anonymous' && !needsReauth(session.status)) return session;
      return {
        ...session,
        status: 'second_factor_pending',
        second_factor_enrollment_required: event.enrollment_required,
      };
    case 'principal_loaded':
      return loaded(session, event.principal, event.nowMs);
    case 'response': {
      if (!isAuthenticated(session.status)) return session;
      const deadline = event.nowMs + IDLE_TIMEOUT_MS;
      const renewed = session.idle_deadline_at === null || deadline > session.idle_deadline_at;
      return withWarnings({
        ...session,
        last_request_at: Math.max(session.last_request_at ?? event.nowMs, event.nowMs),
        idle_deadline_at: renewed ? deadline : session.idle_deadline_at,
        idle_warning_issued: renewed ? false : session.idle_warning_issued,
      });
    }
    case 'tick':
      return tick(session, event.nowMs, event.route).session;
    case 'unauthenticated':
      if (
        isAuthenticated(session.status) ||
        session.status === 'privacy_notice_pending' ||
        session.status === 'expired_idle' ||
        session.status === 'expired_absolute'
      ) {
        return lose(session, 'locked_for_reauth', event.route);
      }
      return session;
    case 'privacy_notice_required':
      if (session.status === 'privacy_notice_pending') return session;
      return {
        ...session,
        status: 'privacy_notice_pending',
        return_route: session.return_route ?? event.route,
        idle_warning_issued: false,
      };
    case 'return_route_consumed':
      return session.return_route === null ? session : { ...session, return_route: null };
    case 'logout':
      return initialSession;
  }
}
