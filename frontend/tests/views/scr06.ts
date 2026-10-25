/**
 * Vista de lectura RV-09 (walk-test) de SCR-06, para las metapropiedades de las prohibiciones de
 * pantalla (PR-APP-07 y PR-APP-10, `tests/views/types.ts`): la sesión, la prueba de oclusión y el
 * acta cerrada.
 *
 * Pinta la sesión con los mismos componentes de solo lectura que usa la consola: la cabecera, los
 * pasos con las marcas del servidor, las horas por zona y la matriz de pases con sus conteos. No
 * hay datos de persona en RV-09 y la respuesta (`CurrentWalkTestOut`) no trae organización, así que
 * la vista no declara `organizationOf` y PR-APP-10 no la recorre (no hay registros de varias
 * organizaciones que aislar).
 */
import { createElement, Fragment } from 'react';

import { PassMatrixTable } from '@/pages/scr06/matrix/PassMatrixTable';
import { OcclusionList } from '@/pages/scr06/occlusion/OcclusionList';
import { RecordView } from '@/pages/scr06/record/RecordView';
import { HoursPanel } from '@/pages/scr06/session/HoursPanel';
import { SessionHeader } from '@/pages/scr06/session/SessionHeader';
import { StepsPanel } from '@/pages/scr06/session/StepsPanel';
import type {
  CommissioningRecord,
  CurrentWalkTest,
  OcclusionTest,
} from '@/ports/scr06Commissioning';
import { text } from '@/texts';

import { readView } from '../generators';
import type { ViewRegistration } from './types';

function walkTest(data: Record<string, unknown>): CurrentWalkTest {
  return data as unknown as CurrentWalkTest;
}

export default {
  scr06_walk_test: {
    screen: 'SCR-06',
    view: 'RV-09',
    data: readView('CurrentWalkTestOut'),
    render: (data) => {
      const { session } = walkTest(data);
      if (session === null) return createElement('p', null, text('scr06.session.empty'));
      return createElement(
        Fragment,
        null,
        createElement(SessionHeader, { session }),
        createElement(StepsPanel, { session }),
        createElement(HoursPanel, { session }),
        createElement(PassMatrixTable, { session }),
      );
    },
    allowedFields: [
      'session',
      'status',
      'kind',
      'catalog_version',
      'passes_per_cell',
      'started_at',
      'last_activity_at',
      'reopened_at',
      'reopen_reason_es',
      'steps',
      'step_id',
      'step_kind',
      'responsible_user_id',
      'ended_at',
      'effective_duration_ms',
      'correction',
      'reason_es',
      'steps_summary',
      'duration_ms',
      'total_duration_ms',
      'rows',
      'row_id',
      'posture',
      'standard_version',
      'predicate_conditions',
      'required_passes',
      'passes',
      'pass_id',
      'detected',
      'missed',
      'false_alarm',
      'result',
      'recorded_at',
      'evidence_ref',
    ],
  },
  /** Prueba de oclusión (`OcclusionTestOut`, IF-13 paso 5): `recorded_by` nunca se pinta. */
  scr06_occlusion_test: {
    screen: 'SCR-06',
    view: 'RV-09',
    data: readView('OcclusionTestOut'),
    render: (data) => createElement(OcclusionList, { tests: [data as unknown as OcclusionTest] }),
    allowedFields: [
      'test_id',
      'camera_id',
      'started_at',
      'ended_at',
      'deadline',
      'verification',
      'declared_reason_es',
      'failure_reason',
    ],
  },
  /**
   * Acta cerrada (`CommissioningRecordOut`, IF-13 paso 7): horas por tipo de paso y total de la
   * zona; ni el responsable ni quién aceptó las falsas alarmas (`accepted_by`) se pintan (H-53).
   */
  scr06_commissioning_record: {
    screen: 'SCR-06',
    view: 'RV-09',
    data: readView('CommissioningRecordOut'),
    render: (data) => createElement(RecordView, { record: data as unknown as CommissioningRecord }),
    allowedFields: [
      'commissioning_record_id',
      'kind',
      'catalog_version',
      'passes_per_cell',
      'closed_at',
      'matrix_results',
      'row_id',
      'detected',
      'missed',
      'false_alarms',
      'false_negatives_total',
      'false_alarm_rate_observed',
      'false_alarm_threshold',
      'false_alarm_acceptance',
      'reason_es',
      'accepted_at',
      'latency',
      'node_tranche',
      'platform_tranche',
      'exposure_tranche',
      'served_tranche',
      'median_ms',
      'p95_ms',
      'max_ms',
      'repetitions',
      'measured_by',
      'not_measured',
      'indicative_sum_median_ms',
      'indicative_sum_p95_ms',
      'cameras_measured',
      'camera_id',
      'measured_fps',
      'declared_min_fps',
      'occlusion_summary',
      'verification',
      'total_duration_ms',
      'steps_summary',
      'step_kind',
      'duration_ms',
      'installer_measurements',
      'beacon_latency_ms_p95',
      'baselines',
      'captured_at',
      'signatures',
      'user_id',
      'role_in_use',
      'signed_at',
    ],
  },
} satisfies Record<string, ViewRegistration>;
