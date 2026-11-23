/**
 * La ficha de vida del hallazgo (SCR-08, grupo `timeline`; TASK-585, O-11 y O-12). Para **ver**,
 * no para llenar: arriba, una línea de resumen («Recibido → cerrado en N días hábiles ·
 * clasificación a tiempo · acción a tiempo»); debajo, la línea de tiempo vertical con un hito por
 * registro del expediente, cada uno con su icono y su título por clave de texto, la hora en la zona
 * de la planta (UI-25), el tiempo desde el paso anterior, el plazo con texto (nunca solo color),
 * quién lo hizo (usuario de la plataforma y su rol) y el sello del expediente copiable.
 *
 * Selección explícita de campos (BR-APP-40): ningún componente serializa el objeto recibido. Toda
 * hora y todo tiempo se muestran tal como los da el servidor, agregados en una zona unipersonal
 * (BR-LAZ-06): la consola no recalcula nada. Nunca compara personas ni ordena por quién.
 */
import { useState, type ReactNode } from 'react';
import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import { reasonList } from '@/ports/scr08Detail';
import type {
  FindingTimeline,
  MilestoneKind,
  TimelineMilestone,
  TimelineSummary,
} from '@/ports/scr08Timeline';
import { text } from '@/texts';
import { formatDuration } from '@/texts/duration';
import { hasLabel, label, labelInline } from '@/texts/labels';

import styles from './timeline.module.css';

/** Un símbolo por tipo de hito (decorativo: el título dice lo mismo con texto). */
const ICONS: Readonly<Record<MilestoneKind, string>> = {
  finding_received: '◉',
  late_segment_attached: '◎',
  classification: '✓',
  review_resolution: '☑',
  action_assigned: '⚙',
  action_evidence_added: '✎',
  action_overdue: '⏰',
  closure: '✔',
  closure_attachment_unrecoverable: '⚠',
  root_cause_analysis: '⌕',
  reopening: '↺',
  unclosable_state_declared: '⛔',
  unclosable_review: '⟳',
  other: '•',
};

type Details = TimelineMilestone['details'];
type DeadlineState = 'met' | 'late' | 'pending';

function deadlineState(met: boolean | null): DeadlineState {
  return met === null ? 'pending' : met ? 'met' : 'late';
}

function labelled(enumName: string, value: string | null): string | null {
  if (value === null) return null;
  return hasLabel(enumName, value) ? label(enumName, value) : text('common.unknown');
}

function outcomeLabel(kind: MilestoneKind, outcome: string | null): string | null {
  const table =
    kind === 'review_resolution'
      ? 'review_resolution_outcome'
      : kind === 'unclosable_review'
        ? 'unclosable_review_outcome'
        : 'classification_outcome';
  return labelled(table, outcome);
}

/** La salida de la clasificación cuya lista de motivos usa una resolución de la cola. */
const REVIEW_REASONS: Readonly<Record<string, string>> = {
  review_confirmed: 'confirmed',
  review_discarded: 'false_positive',
};

function reasonCategory(details: Details): string | null {
  if (details.reason_category === null) return null;
  const outcome =
    details.outcome !== null && Object.hasOwn(REVIEW_REASONS, details.outcome)
      ? (REVIEW_REASONS[details.outcome] ?? null)
      : details.outcome;
  const list = outcome === null ? null : reasonList(outcome);
  return list === null || !hasLabel(list, details.reason_category)
    ? text('common.unknown')
    : label(list, details.reason_category);
}

/** «<categoría> · <texto>»: el motivo con su categoría etiquetada, o solo el texto. */
function reasonLabel(details: Details): string | null {
  const parts = [reasonCategory(details), details.reason_text].filter(
    (part): part is string => part !== null,
  );
  return parts.length === 0 ? null : parts.join(' · ');
}

function Field({ name, children }: { readonly name: string; readonly children: ReactNode }) {
  return (
    <>
      <dt>{name}</dt>
      <dd>{children}</dd>
    </>
  );
}

function When({ instant, timeZone }: { readonly instant: string; readonly timeZone: string }) {
  return <TimestampWithOffset instant={instant} timeZone={timeZone} />;
}

/** Los campos elegidos del hito; solo los que trae, en un orden fijo. */
function MilestoneDetails({
  milestone,
  timeZone,
}: {
  readonly milestone: TimelineMilestone;
  readonly timeZone: string;
}) {
  const { details, kind } = milestone;
  const rows: { key: string; name: string; value: ReactNode }[] = [];
  const add = (key: string, name: string, value: ReactNode | null) => {
    if (value !== null) rows.push({ key, name, value });
  };
  add('outcome', text('scr08.timeline.outcome'), outcomeLabel(kind, details.outcome));
  add('reason', text('scr08.timeline.reason'), reasonLabel(details));
  add(
    'action_type',
    text('scr08.timeline.action_type'),
    labelled('action_type', details.action_type),
  );
  add('description', text('scr08.timeline.description'), details.description);
  add('expected', text('scr08.timeline.expected'), details.expected_outcome);
  add('assignee', text('scr08.timeline.assignee'), details.assignee_display_name);
  add(
    'due_at',
    text('scr08.timeline.due_at'),
    details.due_at === null ? null : <When instant={details.due_at} timeZone={timeZone} />,
  );
  add(
    'performed_at',
    text('scr08.timeline.performed_at'),
    details.performed_at === null ? null : (
      <When instant={details.performed_at} timeZone={timeZone} />
    ),
  );
  add('performed_by_role', text('scr08.timeline.performed_by_role'), details.performed_by_role);
  add('verification', text('scr08.timeline.verification'), details.verification_note);
  add(
    'escalation',
    text('scr08.timeline.escalation'),
    labelled('escalation_level', details.escalation_level),
  );
  add(
    'days_overdue',
    text('scr08.timeline.days_overdue'),
    details.days_overdue === null ? null : String(details.days_overdue),
  );
  add('license', text('scr08.timeline.license'), details.professional_license);
  add(
    'cause_category',
    text('scr08.timeline.cause_category'),
    labelled('cause_category', details.root_cause_category),
  );
  add('statement', text('scr08.timeline.statement'), details.root_cause_statement);
  add('conclusion', text('scr08.timeline.conclusion'), details.conclusion);
  add(
    'from_state',
    text('scr08.timeline.from_state'),
    labelled('finding_lifecycle_state', details.from_state),
  );
  add('compensatory', text('scr08.timeline.compensatory'), details.compensatory_measure);
  add(
    'review_due_at',
    text('scr08.timeline.review_due_at'),
    details.review_due_at === null ? null : (
      <When instant={details.review_due_at} timeZone={timeZone} />
    ),
  );
  add('review_note', text('scr08.timeline.review_note'), details.review_note);
  if (kind === 'other')
    add('record_type', text('scr08.timeline.record_type'), milestone.record_type);
  if (rows.length === 0) return null;
  return (
    <dl className={styles.details}>
      {rows.map((row) => (
        <div key={row.key} data-part={`detail_${row.key}`} className={styles.row}>
          <Field name={row.name}>{row.value}</Field>
        </div>
      ))}
    </dl>
  );
}

/** El sello del expediente, copiable. */
function RecordId({ recordId }: { readonly recordId: string }) {
  const [copied, setCopied] = useState(false);
  return (
    <p className={styles.record}>
      <span>{text('scr08.timeline.record_id')}: </span>
      <code data-part="record_id">{recordId}</code>{' '}
      <button
        type="button"
        className={styles.copy}
        data-part="copy_record_id"
        onClick={() => {
          void navigator.clipboard.writeText(recordId).then(() => {
            setCopied(true);
          });
        }}
      >
        {text('scr08.timeline.copy')}
      </button>
      <span role="status" data-part="copied">
        {copied ? text('scr08.timeline.copied') : ''}
      </span>
    </p>
  );
}

function Milestone({
  milestone,
  timeZone,
}: {
  readonly milestone: TimelineMilestone;
  readonly timeZone: string;
}) {
  const { deadline, actor } = milestone;
  const title = text(`scr08.timeline.kind.${milestone.kind}`);
  const elapsed =
    milestone.elapsed_ms_since_previous === null
      ? null
      : formatDuration(milestone.elapsed_ms_since_previous);
  return (
    <li className={styles.milestone} data-part="milestone" data-kind={milestone.kind}>
      <span className={styles.icon} aria-hidden="true" data-part="icon">
        {ICONS[milestone.kind]}
      </span>
      <div className={styles.body}>
        <h3 data-part="milestone_title">{title}</h3>
        <p className={styles.when}>
          <span data-part="at">
            <When instant={milestone.at} timeZone={timeZone} />
          </span>
          {elapsed !== null && (
            <span data-part="elapsed" data-ms={String(milestone.elapsed_ms_since_previous)}>
              {` · +${elapsed} ${text('scr08.timeline.elapsed')}`}
            </span>
          )}
        </p>
        {deadline !== null && (
          <p
            className={styles.deadline}
            data-part="deadline"
            data-state={deadlineState(deadline.met)}
          >
            <span data-part="deadline_state">
              {text(`scr08.timeline.deadline_${deadlineState(deadline.met)}`)}
            </span>
            {' · '}
            {text('scr08.timeline.deadline')}:{' '}
            <When instant={deadline.due_at} timeZone={timeZone} />
          </p>
        )}
        {actor !== null && (
          <p data-part="actor">
            {text('scr08.timeline.by')}: <span data-part="actor_name">{actor.display_name}</span>
            {actor.role_in_use !== null && (
              <>
                {' · '}
                <span data-part="actor_role" data-value={actor.role_in_use}>
                  {labelInline('role', actor.role_in_use)}
                </span>
              </>
            )}
          </p>
        )}
        <MilestoneDetails milestone={milestone} timeZone={timeZone} />
        <RecordId recordId={milestone.record_id} />
      </div>
    </li>
  );
}

/** «Recibido → cerrado en N días hábiles · clasificación a tiempo · acción a tiempo». */
function Summary({ summary }: { readonly summary: TimelineSummary }) {
  const days = summary.business_days_to_close;
  const parts: { key: string; value: string; state?: DeadlineState }[] = [];
  if (summary.closed_at !== null && days !== null) {
    parts.push({
      key: 'summary_closed',
      value: `${text('scr08.timeline.summary_closed')} ${String(days)} ${text(
        days === 1 ? 'scr08.timeline.business_day' : 'scr08.timeline.business_days',
      )}`,
    });
  } else {
    parts.push({ key: 'summary_open', value: text('scr08.timeline.summary_open') });
  }
  if (summary.classification_met !== null) {
    parts.push({
      key: 'summary_classification',
      value: text(
        summary.classification_met
          ? 'scr08.timeline.classification_met'
          : 'scr08.timeline.classification_late',
      ),
      state: deadlineState(summary.classification_met),
    });
  }
  if (summary.action_met !== null) {
    parts.push({
      key: 'summary_action',
      value: text(summary.action_met ? 'scr08.timeline.action_met' : 'scr08.timeline.action_late'),
      state: deadlineState(summary.action_met),
    });
  }
  return (
    <section
      className={styles.summary}
      aria-label={text('scr08.timeline.summary')}
      data-part="summary"
    >
      <h2>{text('scr08.timeline.summary')}</h2>
      <p>
        {parts.map((part, index) => (
          <span key={part.key}>
            {index > 0 && ' · '}
            <span data-part={part.key} data-state={part.state}>
              {part.value}
            </span>
          </span>
        ))}
      </p>
    </section>
  );
}

/** La ficha completa, sin lecturas: la usa la página. */
export function TimelineView({
  timeline,
  timeZone,
}: {
  readonly timeline: FindingTimeline;
  readonly timeZone: string;
}) {
  return (
    <>
      <Summary summary={timeline.summary} />
      {timeline.time_aggregated && timeline.aggregation_window_minutes !== null && (
        <p role="note" data-part="aggregated">
          {`${text('scr08.timeline.aggregated')} ${String(timeline.aggregation_window_minutes)} ${text(
            'scr08.timeline.aggregated_minutes',
          )}`}
        </p>
      )}
      <section aria-label={text('scr08.timeline.milestones')} data-part="milestones">
        <h2>{text('scr08.timeline.milestones')}</h2>
        <ol className={styles.timeline}>
          {timeline.milestones.map((milestone) => (
            <Milestone key={milestone.record_id} milestone={milestone} timeZone={timeZone} />
          ))}
        </ol>
        {timeline.truncated && (
          <p role="note" data-part="truncated">
            {text('scr08.timeline.truncated')}
          </p>
        )}
      </section>
    </>
  );
}
