/** RV-16: componentes de producción, campos expresos, sin objetos de autoría abiertos. */
import { createElement } from 'react';
import { PackageView } from '@/pages/scr12/package/PackageView';
import { CommitmentView } from '@/pages/scr12/package/Commitments';
import { SampleView } from '@/pages/scr12/package/Sample';
import type { MonthlyPackage, Commitment, SampleItem } from '@/ports/scr12Monthly';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

export default {
  package: {
    screen: 'SCR-12',
    view: 'RV-16',
    data: readView('MonthlyPackageOut'),
    render: (data) =>
      createElement(PackageView, {
        data: data as unknown as MonthlyPackage,
        timeZone: 'America/Bogota',
      }),
    allowedFields: [
      'period',
      'state',
      'generated_at',
      'findings_summary',
      'received',
      'classified',
      'confirmed',
      'discarded',
      'closed',
      'overdue',
      'unclosable',
      'reopened',
      'recurrence_summary',
      'family',
      'episodes_after_closure',
      'commitments_from_previous_period',
      'commitment_id',
      'record_id',
    ],
  },
  commitment: {
    screen: 'SCR-12',
    view: 'RV-16',
    data: readView('CommitmentOut'),
    render: (data) => createElement(CommitmentView, { item: data as unknown as Commitment }),
    allowedFields: ['text', 'responsible_user_id', 'due_date', 'state', 'closed_note'],
  },
  sample: {
    screen: 'SCR-12',
    view: 'RV-16',
    data: readView('SampleItemOut'),
    render: (data) =>
      createElement(SampleView, {
        item: data as unknown as SampleItem,
        timeZone: 'America/Bogota',
      }),
    allowedFields: ['classification_outcome', 'classified_at', 'outcome', 'comment', 'reviewed_at'],
  },
} satisfies Record<string, ViewRegistration>;
