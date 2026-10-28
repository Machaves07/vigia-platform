/**
 * Frescura por tipo de dato (PAT-APP-ESC-01, BR-APP-54): cada lectura que la aplicación guarda en
 * la caché declara aquí su política, por `MÉTODO /ruta` de `backend/openapi/app.yaml`.
 *
 * - `identity`: `GET /me` y `GET /hierarchy`, 30 s, sin refresco periódico.
 * - `live`: bandeja y cola (proyección de U-04), notificaciones y panel de salud: 60 s y refresco
 *   periódico (`periodic_60s`) solo con la pestaña visible, el circuito `platform` cerrado y la
 *   persona activa (D-4).
 * - `reference`: catálogo, compuertas, política, firmantes, configuración del lazo y demás datos
 *   de referencia: 5 min, sin refresco periódico.
 * - `audited`: lecturas auditadas del expediente (detalle, historial, integridad, puntos de
 *   control, auditoría, exportación, paquete, etiquetas, registros crudos, cobertura): frescura 0
 *   y **solo a demanda** (`on_demand`): nunca se piden sin una acción explícita de la persona,
 *   nunca se refrescan solas y nunca se precargan (A-6).
 *
 * `tests/unit/query/policies.test.ts` exige que toda lectura de `app.yaml` tenga política o esté
 * en `UNCACHED_READS` con su motivo.
 */

export type FreshnessKind = 'identity' | 'live' | 'reference' | 'audited';

export interface QueryPolicy {
  readonly kind: FreshnessKind;
  /** `staleTime` de TanStack Query. */
  readonly staleTimeMs: number;
  /** `periodic_60s` de BR-APP-54. */
  readonly periodic: boolean;
  /** `on_demand` de BR-APP-54: solo por acción explícita. */
  readonly explicitOnly: boolean;
}

export const PERIODIC_INTERVAL_MS = 60_000;

export const FRESHNESS: Readonly<Record<FreshnessKind, QueryPolicy>> = {
  identity: { kind: 'identity', staleTimeMs: 30_000, periodic: false, explicitOnly: false },
  live: { kind: 'live', staleTimeMs: 60_000, periodic: true, explicitOnly: false },
  reference: { kind: 'reference', staleTimeMs: 300_000, periodic: false, explicitOnly: false },
  audited: { kind: 'audited', staleTimeMs: 0, periodic: false, explicitOnly: true },
};

const POLICY_KINDS = {
  // Identidad (30 s).
  'GET /me': 'identity',
  'GET /hierarchy': 'identity',

  // Bandeja, cola, notificaciones y panel de salud (60 s con refresco periódico).
  'GET /loop/findings': 'live',
  'GET /loop/review-queue': 'live',
  'GET /notifications': 'live',
  'GET /fleet/nodes': 'live',
  'GET /fleet/alarms': 'live',

  // Catálogo, compuertas, política, firmantes, configuración del lazo y referencia (5 min).
  'GET /zones/{zone_id}/catalog': 'reference',
  'GET /zones/{zone_id}/catalog/versions': 'reference',
  'GET /zones/{zone_id}/catalog/versions/{catalog_version}': 'reference',
  'GET /zones/{zone_id}/regression': 'reference',
  'GET /zones/{zone_id}/gates': 'reference',
  'GET /plants/{plant_id}/admissions': 'reference',
  'GET /plants/{plant_id}/policy': 'reference',
  'GET /plants/{plant_id}/signatory-policy': 'reference',
  'GET /loop/plants/{plant_id}/configuration': 'reference',
  'GET /loop/vocabulary': 'reference',
  'GET /loop/overdue': 'reference',
  'GET /loop/assistant-requests/{request_id}': 'reference',
  'GET /plants/{plant_id}/fleet-thresholds': 'reference',
  'GET /fleet/nodes/{node_id}': 'reference',
  'GET /nodes/{node_id}/enrollment-attempts': 'reference',
  'GET /zones/{zone_id}/walk-tests/current': 'reference',
  'GET /zones/{zone_id}/commissioning-clips': 'reference',
  'GET /zones/{zone_id}/transparency': 'reference',
  'GET /organization/settings': 'reference',
  'GET /users': 'reference',
  'GET /auth/sessions': 'reference',
  'GET /concessions': 'reference',
  'GET /provider/concessions': 'reference',
  'GET /notifications/preferences': 'reference',
  'GET /accreditation/commitments': 'reference',
  'GET /accreditation/exposure': 'reference',
  'GET /accreditation/adoption': 'reference',
  'GET /accreditation/metrics': 'reference',
  'GET /.well-known/vigia-checkpoint-keys': 'reference',
  'GET /.well-known/vigia-verifier': 'reference',

  // Lecturas auditadas: frescura 0 y solo a demanda (BR-APP-54, A-6).
  'GET /loop/findings/{finding_state_id}': 'audited',
  'GET /loop/findings/{finding_state_id}/classifications': 'audited',
  'GET /zones/{zone_id}/coverage': 'audited',
  'GET /zones/{zone_id}/coverage/at': 'audited',
  'GET /integrity/results': 'audited',
  'GET /integrity/checkpoints': 'audited',
  'GET /audit/entries': 'audited',
  'GET /concessions/{concession_id}/queries': 'audited',
  'GET /ledger/records': 'audited',
  'GET /ledger/records/{record_id}': 'audited',
  'GET /labels': 'audited',
  'GET /commissioning-records/{record_id}': 'audited',
  'GET /accreditation/exports/{export_id}': 'audited',
  'GET /accreditation/monthly-packages': 'audited',
  'GET /accreditation/monthly-packages/{package_id}/sample': 'audited',
} as const satisfies Record<string, FreshnessKind>;

/** Lectura de la aplicación con política declarada (`MÉTODO /ruta` de `app.yaml`). */
export type ReadOperation = keyof typeof POLICY_KINDS;

/** Lecturas de `app.yaml` que nunca pasan por la caché, con su motivo. */
export const UNCACHED_READS: Readonly<Record<string, string>> = {
  'GET /health/live': 'sonda de state.connection (fetch propio, sin caché)',
  'GET /api/nodes/zones/{zone_id}/catalog': 'API del nodo (mTLS); la aplicación no la usa',
  'GET /loop/findings/{finding_state_id}/root-cause-analyses/in-progress':
    'SCR-11 la pide al entrar, sin caché: debe mostrar lo último guardado para retomarlo (VIG-363)',
  'GET /commissioning-records/{record_id}/document': 'descarga por navegación, nunca en caché',
  'GET /accreditation/exports/{export_id}/download': 'descarga por navegación, nunca en caché',
  'GET /accreditation/monthly-packages/{plant_id}/{period}/download':
    'descarga por navegación, nunca en caché',
};

export const READ_POLICIES: Readonly<Record<ReadOperation, QueryPolicy>> = Object.fromEntries(
  Object.entries(POLICY_KINDS).map(([operation, kind]) => [operation, FRESHNESS[kind]]),
) as Record<ReadOperation, QueryPolicy>;

/** La política de una lectura; una operación sin política no compila. */
export function policyFor(operation: ReadOperation): QueryPolicy {
  return READ_POLICIES[operation];
}
