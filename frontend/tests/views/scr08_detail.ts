/**
 * RV-12 recorre los componentes de producción del detalle del hallazgo: el detalle de U-04, el
 * historial completo de clasificaciones y la parte del tramo que sale del `Finding` del contrato,
 * que se valida con Ajv antes de pintarse (un campo inyectado en el `Finding` lo deja sin pintar).
 */
import { createElement } from 'react';
import fc from 'fast-check';
import { ContractPart, DetailView, History } from '@/pages/scr08/detail/DetailView';
import type { ClassificationHistory, FindingDetail, FindingRecord } from '@/ports/scr08Detail';
import { validateContractFinding } from '@/ports/validation';
import { finding, readView } from '../generators';
import type { ViewRegistration } from './types';

const TIME_ZONE = 'America/Bogota';

export default {
  findingDetail: {
    screen: 'SCR-08',
    view: 'RV-12',
    data: readView('FindingDetailOut'),
    render: (data) =>
      createElement(DetailView, {
        detail: data as unknown as FindingDetail,
        zone: null,
        timeZone: TIME_ZONE,
        now: { kind: 'hidden' },
        records: { kind: 'ready', records: [] },
        // Sin reproductores: solo pintan textos fijos del catálogo, nunca datos, y «Difuminado por
        // construcción» coincide por azar con los fragmentos `constructor` que genera fast-check.
        clock: null,
        canViewClips: false,
      }),
    allowedFields: [
      'notices',
      'code',
      'since',
      'family',
      'tier',
      'lifecycle_state',
      'occurred_at',
      'time_aggregated',
      'aggregation_window_minutes',
      'age_days',
      'gate_at_event',
      'status',
      'effective_from',
      'episode',
      'total_episode_duration_ms',
      'total_condition_duration_ms',
      'segment_count',
      'expected_segment_count',
      'missing_segment_indexes',
      'observability_state_at_event',
      'standard',
      'title_es',
      'version',
      'declared_text',
      'superseded_since',
      'superseded_in_catalog_version',
      'records',
      'segment_index',
      'record_type',
      'received_at',
      'signals',
      'role',
      'value',
      'valid',
    ],
  },
  findingClassifications: {
    screen: 'SCR-08',
    view: 'RV-12',
    data: readView('ClassificationHistoryOut'),
    render: (data) => {
      const history = data as unknown as ClassificationHistory;
      return createElement(History, {
        classifications: history.classifications,
        reviewResolutions: history.review_resolutions,
        timeAggregated: history.time_aggregated,
        timeZone: TIME_ZONE,
      });
    },
    allowedFields: [
      'review_resolutions',
      'resolved_at',
      'resolved_by',
      'classifications',
      'sequence_in_subject',
      'outcome',
      'reason_category',
      'reason_text',
      'previous_outcome',
      'reinforced_reason',
      'classified_at',
      'classified_by',
      'display_name',
      'role_in_use',
      'time_aggregated',
    ],
  },
  contractFinding: {
    screen: 'SCR-08',
    view: 'RV-12',
    data: fc.record({
      received_at: fc
        .date({
          min: new Date('2026-01-01T00:00:00.000Z'),
          max: new Date('2027-01-01T00:00:00.000Z'),
          noInvalidDate: true,
        })
        .map((date) => date.toISOString()),
      finding: finding(),
    }),
    render: (data) => {
      const check = validateContractFinding(data.finding);
      const record: FindingRecord = check.ok
        ? { record_id: 'registro', ok: true, finding: check.value }
        : { record_id: 'registro', ok: false, error: check.error };
      return createElement(ContractPart, {
        record,
        receivedAt: String(data.received_at),
        timeZone: TIME_ZONE,
        aggregated: false,
      });
    },
    allowedFields: [
      'received_at',
      'finding',
      'node_time',
      'started_at',
      'observability_state',
      'condition_duration_ms',
      'episode_duration_ms',
      'cameras',
      'originating',
      'model_version',
      'software_version',
    ],
  },
} satisfies Record<string, ViewRegistration>;
