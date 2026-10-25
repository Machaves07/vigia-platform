/**
 * Vista de lectura RV-09 (walk-test) de SCR-06, para las metapropiedades de las prohibiciones de
 * pantalla (PR-APP-07 y PR-APP-10, `tests/views/types.ts`).
 *
 * Pinta la sesión con los mismos componentes de solo lectura que usa la consola: la cabecera, los
 * pasos con las marcas del servidor, las horas por zona y la matriz de pases con sus conteos. No
 * hay datos de persona en RV-09 y la respuesta (`CurrentWalkTestOut`) no trae organización, así que
 * la vista no declara `organizationOf` y PR-APP-10 no la recorre (no hay registros de varias
 * organizaciones que aislar).
 */
import { createElement, Fragment } from 'react';

import { PassMatrixTable } from '@/pages/scr06/matrix/PassMatrixTable';
import { HoursPanel } from '@/pages/scr06/session/HoursPanel';
import { SessionHeader } from '@/pages/scr06/session/SessionHeader';
import { StepsPanel } from '@/pages/scr06/session/StepsPanel';
import type { CurrentWalkTest } from '@/ports/scr06Commissioning';
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
} satisfies Record<string, ViewRegistration>;
