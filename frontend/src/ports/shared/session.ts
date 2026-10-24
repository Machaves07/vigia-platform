/**
 * Puerto de la sesión (LC-APP-06, IF-04 y IF-05), compartido por el armazón y SCR-01: `GET /me`
 * y `POST /auth/logout`, ambos `fixed` (U-02).
 *
 * Ninguna de las dos operaciones tiene clave de permiso: basta una sesión. `GET /me` no lleva
 * parámetros: la interfaz nunca envía `role_in_use` (BR-NUC-16).
 */
import type { components } from '@/client/schema';

import { defineScreenPort } from '../base';

/** Respuesta de `GET /me` tal como la define `app.yaml` (`MeResponse` de U-02). */
export type MeResponse = components['schemas']['MeResponse'];

/** Marcador de las rutas de sesión, que el servidor sirve a cualquier sesión válida. */
const SESSION_ROUTE = 'session';

export const sessionPort = defineScreenPort('SCR-01', {
  me: { kind: 'read', status: 'fixed', operationId: 'me_me_get', permission: SESSION_ROUTE },
  logout: {
    kind: 'write',
    status: 'fixed',
    operationId: 'logout_auth_logout_post',
    permission: SESSION_ROUTE,
  },
});
