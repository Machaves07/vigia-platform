/**
 * `Principal` (domain-entities §2.2): la identidad y los permisos efectivos que devuelve
 * `GET /me` (U-02), con los vencimientos de la sesión que calcula el servidor (pendiente nº 5).
 *
 * - `effective_permissions` se copia tal como llega: nunca se deriva de `assignments` ni de los
 *   roles (BR-APP-01, BR-NUC-13). Es la única fuente del mapa de navegación.
 * - `professional_license` y `second_factor_enrolled` son el aditivo de U-05 (VIG-278); si una
 *   respuesta no los trae, quedan ausentes.
 * - Una marca de vencimiento que no es un instante ISO 8601 con zona hace fallar la lectura: sin
 *   `Principal` no se construye navegación (PAT-APP-RES-07).
 */
import type { MeResponse } from '@/ports/shared';

export interface Principal {
  readonly user_id: string;
  readonly display_name: string;
  readonly email: string;
  readonly organization: {
    readonly organization_id: string;
    readonly code: string;
    readonly name: string;
    readonly kind: string;
  };
  readonly assignments: readonly {
    readonly role: string;
    readonly scope_level: string;
    readonly scope_id: string;
  }[];
  readonly effective_permissions: readonly string[];
  readonly concession_id: string | null;
  /** Vencimiento por inactividad que informó el servidor, en ms de la época. */
  readonly idle_expires_at: number;
  /** Vencimiento absoluto que informó el servidor, en ms de la época. */
  readonly absolute_expires_at: number;
  readonly privacy_notice_current_version: string;
  readonly api_version: string;
  readonly professional_license?: string | null;
  readonly second_factor_enrolled?: boolean;
  /** Instante (`Clock`) en que llegó la respuesta. */
  readonly fetched_at: number;
}

const INSTANT = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d{1,6})?(?:Z|[+-]\d{2}:\d{2})$/;

/** Un instante ISO 8601 con zona, en ms de la época; cualquier otra cosa es un error. */
function parseInstant(value: unknown, field: string): number {
  const ms = typeof value === 'string' && INSTANT.test(value) ? Date.parse(value) : Number.NaN;
  if (!Number.isFinite(ms)) {
    throw new TypeError(`state.session: «${field}» de GET /me no es un instante válido.`);
  }
  return ms;
}

/** El `Principal` de una respuesta de `GET /me` recibida en `fetchedAt`. */
export function principalFromMe(me: MeResponse, fetchedAt: number): Principal {
  const optional = me as Partial<
    Pick<MeResponse, 'professional_license' | 'second_factor_enrolled'>
  >;
  return Object.freeze({
    user_id: me.user.user_id,
    display_name: me.user.display_name,
    email: me.user.email,
    organization: Object.freeze({ ...me.organization }),
    assignments: Object.freeze(
      me.assignments.map((assignment) =>
        Object.freeze({
          role: assignment.role,
          scope_level: assignment.scope_level,
          scope_id: assignment.scope_id,
        }),
      ),
    ),
    effective_permissions: Object.freeze([...me.effective_permissions]),
    concession_id: me.concession_id,
    idle_expires_at: parseInstant(me.idle_expires_at, 'idle_expires_at'),
    absolute_expires_at: parseInstant(me.absolute_expires_at, 'absolute_expires_at'),
    privacy_notice_current_version: me.privacy_notice_version,
    api_version: me.api_version,
    ...(optional.professional_license === undefined
      ? {}
      : { professional_license: optional.professional_license }),
    ...(optional.second_factor_enrolled === undefined
      ? {}
      : { second_factor_enrolled: optional.second_factor_enrolled }),
    fetched_at: fetchedAt,
  });
}
