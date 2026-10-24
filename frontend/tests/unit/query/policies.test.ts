/**
 * Frescura por tipo de dato (PAT-APP-ESC-01, BR-APP-54): toda lectura de `app.yaml` tiene política
 * declarada en `policies.ts` o un motivo para no pasar por la caché; una lectura nueva sin
 * política hace fallar esta prueba. Cada fila de la tabla del diseño, con su valor.
 */
import { describe, expect, it } from 'vitest';

import {
  FRESHNESS,
  PERIODIC_INTERVAL_MS,
  policyFor,
  READ_POLICIES,
  UNCACHED_READS,
  type ReadOperation,
} from '@/state/query';

import { API_OPERATIONS } from '../../generators/openapi';

const SPEC_READS = API_OPERATIONS.filter((operation) => operation.method === 'get').map(
  (operation) => `GET ${operation.path}`,
);

/** PAT-APP-ESC-01, fila a fila. */
const TABLE: Readonly<
  Record<'identity' | 'live' | 'reference' | 'audited', readonly ReadOperation[]>
> = {
  identity: ['GET /me', 'GET /hierarchy'],
  live: [
    'GET /loop/findings',
    'GET /loop/review-queue',
    'GET /notifications',
    'GET /fleet/nodes',
    'GET /fleet/alarms',
  ],
  reference: [
    'GET /zones/{zone_id}/catalog',
    'GET /zones/{zone_id}/gates',
    'GET /plants/{plant_id}/policy',
    'GET /plants/{plant_id}/signatory-policy',
    'GET /loop/plants/{plant_id}/configuration',
  ],
  audited: [
    'GET /loop/findings/{finding_state_id}',
    'GET /loop/findings/{finding_state_id}/classifications',
    'GET /integrity/results',
    'GET /integrity/checkpoints',
    'GET /audit/entries',
    'GET /accreditation/exports/{export_id}',
    'GET /accreditation/monthly-packages',
    'GET /labels',
    'GET /ledger/records',
    'GET /zones/{zone_id}/coverage',
  ],
};

describe('policies (PAT-APP-ESC-01)', () => {
  it('toda lectura de app.yaml tiene política o motivo para no pasar por la caché', () => {
    const declared = new Set([...Object.keys(READ_POLICIES), ...Object.keys(UNCACHED_READS)]);
    expect(SPEC_READS.filter((read) => !declared.has(read))).toEqual([]);
  });

  it('ninguna lectura tiene a la vez política y exclusión', () => {
    expect(Object.keys(UNCACHED_READS).filter((read) => read in READ_POLICIES)).toEqual([]);
  });

  it('no declara políticas ni exclusiones de lecturas que app.yaml no tiene', () => {
    const spec = new Set(SPEC_READS);
    expect(
      [...Object.keys(READ_POLICIES), ...Object.keys(UNCACHED_READS)].filter(
        (read) => !spec.has(read),
      ),
    ).toEqual([]);
  });

  it('GET /me y GET /hierarchy: 30 s, sin refresco periódico', () => {
    for (const read of TABLE.identity) {
      expect(policyFor(read)).toEqual({
        kind: 'identity',
        staleTimeMs: 30_000,
        periodic: false,
        explicitOnly: false,
      });
    }
  });

  it('bandeja, cola, notificaciones y panel de salud: 60 s con refresco periódico de 60 s', () => {
    expect(PERIODIC_INTERVAL_MS).toBe(60_000);
    for (const read of TABLE.live) {
      expect(policyFor(read)).toEqual({
        kind: 'live',
        staleTimeMs: 60_000,
        periodic: true,
        explicitOnly: false,
      });
    }
  });

  it('catálogo, compuertas, política, firmantes y configuración del lazo: 5 min', () => {
    for (const read of TABLE.reference) {
      expect(policyFor(read)).toEqual({
        kind: 'reference',
        staleTimeMs: 300_000,
        periodic: false,
        explicitOnly: false,
      });
    }
  });

  it('lecturas auditadas: frescura 0, solo a demanda y nunca periódicas', () => {
    for (const read of TABLE.audited) {
      expect(policyFor(read)).toEqual({
        kind: 'audited',
        staleTimeMs: 0,
        periodic: false,
        explicitOnly: true,
      });
    }
  });

  it('solo las lecturas live refrescan solas y toda frescura 0 es a demanda', () => {
    for (const [read, policy] of Object.entries(READ_POLICIES)) {
      expect(policy.periodic, read).toBe(policy.kind === 'live');
      if (policy.staleTimeMs === 0) expect(policy.explicitOnly, read).toBe(true);
      expect(policy, read).toBe(FRESHNESS[policy.kind]);
    }
  });
});
