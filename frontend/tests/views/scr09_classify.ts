/** RV-13 recorre los componentes de producción del lazo y del historial de clasificaciones. */
import { createElement } from 'react';
import { MemoryRouter } from 'react-router';
import { ClassificationHistory } from '@/pages/scr09/classify/History';
import { LoopView } from '@/pages/scr09/loop/LoopView';
import type { ClassificationHistory as History, FindingDetail } from '@/ports/scr09Classify';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

const CLASSIFICATION_FIELDS = [
  'classifications',
  'sequence_in_subject',
  'outcome',
  'reason_category',
  'reason_text',
  'previous_outcome',
  'reinforced_reason',
  'effect_on_loop',
  'classified_at',
  'classified_by',
  'display_name',
  'role_in_use',
  'time_aggregated',
];

export default {
  loop: {
    screen: 'SCR-09',
    view: 'RV-13',
    data: readView('FindingDetailOut'),
    render: (data) =>
      createElement(
        MemoryRouter,
        null,
        createElement(LoopView, {
          detail: data as unknown as FindingDetail,
          zone: null,
          timeZone: 'America/Bogota',
        }),
      ),
    allowedFields: [
      ...CLASSIFICATION_FIELDS,
      'lifecycle_state',
      'actions',
      'action_type',
      'action_description',
      'expected_outcome',
      'state',
      'assigned_at',
      'due_at',
      'counts_as_overdue',
    ],
  },
  classifications: {
    screen: 'SCR-09',
    view: 'RV-13',
    data: readView('ClassificationHistoryOut'),
    render: (data) => {
      const history = data as unknown as History;
      return createElement(ClassificationHistory, {
        classifications: history.classifications,
        reviewResolutions: history.review_resolutions,
        timeAggregated: history.time_aggregated,
        timeZone: 'America/Bogota',
      });
    },
    allowedFields: [...CLASSIFICATION_FIELDS, 'review_resolutions', 'resolved_at', 'resolved_by'],
  },
} satisfies Record<string, ViewRegistration>;
