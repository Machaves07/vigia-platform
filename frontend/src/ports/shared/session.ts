/**
 * Puerto de la sesión (LC-APP-06, IF-04 y IF-05), compartido por el armazón y SCR-01: `GET /me`,
 * `POST /auth/logout` y `GET /hierarchy` (las plantas del selector de la cabecera, VIG-320), todos
 * `fixed` (U-02).
 *
 * Ninguna de las tres operaciones tiene clave de permiso: basta una sesión. `GET /me` no lleva
 * parámetros: la interfaz nunca envía `role_in_use` (BR-NUC-16).
 */
import type { components } from '@/client/schema';

import { defineScreenPort } from '../base';

/** Respuesta de `GET /me` tal como la define `app.yaml` (`MeResponse` de U-02). */
export type MeResponse = components['schemas']['MeResponse'];
/** Respuesta de `GET /hierarchy`, ya acotada por el servidor al alcance de la sesión. */
export type HierarchyView = components['schemas']['HierarchyView'];

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
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: SESSION_ROUTE,
  },
});
