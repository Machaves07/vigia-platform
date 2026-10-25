/** Única frontera de lectura de SCR-13 (IF-15); el servidor calcula y autoriza. */
import type { components } from '@/client/schema';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type Metrics = components['schemas']['MetricsOut'];
export type MetricSnapshot = components['schemas']['ProgramMetricSnapshot'];
export type Exposure = components['schemas']['ExposureOut'];
export type Adoption = components['schemas']['AdoptionReportOut'];
export type Overdue = components['schemas']['OverduePageOut'];
export type Plant = components['schemas']['PlantView'];

export const metricsPort = defineScreenPort('SCR-13', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  metrics: {
    kind: 'read',
    status: 'fixed',
    operationId: 'metrics_accreditation_metrics_get',
    permission: 'metrics.read',
  },
  exposure: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_exposure_accreditation_exposure_get',
    permission: 'exposure.read',
  },
  adoption: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_adoption_accreditation_adoption_get',
    permission: 'metrics.read',
  },
  overdue: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_overdue_loop_overdue_get',
    permission: 'executive_summary.read',
  },
  coverage: {
    kind: 'read',
    status: 'fixed',
    operationId: 'timeline_zones__zone_id__coverage_get',
    permission: 'coverage.read',
  },
});

export function metricsClock() {
  return currentTransport().clock;
}

export function metricsFailure(
  error: Error,
): 'error' | 'unavailable' | 'not_available_in_scope' | 'not_computable' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http') {
    if (failure.behaviour.kind === 'not_available_in_scope') return 'not_available_in_scope';
    if (failure.error.detail_code === 'accreditation_exposure_not_computable')
      return 'not_computable';
    if (
      failure.error.detail_code === 'accreditation_coverage_unavailable' ||
      failure.error.detail_code === 'loop_gate_state_unavailable'
    )
      return 'unavailable';
  }
  return 'error';
}

/** La organización de la concesión procede solo de la jerarquía autorizada. */
export interface MetricsContext {
  readonly organizationId: string;
  readonly concessionOrganizationId?: string | null;
  readonly reportContractError?: (error: { code: string }) => void;
}
export function acceptsOrganization(organization: string, context: MetricsContext): boolean {
  if (organization === context.organizationId || organization === context.concessionOrganizationId)
    return true;
  context.reportContractError?.({ code: 'foreign_organization' });
  return false;
}
