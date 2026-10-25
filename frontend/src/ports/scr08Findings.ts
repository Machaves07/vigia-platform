/** Puerto de SCR-08 para proyecciones; ninguna fila provoca lecturas del detalle auditado. */
import type { components, operations } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type Finding = components['schemas']['InboxItemOut'];
export type InboxPage = components['schemas']['InboxPageOut'];
export type Plant = components['schemas']['PlantView'];
export type Zone = components['schemas']['ZoneView'];
export type Gate = components['schemas']['GateStateOut'];
export type Coverage = components['schemas']['CoverageStatusOut'];
export interface ZoneGate {
  readonly zone_id: string;
  readonly gate: Gate;
  readonly regression: string;
}
export type InboxFilters = Omit<
  NonNullable<operations['list_findings_loop_findings_get']['parameters']['query']>,
  'cursor' | 'limit'
>;

export const findingsPort = defineScreenPort('SCR-08', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  inbox: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_findings_loop_findings_get',
    permission: 'findings.read',
  },
  overdue: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_overdue_loop_overdue_get',
    permission: 'executive_summary.read',
  },
  queue: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_review_queue_loop_review_queue_get',
    permission: 'findings.read',
  },
  gates: {
    kind: 'read',
    status: 'fixed',
    operationId: 'gate_state_zones__zone_id__gates_get',
    permission: 'catalog.read',
  },
  regression: {
    kind: 'read',
    status: 'fixed',
    operationId: 'regression_state_zones__zone_id__regression_get',
    permission: 'catalog.read',
  },
  coverage: {
    kind: 'read',
    status: 'fixed',
    operationId: 'status_at_zones__zone_id__coverage_at_get',
    permission: 'coverage.read',
  },
});

export function findingsClock() {
  return currentTransport().clock;
}

export async function readGates(
  zoneIds: readonly string[],
  signal: AbortSignal,
): Promise<ZoneGate[]> {
  return Promise.all(
    zoneIds.map(async (zone_id) => {
      const path = { zone_id };
      const gate = await findingsPort.gates.read({ path }, signal);
      const regression = await findingsPort.regression.read({ path }, signal);
      return { zone_id, gate, regression: regression.state };
    }),
  );
}

export function findingsFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http' && failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

/** La API filtra alcance y episodios; la presentación consulta las compuertas por separado. */
export async function readInbox(
  filters: InboxFilters,
  pages: number,
  signal: AbortSignal,
): Promise<InboxPage> {
  const result: InboxPage = { items: [], zones: [], partitions: [], next_cursor: null };
  const seen = new Set<string>();
  for (let index = 0; index < pages; index++) {
    const page = await findingsPort.inbox.read(
      { query: { ...filters, cursor: result.next_cursor, limit: 50 } },
      signal,
    );
    result.items.push(...page.items);
    result.zones.push(...page.zones);
    result.partitions.push(...page.partitions);
    result.next_cursor = page.next_cursor;
    if (page.next_cursor === null) break;
    if (
      page.next_cursor.length === 0 ||
      page.next_cursor.length > 128 ||
      seen.has(page.next_cursor)
    )
      throw new Error('Cursor de bandeja inválido o repetido.');
    seen.add(page.next_cursor);
  }
  // Cada refresco recorre de nuevo la cadena desde el principio: no mezcla instantáneas.
  result.zones = [...new Map(result.zones.map((zone) => [zone.zone_id, zone])).values()];
  result.partitions = [
    ...new Map(result.partitions.map((partition) => [partition.plant_id, partition])).values(),
  ];
  return result;
}

/** Usa los conteos completos del servidor, no la longitud de la página ni los filtros de bandeja. */
export async function readOverdue(plantIds: readonly string[], signal: AbortSignal) {
  const summaries = [];
  for (const plant_id of plantIds) {
    const page = await findingsPort.overdue.read({ query: { plant_id, limit: 1 } }, signal);
    summaries.push({
      plant_id,
      count: page.zones.reduce((total, zone) => total + zone.overdue_count, 0),
      notice: page.partition_notice,
    });
  }
  return summaries;
}

/** Etiqueta de presentación de la compuerta ya decidida por el servidor. */
export function gateDisplay(gate: Gate, regression: string) {
  if (gate.usage.status === 'revoked')
    return { state: 'agreement_revoked', agreement_revoked_at: gate.usage.decided_at };
  if (regression === 'pending') return { state: 'regression_pending' };
  if (gate.mounting.status !== 'approved') return { state: 'no_scope_record' };
  if (gate.resulting_mode === 'productive') return { state: 'productive' };
  return { state: gate.usage.agreement_id === null ? 'no_use_agreement' : 'commissioning' };
}
