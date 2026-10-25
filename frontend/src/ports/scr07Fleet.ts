/** Puerto de SCR-07: inventario y salud. Los estados los calcula el servidor. */
import type { components, operations } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export function fleetClock() {
  return currentTransport().clock;
}

/** Clasificación cerrada, sin que la pantalla importe otro puerto ni el cliente. */
export function fleetFailure(error: Error): {
  state: 'error' | 'unavailable' | 'not_available_in_scope';
  correlationId?: string;
} {
  if (!isPortError(error)) return { state: 'error' };
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return { state: 'unavailable' };
  if (failure.kind === 'http') {
    if (failure.behaviour.kind === 'not_available_in_scope')
      return { state: 'not_available_in_scope' };
    const body: unknown = failure.error.body;
    if (
      typeof body === 'object' &&
      body !== null &&
      'correlation_id' in body &&
      typeof body.correlation_id === 'string'
    )
      return { state: 'error', correlationId: body.correlation_id };
  }
  return { state: 'error' };
}

export type FleetNode = components['schemas']['NodeInventoryView'];
export type FleetAlarm = components['schemas']['FleetAlarmView'];
export type FleetWarning = components['schemas']['FleetAlarmKind'];
export type FleetDetail = components['schemas']['NodeInventoryDetail'];
export type FleetThresholds = components['schemas']['FleetThresholdsBody'];
export type NodeFilters = NonNullable<
  operations['list_fleet_nodes_fleet_nodes_get']['parameters']['query']
>;
export type AlarmFilters = NonNullable<
  operations['list_fleet_alarms_fleet_alarms_get']['parameters']['query']
>;

export const fleetPort = defineScreenPort('SCR-07', {
  nodes: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_fleet_nodes_fleet_nodes_get',
    permission: 'fleet.read',
  },
  detail: {
    kind: 'read',
    status: 'fixed',
    operationId: 'fleet_node_fleet_nodes__node_id__get',
    permission: 'fleet.read',
  },
  alarms: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_fleet_alarms_fleet_alarms_get',
    permission: 'fleet.read',
  },
  thresholds: {
    kind: 'read',
    status: 'fixed',
    operationId: 'plant_fleet_thresholds_plants__plant_id__fleet_thresholds_get',
    permission: 'fleet.read',
  },
  saveThresholds: {
    kind: 'write',
    status: 'fixed',
    operationId: 'put_plant_fleet_thresholds_plants__plant_id__fleet_thresholds_put',
    permission: 'fleet.manage',
  },
  // GET /hierarchy no requiere clave de permiso: basta la sesión. Da las zonas horarias.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
});

/** Pagina dentro del mismo alcance; cada refresco sustituye la instantánea completa. */
export async function readFleet(filters: NodeFilters, signal: AbortSignal): Promise<FleetNode[]> {
  const nodes: FleetNode[] = [];
  let after: string | null = null;
  const seen = new Set<string>();
  do {
    const page = await fleetPort.nodes.read({ query: { ...filters, limit: 200, after } }, signal);
    nodes.push(...page.nodes);
    after = page.next_after;
    if (after !== null) {
      if (seen.has(after)) throw new Error('Cursor de inventario repetido.');
      seen.add(after);
    }
  } while (after !== null);
  return nodes;
}
