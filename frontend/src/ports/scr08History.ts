/**
 * Puerto de SCR-08 para el histórico de lo cerrado (TASK-586; O-11): `GET /loop/closed-findings`
 * (`findings.read`), los hallazgos cerrados y descartados del alcance, desde la proyección y sin
 * auditoría de consulta (BR-LAZ-09). Paginación por clave con el cursor opaco del servidor y 50
 * filas por página (BR-APP-53). Ninguna fila provoca lecturas auditadas: la ficha de vida se pide
 * solo al abrirla.
 */
import type { components, operations } from '@/client/schema';
import { defineScreenPort } from './base';
import { currentTransport } from './base/http';

export type ClosedFinding = components['schemas']['ClosedFindingOut'];
export type ClosedFindingsPage = components['schemas']['ClosedFindingsPageOut'];
export type HistoryFilters = Omit<
  NonNullable<operations['list_closed_findings_loop_closed_findings_get']['parameters']['query']>,
  'cursor' | 'limit'
>;

/** Los dos estados del histórico (los únicos que admite el servidor en `lifecycle_state`). */
export const HISTORY_STATES = ['closed', 'discarded'] as const;

export const historyPort = defineScreenPort('SCR-08', {
  closed: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_closed_findings_loop_closed_findings_get',
    permission: 'findings.read',
  },
});

export function historyClock() {
  return currentTransport().clock;
}

/** Una página del histórico: el cursor viaja tal cual lo dio el servidor. */
export function readClosedPage(
  filters: HistoryFilters,
  cursor: string | null,
  limit: number,
  signal: AbortSignal,
): Promise<ClosedFindingsPage> {
  return historyPort.closed.read({ query: { ...filters, cursor, limit } }, signal);
}
