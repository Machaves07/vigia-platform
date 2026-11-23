/**
 * Puerto de SCR-08 para la ficha de vida del hallazgo (TASK-585; O-11 y O-12): una sola lectura
 * auditada del servidor, `GET /loop/findings/{finding_state_id}/timeline` (`findings.read`), que
 * se pide al abrir la ficha y nunca por refresco (`audited` en `state.query`). La consola no
 * compone la línea de tiempo con otras lecturas ni recalcula horas, tiempos o plazos: los da el
 * servidor, agregados en una zona unipersonal (BR-LAZ-06).
 */
import type { components } from '@/client/schema';
import type { Notice } from '@/components/NoticeStack/notice';
import type { HttpError } from '@/ports/errors/map';
import { defineScreenPort, isPortError } from './base';

export type FindingTimeline = components['schemas']['FindingTimelineOut'];
export type TimelineMilestone = components['schemas']['TimelineMilestoneOut'];
export type TimelineSummary = components['schemas']['TimelineSummaryOut'];
export type MilestoneKind = TimelineMilestone['kind'];

export const timelinePort = defineScreenPort('SCR-08', {
  timeline: {
    kind: 'read',
    status: 'fixed',
    operationId: 'get_timeline_loop_findings__finding_state_id__timeline_get',
    permission: 'findings.read',
  },
});

/** Los catorce tipos de hito, en el orden de la lista cerrada del servidor. */
export const MILESTONE_KINDS: readonly MilestoneKind[] = [
  'finding_received',
  'late_segment_attached',
  'classification',
  'review_resolution',
  'action_assigned',
  'action_evidence_added',
  'action_overdue',
  'closure',
  'closure_attachment_unrecoverable',
  'root_cause_analysis',
  'reopening',
  'unclosable_state_declared',
  'unclosable_review',
  'other',
];

/**
 * El error de la lectura como `Notice` de UI-19: la respuesta HTTP si la hubo; si no, el código
 * local con el mismo comportamiento del mapa cerrado (BR-APP-71).
 */
export function timelineNotice(error: unknown): Notice {
  const http: HttpError =
    isPortError(error) && error.failure.kind === 'http'
      ? error.failure.error
      : { code: 'temporarily_unavailable', status: 0 };
  return { id: 'timeline', error: http };
}
