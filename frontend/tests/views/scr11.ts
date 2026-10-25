/**
 * RV-15 (SCR-11): las vistas de lectura del análisis de causa raíz con los componentes de
 * producción: el contexto objetivo de la zona, los porqués guardados con «pendiente de
 * información» y la firma, el borrador del asistente con sus citas y el rechazo de los
 * validadores. Prohibido mostrar: chat libre y causa atribuida a persona (ningún campo de persona
 * en `allowedFields`; PR-APP-07 lo comprueba).
 */
import fc from 'fast-check';
import { createElement, Fragment } from 'react';

import {
  AssistantDraftPanel,
  ValidatorRejection,
} from '@/components/AssistantDraftPanel/AssistantDraftPanel';
import { rejectionOf, type AssistantDraft } from '@/components/AssistantDraftPanel/state';
import {
  PendingInformation,
  SavedWhys,
  SignedAnalysis,
} from '@/pages/scr11/root_cause/AnalysisViews';
import { ZoneContextPanel } from '@/pages/scr11/root_cause/ZoneContextPanel';
import type { Analysis, AssistantRequest, ZoneContext } from '@/ports/scr11RootCause';

import { readView } from '../generators';
import type { ViewRegistration } from './types';

const TIME_ZONE = 'America/Bogota';
const noop = () => undefined;

/** Una solicitud lista: siempre con borrador (el generador puede dar `draft: null`). */
const readyRequest = fc
  .tuple(readView('AssistantRequestOut'), readView('DraftOut'))
  .map(([request, draft]) => ({ ...request, draft: request.draft ?? draft }));

export default {
  zoneContext: {
    screen: 'SCR-11',
    view: 'RV-15',
    data: readView('ZoneContextOut'),
    render: (data) =>
      createElement(ZoneContextPanel, {
        context: data as unknown as ZoneContext,
        timeZone: TIME_ZONE,
      }),
    allowedFields: [
      'family',
      'tier',
      'standard_title_es',
      'standard_version',
      'standard_text',
      'observability_state_at_event',
      'period',
      'from',
      'to',
      'recurrences_in_period',
      'coverage_summary_period',
      'observable_ms',
      'degraded_ms',
      'not_observable_ms',
      'no_communication_ms',
      'never_reported_ms',
      'zone_not_active_ms',
      'node_state',
      'communication_state',
      'last_heartbeat_at',
      'signals_by_role',
      'role',
      'value',
      'valid',
      'episode_durations',
      'episode_ms',
      'condition_ms',
      'finding_counts_by_family_in_period',
      'count',
      'previous_actions',
      'action_type',
      'outcome',
      'closed_at',
      'recurrence_after_days',
      'previous_cause_categories',
      'category',
    ],
  },
  analysis: {
    screen: 'SCR-11',
    view: 'RV-15',
    data: readView('vigia_platform__loop__adapters__http__root_cause_routes__AnalysisOut'),
    render: (data) => {
      const analysis = data as unknown as Analysis;
      return createElement(
        Fragment,
        null,
        createElement(SavedWhys, { analysis }),
        createElement(PendingInformation, { items: analysis.pending_information }),
        createElement(SignedAnalysis, { analysis, timeZone: TIME_ZONE }),
      );
    },
    allowedFields: [
      'why_steps',
      'statement',
      'category',
      'pending_information',
      'signer',
      'display_name',
      'professional_license',
      'signed_at',
      'record_id',
      'root_cause_category',
      'root_cause_statement',
      'conclusion',
    ],
  },
  assistantDraft: {
    screen: 'SCR-11',
    view: 'RV-15',
    data: readyRequest,
    render: (data) => {
      const request = data as unknown as AssistantRequest;
      return createElement(AssistantDraftPanel, {
        state: {
          status: 'ready',
          requestId: request.request_id,
          draft: request.draft as AssistantDraft,
        },
        onRequest: noop,
        onUseDraft: noop,
      });
    },
    allowedFields: [
      'draft',
      'draft_text',
      'citations',
      'kind',
      'id',
      'quoted_fact_es',
      'insufficient_evidence_declared',
      'proposed_cause_category',
      'model',
      'model_version',
    ],
  },
  validatorRejection: {
    screen: 'SCR-11',
    view: 'RV-15',
    data: fc.record({
      detail_code: fc.constantFrom(
        'loop_blocked_vocabulary_term',
        'loop_person_attribution_detected',
        'loop_assistant_output_rejected',
      ),
      rejection: fc.record({
        code: fc.constantFrom('blocked_vocabulary_term', 'person_attribution_detected'),
        term: fc.stringMatching(/^[a-z]{8,24}$/),
        suggestion: fc.stringMatching(/^[a-z]{8,40}$/),
      }),
    }),
    render: (data) =>
      createElement(ValidatorRejection, {
        rejection: rejectionOf(
          data,
          typeof data.detail_code === 'string' ? data.detail_code : null,
        ),
      }),
    allowedFields: ['rejection', 'code', 'term', 'suggestion', 'detail_code'],
  },
} satisfies Record<string, ViewRegistration>;
