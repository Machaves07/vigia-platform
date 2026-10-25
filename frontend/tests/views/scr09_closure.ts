/** RV-13 recorre las vistas de producción, con contenido de cierre generado y campos cerrados. */
import fc from 'fast-check';
import { createElement } from 'react';
import { MemoryRouter } from 'react-router';
import { ClosureHistory } from '@/pages/scr09/loop/closure/History';
import { OverdueView } from '@/pages/scr09/loop/closure/Overdue';
import type { LedgerRecord, OverduePage } from '@/ports/scr09Closure';
import { readView } from '../generators';
import type { ViewRegistration } from './types';

const history = fc
  .tuple(
    readView('LedgerRecordOut'),
    readView('ClosureBody'),
    readView('EvidenceEntryBody'),
    readView('UnclosableDeclarationBody'),
  )
  .map(([record, closure, evidence, declaration]) => ({
    items: [
      {
        ...record,
        record_type: 'closure',
        content: {
          ...closure,
          signer: {
            display_name: 'Firmante sintético',
            professional_license: closure.professional_license,
          },
        },
      },
      {
        ...record,
        record_id: '0190a1b2-8888-7000-8000-000000000001',
        record_type: 'action_evidence_added',
        content: evidence,
      },
      {
        ...record,
        record_id: '0190a1b2-9999-7000-8000-000000000001',
        record_type: 'unclosable_state_declared',
        content: declaration,
      },
      {
        ...record,
        record_id: '0190a1b2-9999-7000-8000-000000000002',
        record_type: 'unclosable_review',
        content: {
          review_note: closure.closure_description,
          outcome: 'renewed',
          reviewer: {
            display_name: 'Revisor sintético',
            professional_license: closure.professional_license,
          },
        },
      },
      {
        ...record,
        record_id: '0190a1b2-9999-7000-8000-000000000003',
        record_type: 'reopening',
        content: { reason_text: closure.closure_description },
      },
      {
        ...record,
        record_id: '0190a1b2-9999-7000-8000-000000000004',
        record_type: 'action_overdue',
        content: { escalation_level: 'coordinator' },
      },
    ],
  }));

export default {
  history: {
    screen: 'SCR-09',
    view: 'RV-13',
    data: history,
    render: (data, context) =>
      createElement(ClosureHistory, {
        records: data.items as LedgerRecord[],
        timeZone: 'America/Bogota',
        organizationId: context.organizationId,
        concessionOrganizationId: context.concessionOrganizationId,
        reportContractError: context.reportContractError,
      }),
    organizationOf: 'organization_id',
    allowedFields: [
      'items',
      'record_type',
      'received_at',
      'content',
      'description',
      'performed_by_role',
      'verification_note',
      'closure_description',
      'evaluated_alternative',
      'rejection_reason',
      'responsible_display_name',
      'decision_date',
      'compensatory_measure',
      'review_note',
      'reason_text',
      'performed_at',
      'signed_at',
      'declared_at',
      'review_due_at',
      'new_review_due_at',
      'reviewed_at',
      'reopened_at',
      'signer',
      'reviewer',
      'display_name',
      'professional_license',
      'outcome',
      'escalation_level',
    ],
  },
  overdue: {
    screen: 'SCR-09',
    view: 'RV-13',
    data: readView('OverduePageOut'),
    render: (data) =>
      createElement(
        MemoryRouter,
        null,
        createElement(OverdueView, {
          page: data as unknown as OverduePage,
          timeZone: 'America/Bogota',
        }),
      ),
    allowedFields: [
      'zones',
      'zone_id',
      'overdue_count',
      'highlighted_count',
      'items',
      'finding_state_id',
      'lifecycle_state',
      'due_at',
      'overdue_days',
      'highlighted',
    ],
  },
} satisfies Record<string, ViewRegistration>;
