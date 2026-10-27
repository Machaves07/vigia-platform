/** SCR-10: proyección no auditada y resolución individual (SUP-U05-05 y 06).
 * La proyección no incluye señales ni clips: el expediente se consulta solo al abrir evidencia.
 */
import loop from '../../../backend/resources/labels.loop.es.json';
import type { components } from '@/client/schema';
import { validateDetectionForReview, type DetectionForReview } from '@/contracts';
import { defineScreenPort, isPortError } from './base';
import { currentTransport } from './base/http';
import type { Notice } from '@/components/NoticeStack/notice';

export type QueueItem = components['schemas']['ReviewQueueItemOut'];
export type QueuePage = components['schemas']['ReviewQueuePageOut'];
export type ResolutionBody = components['schemas']['ReviewResolutionBody'];
export type ResolutionResult = components['schemas']['ReviewResolutionOut'];
export type Plant = components['schemas']['PlantView'];
export type Evidence = components['schemas']['EvidenceOut'];
export type { DetectionForReview };

export const reviewQueuePort = defineScreenPort('SCR-10', {
  list: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_review_queue_loop_review_queue_get',
    permission: 'findings.read',
  },
  resolve: {
    kind: 'write',
    status: 'fixed',
    operationId: 'resolve_loop_review_queue__detection_id__resolution_post',
    permission: 'review_queue.resolve',
  },
  hierarchy: {
    kind: 'read',
    status: 'fixed',
    operationId: 'hierarchy_hierarchy_get',
    permission: 'session',
  },
  records: {
    kind: 'read',
    status: 'fixed',
    operationId: 'list_records_ledger_records_get',
    permission: 'findings.read',
  },
  receipt: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_record_ledger_records__record_id__get',
    permission: 'findings.read',
  },
  clip: {
    kind: 'write',
    status: 'fixed',
    operationId: 'read_url_evidence__evidence_id__read_url_post',
    permission: 'evidence.read',
    dependency: 'evidence_store',
  },
});

export const REASON_LIMITS = { minLength: 20, maxLength: 1000 };
export const OUTCOMES = Object.keys(loop.review_resolution_outcome);

export function reasonCategories(outcome: string) {
  const lists =
    outcome === 'review_confirmed'
      ? (['reason_category_confirmed'] as const)
      : outcome === 'review_discarded'
        ? (['reason_category_authorized', 'reason_category_false_positive'] as const)
        : [];
  return lists.flatMap((list) => Object.keys(loop[list]).map((value) => ({ list, value })));
}

export function queueClock() {
  return currentTransport().clock;
}

export function queueFailure(error: Error): 'error' | 'unavailable' | 'not_available_in_scope' {
  if (!isPortError(error)) return 'error';
  const failure = error.failure;
  if (failure.kind === 'offline' || failure.kind === 'unavailable') return 'unavailable';
  if (failure.kind === 'http' && failure.behaviour.kind === 'not_available_in_scope')
    return 'not_available_in_scope';
  return 'error';
}

export function resolutionNotice(error: Error | null): Notice | null {
  if (error === null || !isPortError(error) || error.failure.kind !== 'http') return null;
  const { failure } = error;
  const reason = [
    'loop_reason_required',
    'loop_reason_too_short',
    'loop_blocked_vocabulary_term',
    'loop_person_attribution_detected',
  ].some((code) => code === failure.error.detail_code);
  return {
    id: 'scr10:resolution',
    error: failure.error,
    field: reason ? 'reason_text' : failure.behaviour.kind === 'anchored' ? 'form' : null,
  };
}

/** Recorre otra vez la cadena desde el principio al refrescar; no mezcla páginas antiguas. */
export async function readQueue(pages: number, signal: AbortSignal): Promise<QueuePage> {
  const result: QueuePage = { items: [], next_cursor: null };
  const seen = new Set<string>();
  for (let index = 0; index < pages; index++) {
    const page = await reviewQueuePort.list.read(
      { query: { cursor: result.next_cursor, limit: 50 } },
      signal,
    );
    result.items.push(...page.items);
    result.next_cursor = page.next_cursor;
    if (page.next_cursor === null) break;
    if (!/^[A-Za-z0-9_-]{1,128}$/.test(page.next_cursor) || seen.has(page.next_cursor))
      throw new Error('Cursor de revisión inválido o repetido.');
    seen.add(page.next_cursor);
  }
  result.items = [...new Map(result.items.map((item) => [item.detection_id, item])).values()].sort(
    (a, b) =>
      Date.parse(a.review_due_at) - Date.parse(b.review_due_at) ||
      a.detection_id.localeCompare(b.detection_id),
  );
  return result;
}

export interface DetectionEvidence {
  readonly detection: DetectionForReview;
  readonly clips: readonly Evidence[];
}

/** Lectura auditada acotada al recibo de la fila. No usa detection_id como record_id.
 * El tipo persistido es el evento de recepción, no el nombre del modelo del contrato.
 * El servidor aplica el alcance; aquí además se exige identidad exacta antes de presentar datos.
 */
export async function readDetection(
  item: QueueItem,
  organizationId: string,
  signal: AbortSignal,
): Promise<DetectionEvidence | null> {
  let cursor: components['schemas']['RecordCursorOut'] | null = null;
  const seen = new Set<string>();
  do {
    const page = await reviewQueuePort.records.read(
      {
        query: {
          record_type: ['detection_for_review_received'],
          plant_id: item.plant_id,
          zone_id: item.zone_id,
          received_from: item.received_at,
          received_before: new Date(Date.parse(item.received_at) + 1).toISOString(),
          page_size: 50,
          ...(cursor === null
            ? {}
            : { after_received_at: cursor.received_at, after_record_id: cursor.record_id }),
        },
      },
      signal,
    );
    for (const record of page.items) {
      const data = record.content;
      if (!validateDetectionForReview(data)) continue;
      if (
        data.detection_id !== item.detection_id ||
        data.organization_id !== organizationId ||
        data.plant_id !== item.plant_id ||
        data.zone_id !== item.zone_id
      )
        continue;
      const clipIds = new Set(
        data.cameras.flatMap((camera) => camera.clips.map((clip) => clip.clip_id)),
      );
      return {
        detection: data,
        clips: record.evidences.filter((clip) => clipIds.has(clip.clip_id)),
      };
    }
    cursor = page.next_cursor;
    if (cursor !== null) {
      const key = `${cursor.received_at}:${cursor.record_id}`;
      if (seen.has(key)) throw new Error('Cursor del expediente repetido.');
      seen.add(key);
    }
  } while (cursor !== null);
  return null;
}
