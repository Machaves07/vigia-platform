/** Frontera propia de evidencia, cierre, incerrable y reapertura (SUP-U05-08 a 15). */
import loop from '../../../backend/resources/labels.loop.es.json';
import type { components } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';

export type Detail = components['schemas']['FindingDetailOut'];
export type EvidenceBody = components['schemas']['EvidenceEntryBody'];
export type ClosureBody = components['schemas']['ClosureBody'];
export type DeclarationBody = components['schemas']['UnclosableDeclarationBody'];
export type ReviewBody = components['schemas']['UnclosableReviewBody'];
export type ReopeningBody = components['schemas']['ReopeningBody'];
export type LedgerRecord = components['schemas']['LedgerRecordOut'];
export type OverduePage = components['schemas']['OverduePageOut'];

export const closurePort = defineScreenPort('SCR-09', {
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  detail: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_finding_loop_findings__finding_state_id__get',
    permission: 'findings.read',
  },
  records: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_records_ledger_records_get',
    permission: 'findings.read',
  },
  configuration: {
    kind: 'read',
    status: 'fixed',
    operationId: 'configuration_loop_plants__plant_id__configuration_get',
    permission: 'hierarchy.read',
  },
  policy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'plant_policy_plants__plant_id__policy_get',
    permission: 'hierarchy.read',
  },
  evidence: {
    kind: 'write',
    status: 'fixed',
    operationId: 'add_evidence_loop_actions__action_id__evidence_post',
    permission: 'actions.manage',
  },
  uploadGrant: {
    kind: 'write',
    status: 'fixed',
    operationId: 'upload_grant_loop_actions__action_id__attachments_upload_grant_post',
    permission: 'actions.manage',
    dependency: 'evidence_store',
  },
  readAttachment: {
    kind: 'write',
    status: 'fixed',
    operationId: 'read_url_loop_attachments__attachment_id__read_url_post',
    permission: 'closure_attachments.read',
    dependency: 'evidence_store',
  },
  close: {
    kind: 'write',
    status: 'fixed',
    operationId: 'close_loop_findings__finding_state_id__closure_post',
    permission: 'findings.close_and_sign',
  },
  declare: {
    kind: 'write',
    status: 'fixed',
    operationId: 'declare_loop_findings__finding_state_id__unclosable_post',
    permission: 'findings.close_and_sign',
  },
  review: {
    kind: 'write',
    status: 'fixed',
    operationId: 'review_loop_unclosable__declaration_id__reviews_post',
    permission: 'findings.close_and_sign',
  },
  reopen: {
    kind: 'write',
    status: 'fixed',
    operationId: 'reopen_loop_findings__finding_state_id__reopening_post',
    permission: 'actions.manage',
  },
  overdue: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_overdue_loop_overdue_get',
    permission: 'executive_summary.read',
  },
});

export const ACTION_TYPES = Object.keys(loop.action_type);
export const REVIEW_OUTCOMES = Object.keys(loop.unclosable_review_outcome);
export const RECORD_TYPES = [
  'action_evidence_added',
  'closure',
  'unclosable_state_declared',
  'unclosable_review',
  'reopening',
  'action_overdue',
];
export const closureClock = () => currentTransport().clock;

/** Solo los campos elegidos se pintan; nunca se vuelca el contenido abierto del expediente. */
export function recordContent(record: LedgerRecord): Record<string, unknown> {
  const value = record.content;
  return value !== null && typeof value === 'object' && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}
export function stringField(value: unknown): string {
  return typeof value === 'string' ? value : '';
}

/** El expediente se pagina completo por zona; el sujeto se selecciona sin inventar una ruta. */
export async function readHistory(detail: Detail, organizationId: string, signal: AbortSignal) {
  const records: LedgerRecord[] = [];
  let cursor: components['schemas']['RecordCursorOut'] | null = null;
  do {
    const page = await closurePort.records.read(
      {
        query: {
          plant_id: detail.plant_id,
          zone_id: detail.zone_id,
          record_type: RECORD_TYPES,
          page_size: 200,
          ...(cursor === null
            ? {}
            : { after_received_at: cursor.received_at, after_record_id: cursor.record_id }),
        },
      },
      signal,
    );
    for (const record of page.items) {
      if (
        record.organization_id !== organizationId ||
        record.plant_id !== detail.plant_id ||
        record.scope.zone_id !== detail.zone_id
      )
        continue;
      if (recordContent(record).subject_id !== detail.subject_id) continue;
      if (RECORD_TYPES.includes(record.record_type)) records.push(record);
    }
    cursor = page.next_cursor;
  } while (cursor !== null);
  return records;
}

export function closureNotice(
  error: Error | null,
  fields: Readonly<Record<string, string>> = {},
): Notice | null {
  if (!isPortError(error) || error.failure.kind !== 'http') return null;
  const detail = error.failure.error.detail_code ?? '';
  return {
    id: `closure:${detail || error.failure.error.code}`,
    error: error.failure.error,
    field: Object.hasOwn(fields, detail) ? (fields[detail] ?? 'form') : 'form',
  };
}
