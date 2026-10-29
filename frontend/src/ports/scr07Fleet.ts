/**
 * Puerto de SCR-07: inventario y salud (VIG-311); alta y ciclo de vida del nodo, versión objetivo
 * y recaptura del encuadre (VIG-325). Los estados y la autorización los decide el servidor.
 */
import type { components, operations } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import type { FreeTextLimits } from './validation';
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
  // «Pendiente de su parte» del inicio del administrador (VIG-365): invitaciones sin aceptar y
  // zonas sin acta de alcance; cada una se pide solo con su clave.
  users: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_users_users_get',
    permission: 'users.manage',
  },
  gates: {
    kind: 'read',
    status: 'fixed',
    operationId: 'gate_state_zones__zone_id__gates_get',
    permission: 'catalog.read',
  },
  // GET /hierarchy no requiere clave de permiso: basta la sesión. Da las zonas horarias.
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  declare: {
    kind: 'write',
    status: 'fixed',
    operationId: 'declare_node_plants__plant_id__nodes_post',
    permission: 'commissioning.run',
  },
  issueCode: {
    kind: 'write',
    status: 'fixed',
    operationId: 'issue_enrollment_code_nodes__node_id__enrollment_codes_post',
    permission: 'commissioning.run',
  },
  attempts: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_enrollment_attempts_nodes__node_id__enrollment_attempts_get',
    permission: 'fleet.read',
  },
  assignZone: {
    kind: 'write',
    status: 'fixed',
    operationId: 'assign_zone_nodes__node_id__zones_post',
    permission: 'commissioning.run',
  },
  unassignZone: {
    kind: 'write',
    status: 'fixed',
    operationId: 'unassign_zone_nodes__node_id__zones__zone_id__unassignment_post',
    permission: 'commissioning.run',
  },
  revoke: {
    kind: 'write',
    status: 'fixed',
    operationId: 'revoke_node_nodes__node_id__revocation_post',
    permission: 'fleet.manage',
  },
  decommission: {
    kind: 'write',
    status: 'fixed',
    operationId: 'decommission_node_nodes__node_id__decommission_post',
    permission: 'fleet.manage',
  },
  publishTargetVersion: {
    kind: 'write',
    status: 'fixed',
    operationId: 'publish_target_version_fleet_target_versions_post',
    permission: 'fleet.manage',
  },
  recaptureFraming: {
    kind: 'write',
    status: 'fixed',
    operationId: 'framing_recapture_zones__zone_id__framing_recaptures_post',
    permission: 'commissioning.run',
  },
});

export type DeclaredNode = components['schemas']['DeclaredNodeView'];
export type DeclarationBody = components['schemas']['NodeDeclarationBody'];
export type EnrollmentCode = components['schemas']['EnrollmentCodeView'];
export type EnrollmentAttempt = components['schemas']['EnrollmentAttemptView'];
export type TargetVersionBody = components['schemas']['TargetVersionBody'];
export type TargetVersionPublication = components['schemas']['TargetVersionPublicationView'];
export type FramingRecaptureBody = components['schemas']['FramingRecaptureBody'];
export type FramingRegression = components['schemas']['RegressionOut'];
export type HierarchyPlant = components['schemas']['PlantView'];
export type HierarchyNode = components['schemas']['NodeView'];

/**
 * Límites de `reason_es` de la flota y de la recaptura (`REASON_MIN_CHARS`/`REASON_MAX_CHARS` de
 * `fleet.domain.node_fleet_record` y `MIN_REASON_CHARS`/`MAX_REASON_CHARS` de
 * `catalog.domain.catalog_version`, 10 a 500). `NodeReasonBody` y `FramingRecaptureBody` no los
 * publican en el esquema; el espejo local nunca es la barrera: el servidor decide.
 */
export const NODE_REASON_LIMITS: FreeTextLimits = { minLength: 10, maxLength: 500 };

/** Estado de nodo del servidor que habilita la baja (`fleet_node_not_revoked` si no lo es). */
export const REVOKED_STATUS = 'revoked';

/**
 * Aviso de una escritura de la flota: el `detail_code` del servidor se ancla al campo que nombra
 * `fields`; lo demás va al formulario (`form`). Una escritura cancelada no avisa de nada.
 */
export function fleetNotice(
  error: Error | null,
  fields: Readonly<Record<string, string>> = {},
): Notice | null {
  if (error === null) return null;
  if (!isPortError(error)) {
    return { id: 'fleet:internal', error: { code: 'internal_error', status: 0 }, field: 'form' };
  }
  const failure = error.failure;
  switch (failure.kind) {
    case 'http': {
      const detail = failure.error.detail_code ?? '';
      return {
        id: `fleet:${detail || failure.error.code}`,
        error: failure.error,
        field: Object.hasOwn(fields, detail) ? (fields[detail] ?? 'form') : 'form',
      };
    }
    case 'invalid_input':
      return {
        id: 'fleet:invalid',
        error: { code: 'invalid_request', status: 422 },
        field: 'form',
      };
    case 'unavailable':
      return { id: 'fleet:unavailable', error: { code: 'not_found', status: 404 }, field: 'form' };
    case 'aborted':
      return null;
    default:
      return {
        id: 'fleet:offline',
        error: { code: 'temporarily_unavailable', status: 0 },
        field: 'form',
      };
  }
}

/** Pagina dentro del mismo alcance; cada refresco sustituye la instantánea completa. */
export async function readFleet(filters: NodeFilters, signal: AbortSignal): Promise<FleetNode[]> {
  const nodes: FleetNode[] = [];
  let after: string | null = null;
  const seen = new Set<string>();
  do {
    // MAX_NODES_PAGE de la API de flota es 100, no el máximo genérico de UI-14.
    const page = await fleetPort.nodes.read({ query: { ...filters, limit: 100, after } }, signal);
    nodes.push(...page.nodes);
    after = page.next_after;
    if (after !== null) {
      if (seen.has(after)) throw new Error('Cursor de inventario repetido.');
      seen.add(after);
    }
  } while (after !== null);
  return nodes;
}
