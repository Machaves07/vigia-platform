/**
 * Puerto de SCR-01, parte de sesión (IF-01 a IF-04, VIG-321): inicio de sesión en dos pasos,
 * inscripción del segundo factor, aceptación del aviso de tratamiento y activación por invitación.
 * Todas son escrituras `fixed` de U-02; `GET /me` y `POST /auth/logout` están en el puerto
 * compartido de sesión (`ports/shared/session.ts`), con la política `identity` ya declarada en
 * `state.query`. Ninguna de estas escrituras pasa por la caché de consultas: el QR, el secreto y
 * los códigos de recuperación viven solo en el estado del componente que los muestra (BR-APP-12).
 *
 * `sessionFailure` clasifica cada rechazo en una lista cerrada que la página traduce a una sola
 * clave de texto: la interfaz nunca distingue correo desconocido, contraseña o código incorrecto
 * (BR-APP-10) ni enlace usado, vencido o inexistente (BR-APP-18), y nunca pinta `message_es`.
 */
import type { components } from '@/client/schema';

import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

/** Rutas públicas (`UnauthenticatedRoute` de U-02): no exigen sesión ni clave de permiso. */
const PUBLIC_ROUTE = 'public';
/** Ruta con sesión sin clave propia (`SessionRoute`). */
const SESSION_ROUTE = 'session';

export type LoginStatus = components['schemas']['LoginResponse']['status'];
export type EnrollmentView = components['schemas']['EnrollmentView'];
export type ActivationResponse = components['schemas']['ActivationResponse'];

export const sessionScreenPort = defineScreenPort('SCR-01', {
  login: {
    kind: 'write',
    status: 'fixed',
    operationId: 'login_auth_login_post',
    permission: PUBLIC_ROUTE,
  },
  secondFactor: {
    kind: 'write',
    status: 'fixed',
    operationId: 'second_factor_auth_second_factor_post',
    permission: PUBLIC_ROUTE,
  },
  enroll: {
    kind: 'write',
    status: 'fixed',
    operationId: 'enroll_auth_second_factor_enroll_post',
    permission: PUBLIC_ROUTE,
  },
  acceptInvitation: {
    kind: 'write',
    status: 'fixed',
    operationId: 'accept_invitations__token__accept_post',
    permission: PUBLIC_ROUTE,
  },
  acceptNotice: {
    kind: 'write',
    status: 'fixed',
    operationId: 'accept_notice_privacy_notice_accept_post',
    permission: SESSION_ROUTE,
  },
});

/** Reloj del transporte: las cuentas regresivas usan el mismo que las peticiones. */
export function sessionClock() {
  return currentTransport().clock;
}

/**
 * Rechazo clasificado:
 * - `invalid_credentials`: `unauthenticated` o `invalid_request` en el inicio de sesión y en el
 *   código (cualquier causa, BR-APP-10);
 * - `enrollment_required`: el segundo paso pide inscribir el factor (`second_factor_required`);
 * - `throttled`: cuenta regresiva con `retry_after_seconds` (BR-APP-11);
 * - `not_found`: enlace de invitación usado, vencido o inexistente (BR-APP-18);
 * - `privacy_notice_required`: falta aceptar el aviso vigente (IF-03);
 * - `unavailable`: sin conexión o tiempo de espera agotado;
 * - `error`: cualquier otro, con el `correlation_id` si lo hay.
 */
export type SessionFailure =
  | { readonly kind: 'invalid_credentials' }
  | { readonly kind: 'enrollment_required' }
  | { readonly kind: 'throttled'; readonly retryAfterSeconds: number | null }
  | { readonly kind: 'not_found' }
  | { readonly kind: 'privacy_notice_required' }
  | { readonly kind: 'unavailable' }
  | { readonly kind: 'error'; readonly correlationId?: string };

function correlationOf(body: unknown): string | undefined {
  if (typeof body !== 'object' || body === null || !('correlation_id' in body)) return undefined;
  return typeof body.correlation_id === 'string' ? body.correlation_id : undefined;
}

export function sessionFailure(error: unknown): SessionFailure {
  if (!isPortError(error)) return { kind: 'error' };
  const failure = error.failure;
  switch (failure.kind) {
    case 'offline':
    case 'unavailable':
    case 'timeout':
    case 'network':
      return { kind: 'unavailable' };
    // Estructura rechazada antes de enviar: la página ya valida lo mismo, así que es uniforme.
    case 'invalid_input':
      return { kind: 'invalid_credentials' };
    case 'http':
      break;
    default:
      return { kind: 'error' };
  }
  const { behaviour } = failure;
  switch (failure.error.code) {
    case 'unauthenticated':
    case 'invalid_request':
      return { kind: 'invalid_credentials' };
    case 'second_factor_required':
      return { kind: 'enrollment_required' };
    case 'not_found':
      return { kind: 'not_found' };
    case 'privacy_notice_required':
      return { kind: 'privacy_notice_required' };
    case 'throttled':
    case 'rate_limited':
      return {
        kind: 'throttled',
        retryAfterSeconds: behaviour.kind === 'countdown' ? behaviour.retry_after_seconds : null,
      };
    default: {
      const correlationId = correlationOf(failure.error.body);
      return correlationId === undefined ? { kind: 'error' } : { kind: 'error', correlationId };
    }
  }
}
