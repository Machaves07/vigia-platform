/**
 * `ports.liveview` (LC-APP-18, PAT-APP-SEG-03, BR-APP-65): el puerto de la vista en vivo.
 *
 * Una sola operación, `POST /zones/{zone_id}/live-view-token` (`fixed` en `app.yaml`), que pide el
 * token de 600 s y la dirección local del nodo. La clave de permiso es `live_view.open` (SCR-06 y
 * SCR-16). El cuerpo de la respuesta es el `LiveViewHandoff` del diseño (`domain-entities.md`
 * §2.13): `token`, `live_view_local_url` y `expires_at`.
 *
 * Este módulo es el único que conoce la operación; el enlace se compone en `handoff.ts` y el
 * descarte del token, en el estado de `LiveViewLauncher`. Nunca pasa por la caché de consultas:
 * una copia en la caché de TanStack Query sobreviviría al cierre de la pantalla (PR-APP-29).
 */
import type { components } from '@/client/schema';

import { defineScreenPort, isPortError } from '../base';
import type { HttpError } from '../errors/map';

/** Respuesta de `POST /zones/{zone_id}/live-view-token` (`LiveViewTokenOut` de U-02). */
export type LiveViewHandoff = components['schemas']['LiveViewTokenOut'];

/**
 * El puerto. La pantalla de la vista en vivo es compartida por SCR-06 (comisionamiento) y SCR-16
 * (transparencia); el marco de `ports.base` exige una sola pantalla por puerto y esta es la
 * primera consumidora del diseño.
 */
export const liveViewPort = defineScreenPort('SCR-06', {
  requestToken: {
    kind: 'write',
    status: 'fixed',
    operationId: 'live_view_token_zones__zone_id__live_view_token_post',
    permission: 'live_view.open',
  },
});

export type LiveViewPort = typeof liveViewPort;

/** El error de la plataforma que la pantalla muestra, tal como lo espera el mapa cerrado. */
export type LiveViewError = HttpError;

/**
 * Traduce lo que rechazó el puerto al error que se presenta con `behaviourFor` (BR-APP-71).
 *
 * - `null` solo si el llamador canceló: no hay nada que mostrar.
 * - Un fallo sin respuesta (tiempo de espera, red o circuito abierto) se presenta como
 *   `temporarily_unavailable`, igual que en el resto de la aplicación (BR-APP-61); la escritura
 *   con el circuito `platform` abierto ni sale.
 * - Un error sin control se presenta como `internal_error` (nunca se muestra su detalle).
 */
export function liveViewFailure(error: unknown): LiveViewError | null {
  if (!isPortError(error)) return { code: 'internal_error', status: 0 };
  const failure = error.failure;
  switch (failure.kind) {
    case 'aborted':
      return null;
    case 'http':
      return failure.error;
    case 'version_outdated':
      return { code: 'version_outdated', status: 0 };
    case 'invalid_input':
      return { code: 'invalid_request', status: 422 };
    case 'unavailable':
    case 'offline':
    case 'timeout':
    case 'network':
      return { code: 'temporarily_unavailable', status: 0 };
  }
}

export {
  liveViewLink,
  LIVE_VIEW_LINK_REL,
  LIVE_VIEW_LINK_TARGET,
  type LiveViewLinkProblem,
} from './handoff';
