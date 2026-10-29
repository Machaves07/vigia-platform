/**
 * Puerto de SCR-02 · Concesiones (IF-06 e IF-23; RV-04; BR-APP-30, 51, 54, 67 a 69; VIG-322).
 * Todas las operaciones son `fixed` de U-02:
 *
 * - Cliente: `GET /concessions` (`concessions.read`), `GET /concessions/{concession_id}/queries`
 *   (`concessions.read`, lectura auditada y **solo a demanda**: la pide abrir el detalle o pulsar
 *   «Actualizar», nunca un temporizador; A-6) y `POST /concessions/{concession_id}/revoke`
 *   (`concessions.revoke`).
 * - Proveedor: `GET/POST /provider/concessions` (`concessions.grant`).
 * - `GET /hierarchy` (ruta de sesión) para nombrar el alcance de las concesiones del cliente.
 *
 * Las políticas de consulta de estas lecturas están en `state.query` (`policies.ts`). El tope del
 * vencimiento es `concession_max_days` del cliente: solo lo conoce el servidor, que rechaza lo que
 * lo supera; la pantalla no replica esa regla (BR-APP-43).
 */
import grant from '../../openapi/bodies/grant_provider_concessions_post.json';
import type { components } from '@/client/schema';
import { reasonLimits } from '@/components/ReasonField/limits';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

/** Rutas con sesión sin clave propia (`SessionRoute` de U-02). */
const SESSION_ROUTE = 'session';

export type ClientConcession = components['schemas']['ConcessionView'];
export type ProviderConcession = components['schemas']['ProviderConcessionView'];
export type ProviderQuery = components['schemas']['ProviderQueryItem'];
export type GrantBody = components['schemas']['GrantBody'];
export type Hierarchy = components['schemas']['HierarchyView'];
export type GrantScopeLevel = GrantBody['scope_level'];

export const concessionsPort = defineScreenPort('SCR-02', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: SESSION_ROUTE,
  },
  concessions: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_concessions_concessions_get',
    permission: 'concessions.read',
  },
  queries: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_queries_concessions__concession_id__queries_get',
    permission: 'concessions.read',
  },
  revoke: {
    kind: 'write',
    status: 'fixed',
    operationId: 'revoke_concessions__concession_id__revoke_post',
    permission: 'concessions.revoke',
  },
  providerConcessions: {
    kind: 'read',
    status: 'fixed',
    operationId: 'own_concessions_provider_concessions_get',
    permission: 'concessions.grant',
  },
  grant: {
    kind: 'write',
    status: 'fixed',
    operationId: 'grant_provider_concessions_post',
    permission: 'concessions.grant',
  },
});

export function concessionsClock() {
  return currentTransport().clock;
}

/** Límites del motivo de la solicitud (10 a 500), leídos del esquema generado (BR-APP-44). */
export const GRANT_REASON_LIMITS = reasonLimits(grant, 'reason');

const grantProperties = grant.body.$defs.GrantBody.properties;

/** Niveles de alcance que admite la solicitud (organización o planta), del esquema generado. */
export const GRANT_SCOPE_LEVELS: readonly GrantScopeLevel[] =
  grantProperties.scope_level.enum.filter(
    (value): value is GrantScopeLevel => value === 'organization' || value === 'plant',
  );

/**
 * Límites de `duration_hours` del esquema (1 h y el tope del esquema). El tope de cada cliente
 * (`concession_max_days`) puede ser menor y lo aplica el servidor.
 */
export const GRANT_DURATION_HOURS: { readonly min: number; readonly max: number } = (() => {
  const integer = grantProperties.duration_hours.anyOf.find(
    (option): option is { type: string; minimum: number; maximum: number } =>
      'minimum' in option && 'maximum' in option,
  );
  if (integer === undefined) {
    throw new RangeError('grant_provider_concessions_post: duration_hours sin mínimo ni máximo.');
  }
  return { min: integer.minimum, max: integer.maximum };
})();

/** Una página de consultas del proveedor con el cursor opaco del servidor (`after`). */
export async function readQueries(
  concessionId: string,
  cursor: Readonly<Record<string, string>> | null,
  pageSize: number,
  signal: AbortSignal,
) {
  // `KeysetTable` solo admite claves de cursor `cursor` o `after_*`: con `after` a secas, la
  // primera página con continuación (más de 50 consultas) se rechazaba como «No se pudo cargar».
  const after = cursor?.after_query;
  const page = await concessionsPort.queries.read(
    {
      path: { concession_id: concessionId },
      query: { limit: pageSize, ...(after === undefined ? {} : { after }) },
    },
    signal,
  );
  return {
    items: page.queries,
    nextCursor: page.next_after === null ? null : { after_query: page.next_after },
  };
}

/** Las concesiones del cliente (`GET /concessions` no pagina: una sola página). */
export async function readClientConcessions(signal: AbortSignal) {
  const view = await concessionsPort.concessions.read({}, signal);
  return { items: view.concessions, nextCursor: null };
}

/** Las concesiones que la persona del proveedor se concedió (una sola página). */
export async function readProviderConcessions(signal: AbortSignal) {
  const view = await concessionsPort.providerConcessions.read(undefined, signal);
  return { items: view.concessions, nextCursor: null };
}

/**
 * Aviso del servidor para un formulario. Los `invalid_request` y `conflict` (comportamiento
 * `anchored`) se anclan al campo que indique `anchor(code)`, con el mensaje del servidor. Cualquier
 * otra respuesta del servidor va a la pila global (`field: null`). `null` si el fallo no es una
 * respuesta HTTP.
 */
export function writeNotice(
  error: Error | null,
  id: string,
  anchor: (code: string) => string = () => 'form',
): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const anchored = error.failure.behaviour.kind === 'anchored';
  return {
    id: `scr02:${id}`,
    error: error.failure.error,
    field: anchored ? anchor(error.failure.error.code) : null,
  };
}

/** El fallo local del puerto (cuerpo que no cumple su esquema, sin conexión), si lo hay. */
export function localFailure(error: Error | null): 'invalid_input' | 'unavailable' | null {
  if (error === null) return null;
  if (!isPortError(error)) return 'unavailable';
  if (error.failure.kind === 'invalid_input') return 'invalid_input';
  if (error.failure.kind === 'http') return null;
  return 'unavailable';
}
