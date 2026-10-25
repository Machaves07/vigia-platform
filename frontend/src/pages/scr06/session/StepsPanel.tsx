/**
 * Pasos del comisionamiento (IF-13 paso 2, RV-09, BR-APP-45): abrir con tipo y responsable, cerrar
 * y corregir las horas con motivo. Cada paso muestra las marcas del servidor (`started_at`,
 * `ended_at`, `effective_duration_ms`) y, mientras está abierto, el cronómetro de presentación
 * que parte del `started_at` del servidor (nunca del montaje ni del reloj del navegador).
 *
 * Sin `onOpen` ni `onClose` la lista es de solo lectura: es la forma en que la vista RV-09 la
 * pinta en las metapropiedades de PR-APP-07 y PR-APP-10.
 */
import { useId, useState } from 'react';

import type { Notice } from '@/components/NoticeStack/notice';
import { ReasonInput } from '@/pages/scr06/common/ReasonInput';
import { ServerTimestamp } from '@/pages/scr06/common/ServerTimestamp';
import { TextField } from '@/pages/scr06/common/TextField';
import { formatDurationMs } from '@/pages/scr06/common/format';
import type { FieldIssues } from '@/pages/scr06/common/fieldIssues';
import type { StepKind, WalkTestSession, WalkTestStep } from '@/ports/scr06Commissioning';
import type { Clock } from '@/state/clock';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import { Stopwatch } from './Stopwatch';
import { STEP_KINDS } from './stepKinds';
import styles from './session.module.css';

/** Corrección de las horas de un paso: motivo obligatorio y marcas opcionales (U-03 §3.3). */
export interface StepCorrection {
  readonly started_at: string | null;
  readonly ended_at: string | null;
  readonly reason_es: string;
}

export interface StepsPanelProps {
  readonly session: WalkTestSession;
  /** Con reloj, el paso en curso muestra su cronómetro desde el `started_at` del servidor. */
  readonly clock?: Clock;
  readonly onOpen?: (input: {
    readonly step_kind: StepKind;
    readonly responsible_user_id: string;
  }) => void;
  readonly onClose?: (step: WalkTestStep, correction: StepCorrection | null) => void;
  readonly busy?: boolean;
  /** Error estructural del espejo del esquema por campo (responsable, horas corregidas). */
  readonly issues?: FieldIssues;
  /** Aviso del servidor anclado al motivo de la corrección. */
  readonly correctionNotice?: Notice | undefined;
}

/** Formulario de apertura de un paso: tipo y responsable. */
function OpenStepForm({
  onOpen,
  busy,
  issues,
}: {
  readonly onOpen: (input: {
    readonly step_kind: StepKind;
    readonly responsible_user_id: string;
  }) => void;
  readonly busy: boolean;
  readonly issues: FieldIssues;
}) {
  const [kind, setKind] = useState<StepKind>('physical_setup');
  const [responsible, setResponsible] = useState('');
  const selectId = useId();
  return (
    <form
      className={styles.form}
      aria-label={text('scr06.steps.open.submit')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        onOpen({ step_kind: kind, responsible_user_id: responsible.trim() });
      }}
    >
      <div className={styles.field}>
        <label htmlFor={selectId} className={styles.label}>
          {text('scr06.steps.kind')}
        </label>
        <select
          id={selectId}
          className={styles.control}
          value={kind}
          onChange={(event) => {
            setKind(event.target.value as StepKind);
          }}
        >
          {STEP_KINDS.map((value) => (
            <option key={value} value={value}>
              {label('step_kind', value)}
            </option>
          ))}
        </select>
      </div>
      <TextField
        label={text('scr06.steps.responsible')}
        hint={text('scr06.steps.responsible_hint')}
        value={responsible}
        onChange={setResponsible}
        errorKey={issues.get('responsible_user_id') ?? null}
        required
      />
      <button type="submit" className={styles.button} disabled={busy} data-part="open_step">
        {text(busy ? 'scr06.steps.open.submitting' : 'scr06.steps.open.submit')}
      </button>
    </form>
  );
}

/** Formulario de cierre de un paso abierto: cierre simple o con corrección de horas y motivo. */
function CloseStepForm({
  step,
  onClose,
  busy,
  issues,
  correctionNotice,
}: {
  readonly step: WalkTestStep;
  readonly onClose: (step: WalkTestStep, correction: StepCorrection | null) => void;
  readonly busy: boolean;
  readonly issues: FieldIssues;
  readonly correctionNotice?: Notice | undefined;
}) {
  const [expanded, setExpanded] = useState(false);
  const [reason, setReason] = useState('');
  const [startedAt, setStartedAt] = useState('');
  const [endedAt, setEndedAt] = useState('');
  const [showErrors, setShowErrors] = useState(false);
  const field = `steps.${step.step_id}.correction.reason_es`;
  if (!expanded) {
    return (
      <button
        type="button"
        className={styles.button}
        disabled={busy}
        onClick={() => {
          setExpanded(true);
        }}
        data-part="close_step"
      >
        {text('scr06.steps.close')}
      </button>
    );
  }
  return (
    <form
      className={styles.form}
      aria-label={text('scr06.steps.correction.title')}
      noValidate
      onSubmit={(event) => {
        event.preventDefault();
        if (reason.trim() === '') {
          setShowErrors(true);
          return;
        }
        onClose(step, {
          started_at: startedAt.trim() === '' ? null : startedAt.trim(),
          ended_at: endedAt.trim() === '' ? null : endedAt.trim(),
          reason_es: reason,
        });
      }}
    >
      <h4 className={styles.subtitle}>{text('scr06.steps.correction.title')}</h4>
      <p className={styles.hint}>{text('scr06.steps.correction.hint')}</p>
      <TextField
        label={text('scr06.steps.correction.started_at')}
        value={startedAt}
        onChange={setStartedAt}
        errorKey={issues.get('correction.started_at') ?? null}
      />
      <TextField
        label={text('scr06.steps.correction.ended_at')}
        value={endedAt}
        onChange={setEndedAt}
        errorKey={issues.get('correction.ended_at') ?? null}
      />
      <ReasonInput
        label={text('scr06.steps.correction.reason')}
        value={reason}
        onChange={setReason}
        name={field}
        notice={correctionNotice}
        errorKey={issues.get('correction.reason_es') ?? null}
        showErrors={showErrors}
      />
      <div className={styles.actions}>
        <button
          type="button"
          className={styles.button}
          disabled={busy}
          data-part="close_without_correction"
          onClick={() => {
            onClose(step, null);
          }}
        >
          {text('scr06.steps.close')}
        </button>
        <button
          type="submit"
          className={styles.button}
          disabled={busy}
          data-part="close_with_correction"
        >
          {text(busy ? 'scr06.steps.close.submitting' : 'scr06.steps.correction.submit')}
        </button>
      </div>
    </form>
  );
}

function StepRow({
  step,
  clock,
  onClose,
  busy,
  issues,
  correctionNotice,
}: {
  readonly step: WalkTestStep;
  readonly clock: Clock | undefined;
  readonly onClose: ((step: WalkTestStep, correction: StepCorrection | null) => void) | undefined;
  readonly busy: boolean;
  readonly issues: FieldIssues;
  readonly correctionNotice: Notice | undefined;
}) {
  const open = step.ended_at === null;
  return (
    <tr data-part="step" data-step-id={step.step_id} data-open={open}>
      <th scope="row">{label('step_kind', step.step_kind)}</th>
      <td>
        <code data-part="responsible">{step.responsible_user_id}</code>
      </td>
      <td>
        <ServerTimestamp instant={step.started_at} />
      </td>
      <td data-part="step_end">
        {step.ended_at === null ? (
          <>
            <span>{text('scr06.steps.open_step')}</span>{' '}
            {clock === undefined ? null : <Stopwatch startedAt={step.started_at} clock={clock} />}
          </>
        ) : (
          <ServerTimestamp instant={step.ended_at} />
        )}
      </td>
      <td data-part="step_duration">
        {step.effective_duration_ms === null ? '—' : formatDurationMs(step.effective_duration_ms)}
      </td>
      <td data-part="step_correction">
        {step.correction === null ? (
          '—'
        ) : (
          <span data-part="correction_reason">{step.correction.reason_es}</span>
        )}
      </td>
      {onClose === undefined ? null : (
        <td>
          {open ? (
            <CloseStepForm
              step={step}
              onClose={onClose}
              busy={busy}
              issues={issues}
              correctionNotice={correctionNotice}
            />
          ) : null}
        </td>
      )}
    </tr>
  );
}

export function StepsPanel({
  session,
  clock,
  onOpen,
  onClose,
  busy = false,
  issues = new Map(),
  correctionNotice,
}: StepsPanelProps) {
  const headingId = useId();
  return (
    <section className={styles.panel} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.subtitle}>
        {text('scr06.steps.title')}
      </h3>
      {session.steps.length === 0 ? (
        <p className={styles.empty} data-part="steps_empty">
          {text('scr06.steps.empty')}
        </p>
      ) : (
        <table className={styles.table} aria-labelledby={headingId}>
          <thead>
            <tr>
              <th scope="col">{text('scr06.steps.kind')}</th>
              <th scope="col">{text('scr06.steps.responsible')}</th>
              <th scope="col">{text('scr06.steps.started_at')}</th>
              <th scope="col">{text('scr06.steps.ended_at')}</th>
              <th scope="col">{text('scr06.steps.duration')}</th>
              <th scope="col">{text('scr06.steps.correction')}</th>
              {onClose === undefined ? null : <th scope="col">{text('scr06.steps.close')}</th>}
            </tr>
          </thead>
          <tbody>
            {session.steps.map((step) => (
              <StepRow
                key={step.step_id}
                step={step}
                clock={clock}
                onClose={onClose}
                busy={busy}
                issues={issues}
                correctionNotice={correctionNotice}
              />
            ))}
          </tbody>
        </table>
      )}
      {onOpen === undefined ? null : <OpenStepForm onOpen={onOpen} busy={busy} issues={issues} />}
    </section>
  );
}
