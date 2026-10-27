/** RV-14 recorre los mismos componentes de lectura que SCR-10; sin selección ni descarte masivo. */
import { createElement } from 'react';
import { QueueView } from '@/pages/scr10/queue/QueueView';
import { EvidenceView } from '@/pages/scr10/queue/EvidenceView';
import type { DetectionForReview, QueuePage } from '@/ports/scr10ReviewQueue';
import { detectionForReview, readView } from '../generators';
import { FakeClock } from '../support/fakeClock';
import type { ViewRegistration } from './types';

export default {
  evidence: {
    screen: 'SCR-10',
    view: 'RV-14',
    data: detectionForReview().map((detection) => ({ detection })),
    render: (data) =>
      createElement(EvidenceView, {
        data: { detection: data.detection as DetectionForReview, clips: [] },
        canViewClips: false,
        clock: new FakeClock(0),
      }),
    allowedFields: ['detection', 'signals', 'role', 'value', 'valid'],
  },
  queue: {
    screen: 'SCR-10',
    view: 'RV-14',
    data: readView('ReviewQueuePageOut'),
    render: (data) =>
      createElement(QueueView, {
        items: (data as unknown as QueuePage).items,
        plants: [],
        now: 0,
        canResolve: false,
        onResolve: () => undefined,
        onEvidence: () => undefined,
        // La primera detección, señalada como desde el detalle del hallazgo (VIG-359).
        target: (data as unknown as QueuePage).items[0]?.detection_id ?? null,
      }),
    allowedFields: [
      'items',
      'family',
      'max_confidence',
      'review_threshold',
      'publication_threshold',
      'review_due_at',
      'review_reason',
      'reminders_sent',
    ],
  },
} satisfies Record<string, ViewRegistration>;
