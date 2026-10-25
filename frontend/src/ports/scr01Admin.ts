/**
 * Puerto de SCR-01, parte de cuenta y administración de usuarios (IF-02 paso 4 e IF-04; RV-01 y
 * RV-02; VIG-305). Todas las operaciones son `fixed` de U-02:
 *
 * - Cuenta (rutas de sesión, sin clave propia): `GET /auth/sessions`, `POST
 *   /auth/sessions/close-others` y `POST /auth/password`. Las sesiones llegan sin identificador ni
 *   hash (`SessionView`): la interfaz no tiene con qué nombrar una sesión concreta.
 * - Usuarios (`users.manage`): `GET/POST /users`, `PATCH /users/{user_id}`, desactivar,
 *   reactivar y restablecer el segundo factor. Roles (`roles.manage`): asignar y retirar.
 *
 * Las incompatibilidades de roles y el último administrador los decide el servidor y llegan como
 * `conflict` anclado (`writeNotice`); la pantalla no replica ninguna regla (BR-APP-43). Las
 * políticas de consulta de `GET /users` y `GET /auth/sessions` están en `state.query`.
 */
import assignBody from '../../openapi/bodies/assign_role_users__user_id__roles_post.json';
import grant from '../../openapi/bodies/grant_provider_concessions_post.json';
import type { components } from '@/client/schema';
import { reasonLimits } from '@/components/ReasonField/limits';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

/** Rutas con sesión sin clave propia (`SessionRoute` de U-02). */
const SESSION_ROUTE = 'session';

export type AccountSession = components['schemas']['SessionView'];
export type ManagedUser = components['schemas']['ManagedUserView'];
export type UserAssignment = components['schemas']['UserAssignmentView'];
export type AssignmentBody = components['schemas']['AssignmentBody'];
export type InviteBody = components['schemas']['InviteBody'];
export type Invitation = components['schemas']['InvitationView'];
export type Role = components['schemas']['Role'];
export type ScopeLevel = components['schemas']['ScopeLevel'];
export type Plant = components['schemas']['PlantView'];

export const adminPort = defineScreenPort('SCR-01', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: SESSION_ROUTE,
  },
  sessions: {
    kind: 'read',
    status: 'fixed',
    operationId: 'sessions_auth_sessions_get',
    permission: SESSION_ROUTE,
  },
  closeOthers: {
    kind: 'write',
    status: 'fixed',
    operationId: 'close_others_auth_sessions_close_others_post',
    permission: SESSION_ROUTE,
  },
  changePassword: {
    kind: 'write',
    status: 'fixed',
    operationId: 'change_password_auth_password_post',
    permission: SESSION_ROUTE,
  },
  users: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_users_users_get',
    permission: 'users.manage',
  },
  invite: {
    kind: 'write',
    status: 'fixed',
    operationId: 'invite_users_post',
    permission: 'users.manage',
  },
  updateProfile: {
    kind: 'write',
    status: 'fixed',
    operationId: 'update_profile_users__user_id__patch',
    permission: 'users.manage',
  },
  deactivate: {
    kind: 'write',
    status: 'fixed',
    operationId: 'deactivate_users__user_id__deactivate_post',
    permission: 'users.manage',
  },
  reactivate: {
    kind: 'write',
    status: 'fixed',
    operationId: 'reactivate_users__user_id__reactivate_post',
    permission: 'users.manage',
  },
  resetSecondFactor: {
    kind: 'write',
    status: 'fixed',
    operationId: 'reset_second_factor_users__user_id__second_factor_reset_post',
    permission: 'users.manage',
  },
  assignRole: {
    kind: 'write',
    status: 'fixed',
    operationId: 'assign_role_users__user_id__roles_post',
    permission: 'roles.manage',
  },
  removeRole: {
    kind: 'write',
    status: 'fixed',
    operationId: 'remove_role_users__user_id__roles__assignment_id__delete',
    permission: 'roles.manage',
  },
});

export function adminClock() {
  return currentTransport().clock;
}

/**
 * Límites del motivo del restablecimiento del segundo factor. `POST
 * /users/{user_id}/second-factor/reset` no tiene cuerpo en `app.yaml`, así que no publica los
 * suyos: se usan los del único motivo de U-02, el de la concesión (`GrantBody.reason`), leídos
 * del esquema generado y nunca copiados (BR-APP-44).
 */
export const RESET_REASON_LIMITS = reasonLimits(grant, 'reason');

/**
 * Todos los valores de `Role` y `ScopeLevel`, leídos del esquema generado de `POST
 * /users/{user_id}/roles` y nunca copiados. La pantalla ofrece todos los roles: cuáles admite cada
 * organización (p. ej. `platform_operator` solo en la proveedora) lo decide el servidor (BR-APP-43).
 */
export const ASSIGNABLE_ROLES: readonly Role[] = assignBody.body.$defs.Role.enum.filter(
  (value): value is Role => typeof value === 'string',
);

export const SCOPE_LEVELS: readonly ScopeLevel[] = assignBody.body.$defs.ScopeLevel.enum.filter(
  (value): value is ScopeLevel => typeof value === 'string',
);

/** Una página de usuarios con el cursor opaco del servidor (`after`). */
export async function readUsers(
  cursor: Readonly<Record<string, string>> | null,
  pageSize: number,
  signal: AbortSignal,
) {
  const page = await adminPort.users.read(
    {
      query: { limit: pageSize, ...(cursor?.after === undefined ? {} : { after: cursor.after }) },
    },
    signal,
  );
  return {
    items: page.users,
    nextCursor: page.next_after === null ? null : { after: page.next_after },
  };
}

/** `contract_tag` y `app_version` publicados en `/version.json` (metadatos, no de `app.yaml`). */
export interface ReleaseInfo {
  readonly app_version: string | null;
  readonly contract_tag: string | null;
}

function textField(body: unknown, key: string): string | null {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return null;
  const value = (body as Record<string, unknown>)[key];
  return typeof value === 'string' && value.trim() !== '' && value.length <= 64 ? value : null;
}

/**
 * Lee `/version.json` del mismo origen para la sección de versiones de la cuenta (PAT-APP-MAN-01).
 * Una respuesta sin las claves o malformada deja `null`: la pantalla muestra «no disponible».
 */
export async function readRelease(signal: AbortSignal): Promise<ReleaseInfo> {
  const response = await globalThis.fetch(new URL('/version.json', globalThis.location.origin), {
    cache: 'no-store',
    credentials: 'same-origin',
    signal,
  });
  if (!response.ok) return { app_version: null, contract_tag: null };
  const body: unknown = await response.json().catch(() => null);
  return {
    app_version: textField(body, 'app_version'),
    contract_tag: textField(body, 'contract_tag'),
  };
}

/** Versión de la aplicación incrustada en la construcción. */
export function embeddedAppVersion(): string {
  return import.meta.env.VITE_APP_VERSION;
}

/**
 * Aviso del servidor para un formulario: los `invalid_request` y `conflict` (comportamiento
 * `anchored`) se anclan al campo `form` con el mensaje del servidor; cualquier otra respuesta del
 * servidor va a la pila global (`field: null`). `null` si el fallo no es una respuesta HTTP.
 */
export function writeNotice(error: Error | null, id: string): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const anchored = error.failure.behaviour.kind === 'anchored';
  return { id: `scr01:${id}`, error: error.failure.error, field: anchored ? 'form' : null };
}

/** El fallo local del puerto (cuerpo que no cumple su esquema, sin conexión), si lo hay. */
export function localFailure(error: Error | null): 'invalid_input' | 'unavailable' | null {
  if (error === null) return null;
  if (!isPortError(error)) return 'unavailable';
  if (error.failure.kind === 'invalid_input') return 'invalid_input';
  if (error.failure.kind === 'http') return null;
  return 'unavailable';
}

/** Estado de pantalla ante un fallo de lectura. */
export type ReadFailure =
  | { readonly state: 'unavailable' }
  | { readonly state: 'not_available_in_scope' }
  | { readonly state: 'error'; readonly correlationId?: string };

export function readFailure(error: Error): ReadFailure {
  if (!isPortError(error)) return { state: 'error' };
  const failure = error.failure;
  if (failure.kind !== 'http') return { state: 'unavailable' };
  if (failure.behaviour.kind === 'not_available_in_scope')
    return { state: 'not_available_in_scope' };
  if (failure.behaviour.kind === 'transient' || failure.behaviour.kind === 'degraded_section')
    return { state: 'unavailable' };
  const body = failure.error.body;
  const correlation =
    typeof body === 'object' && body !== null && 'correlation_id' in body
      ? body.correlation_id
      : undefined;
  return typeof correlation === 'string'
    ? { state: 'error', correlationId: correlation }
    : { state: 'error' };
}
