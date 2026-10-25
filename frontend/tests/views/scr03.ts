/** RV-05 y RV-06: proyecciones reales y campos explícitos, sin sujetos ni registros fuente. */
import { createElement } from 'react';
import { composeCoverage } from '@/state/coverage/compose';
import {
  ResultsView,
  CheckpointsView,
  PublicKeyView,
  VerifierView,
} from '@/pages/scr03/IntegrityViews';
import { CoverageView } from '@/pages/scr03/CoverageView';
import type {
  Checkpoints,
  CheckpointKeys,
  CoverageResponse,
  IntegrityResult,
} from '@/ports/scr03Integrity';
import { readView } from '../generators';
import { FakeClock } from '../support/fakeClock';
import type { ViewRegistration } from './types';

export default {
  checkpointPublicKey: {
    screen: 'SCR-03',
    view: 'RV-05',
    data: readView('CheckpointKeyOut'),
    render: (data) =>
      createElement(PublicKeyView, { keyData: data as unknown as CheckpointKeys['keys'][number] }),
    allowedFields: ['key_id', 'public_key'],
  },
  autonomousVerifier: {
    screen: 'SCR-03',
    view: 'RV-05',
    data: readView('VerifierOut'),
    render: (data) => createElement(VerifierView, { sha256: String(data.sha256) }),
    allowedFields: ['sha256'],
  },
  integrityResults: {
    screen: 'SCR-03',
    view: 'RV-05',
    data: readView('IntegrityResultsOut'),
    render: (data) =>
      createElement(ResultsView, {
        results: data.results as IntegrityResult[],
        plants: [],
        timeZone: 'UTC',
      }),
    allowedFields: [
      'results',
      'chain',
      'kind',
      'plant_id',
      'head_sequence',
      'result',
      'broken_sequence',
      'verified_at',
    ],
  },
  integrityCheckpoints: {
    screen: 'SCR-03',
    view: 'RV-05',
    data: readView('CheckpointsOut'),
    render: (data, context) =>
      createElement(CheckpointsView, {
        data: data as unknown as Checkpoints,
        keys: { purpose: 'checkpoint', keys: [] },
        plants: [],
        timeZone: 'UTC',
        ...context,
      }),
    allowedFields: ['checkpoints', 'chain', 'kind', 'plant_id', 'sequence', 'taken_at', 'key_id'],
    organizationOf: 'organization_id',
  },
  zoneCoverage: {
    screen: 'SCR-03',
    view: 'RV-06',
    // El esquema permite fechas independientes: fijamos un tramo coherente para que la
    // metapropiedad recorra intervalos realmente pintados, no solo el estado de error.
    data: readView('CoverageTimelineOut').map((data) => {
      const response = data as unknown as CoverageResponse;
      const from = '2026-01-01T00:00:00.000Z';
      const to = '2026-01-08T00:00:00.000Z';
      const duration = 7 * 86_400_000;
      return {
        ...data,
        period: { from, to },
        intervals: [
          {
            ...response.intervals[0],
            starts_at: from,
            ends_at: to,
            duration_ms: duration,
            layer: 'node_report',
            state: 'observable',
            causes: [],
            clock_basis: 'node',
          },
        ],
        summary: {
          observable_ms: duration,
          degraded_ms: 0,
          not_observable_ms: 0,
          no_communication_ms: 0,
          never_reported_ms: 0,
          zone_not_active_ms: 0,
        },
      };
    }),
    render: (data, context) => {
      const response = data as unknown as CoverageResponse;
      const period = {
        fromMs: Date.parse(response.period.from ?? ''),
        toMs: Date.parse(response.period.to ?? ''),
      };
      const valid =
        Number.isFinite(period.fromMs) &&
        Number.isFinite(period.toMs) &&
        period.toMs > period.fromMs &&
        period.toMs - period.fromMs <= 366 * 86_400_000;
      // Los generadores de esquema no garantizan coherencia temporal entre campos.
      const composite = valid
        ? composeCoverage(period, [{ period, response }])
        : composeCoverage({ fromMs: 0, toMs: 1 }, []);
      return createElement(CoverageView, {
        data: { organization_id: response.organization_id, composite },
        clock: new FakeClock(),
        ...context,
      });
    },
    allowedFields: [
      'period',
      'from',
      'to',
      'intervals',
      'starts_at',
      'ends_at',
      'duration_ms',
      'layer',
      'state',
      'causes',
      'clock_basis',
      'summary',
      'observable_ms',
      'degraded_ms',
      'not_observable_ms',
      'no_communication_ms',
      'never_reported_ms',
      'zone_not_active_ms',
    ],
    organizationOf: 'organization_id',
  },
} satisfies Record<string, ViewRegistration>;
