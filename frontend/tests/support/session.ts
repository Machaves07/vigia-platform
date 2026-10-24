/**
 * Apoyo de las pruebas de `state.session` (VIG-300): respuestas sintéticas de `GET /me` con las
 * marcas de vencimiento que fija el servidor (BR-NUC-25). Sin datos de personas reales.
 */
import type { MeResponse } from '@/ports/shared';

export const USER_A = '0190a1b2-0000-7000-8000-00000000000a';
export const USER_B = '0190a1b2-0000-7000-8000-00000000000b';

/** Instante ISO 8601 UTC con milisegundos y `Z`, como `format_timestamp` de U-02. */
function iso(ms: number): string {
  return new Date(ms).toISOString();
}

export function meResponse(options: {
  readonly idleExpiresAt: number;
  readonly absoluteExpiresAt: number;
  readonly userId?: string;
  readonly permissions?: readonly string[];
}): MeResponse {
  return {
    user: {
      user_id: options.userId ?? USER_A,
      display_name: 'Persona sintética',
      email: 'persona@ejemplo.test',
    },
    organization: {
      organization_id: '0190a1b2-0000-7000-8000-0000000000f1',
      code: 'ORG-SINT',
      name: 'Organización sintética',
      kind: 'client',
    },
    assignments: [
      {
        role: 'administrator',
        scope_level: 'organization',
        scope_id: '0190a1b2-0000-7000-8000-0000000000f1',
      },
    ],
    effective_permissions: [...(options.permissions ?? ['findings.read'])],
    concession_id: null,
    idle_expires_at: iso(options.idleExpiresAt),
    absolute_expires_at: iso(options.absoluteExpiresAt),
    privacy_notice_version: 'v1',
    api_version: '1.0.0',
    professional_license: null,
    second_factor_enrolled: true,
  };
}
