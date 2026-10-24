/**
 * UI-15 `StepWizard` sobre el reductor puro `stepWizard.ts` (BR-APP-45, 20; PR-APP-13).
 *
 * - Un paso por etapa; «Siguiente» queda deshabilitado hasta que la pantalla marca el paso en
 *   pantalla como completo (`complete`), y la lista de pasos solo deja volver a los alcanzables.
 * - Tras el último paso, el resumen; «Enviar» solo existe en el resumen.
 * - «Guardar sin terminar» solo aparece con `serverProgressRef` (estado «en curso» del servidor:
 *   causa raíz en U-04, walk-test en U-03); sin él no hay botón.
 * - Con cambios sin enviar (`dirty`), salir de la pantalla pide confirmación (`useLeaveGuard`);
 *   tras enviar, ya no. Exige el enrutador en modo de datos.
 */
import { useEffect, useId, useReducer, useRef, useState } from 'react';
import type { ReactNode } from 'react';

import {
  canAdvance,
  canGoTo,
  canSavePartial,
  canSubmit,
  createStepWizard,
  isSummary,
  stepWizardReducer,
} from '@/state/reducers/stepWizard';
import { useLeaveGuard } from '@/state/reducers/useLeaveGuard';
import { text } from '@/texts';

import styles from './StepWizard.module.css';

interface WizardStep {
  readonly id: string;
  /** Título del paso, ya resuelto desde el catálogo por la pantalla. */
  readonly title: string;
  readonly content: ReactNode;
  /** La pantalla da el paso por respondido. */
  readonly complete: boolean;
}

export interface StepWizardProps {
  readonly title: string;
  readonly steps: readonly WizardStep[];
  readonly summary: ReactNode;
  /** Hay cambios sin enviar (`FormDraft.dirty`). */
  readonly dirty: boolean;
  /** `FormDraft.server_progress_ref`; `null` si la operación no tiene estado «en curso». */
  readonly serverProgressRef?: string | null;
  readonly onSubmit: () => Promise<void>;
  readonly onSavePartial?: () => Promise<void>;
  readonly onStepCompleted?: (index: number) => void;
}

type Outcome = 'submit_failed' | 'save_failed' | 'saved_partial' | null;

function LeaveDialog({ onStay, onLeave }: { onStay: () => void; onLeave: () => void }) {
  const titleId = useId();
  const bodyId = useId();
  const stayRef = useRef<HTMLButtonElement>(null);
  useEffect(() => {
    stayRef.current?.focus();
  }, []);
  return (
    <div
      className={styles.dialog}
      role="alertdialog"
      aria-modal="true"
      aria-labelledby={titleId}
      aria-describedby={bodyId}
    >
      <h3 id={titleId}>{text('components_structure.wizard.leave_title')}</h3>
      <p id={bodyId}>{text('components_structure.wizard.leave_body')}</p>
      <div className={styles.actions}>
        <button ref={stayRef} type="button" className={styles.button} onClick={onStay}>
          {text('components_structure.wizard.stay')}
        </button>
        <button type="button" className={styles.button} onClick={onLeave}>
          {text('components_structure.wizard.leave')}
        </button>
      </div>
    </div>
  );
}

export function StepWizard({
  title,
  steps,
  summary,
  dirty,
  serverProgressRef = null,
  onSubmit,
  onSavePartial,
  onStepCompleted,
}: StepWizardProps) {
  const [state, dispatch] = useReducer(stepWizardReducer, undefined, () =>
    createStepWizard(steps.length, serverProgressRef),
  );
  const [outcome, setOutcome] = useState<Outcome>(null);
  const headingId = useId();
  const guard = useLeaveGuard(dirty && !state.submitted);

  const { position } = state;
  const current = steps[position];
  const onStep = current !== undefined;
  const currentComplete = current?.complete ?? false;

  // El reductor solo cambia el paso en pantalla: completar o invalidar es idempotente.
  useEffect(() => {
    if (!onStep) return;
    dispatch({ type: currentComplete ? 'complete_step' : 'invalidate_step', index: position });
  }, [onStep, currentComplete, position]);

  useEffect(() => {
    if (serverProgressRef !== null) dispatch({ type: 'server_progress', ref: serverProgressRef });
  }, [serverProgressRef]);

  const next = () => {
    if (!canAdvance(state)) return;
    onStepCompleted?.(position);
    setOutcome(null);
    dispatch({ type: 'next' });
  };

  const submit = () => {
    if (!canSubmit(state)) return;
    setOutcome(null);
    dispatch({ type: 'submit' });
    onSubmit().then(
      () => {
        dispatch({ type: 'settled', ok: true });
      },
      () => {
        setOutcome('submit_failed');
        dispatch({ type: 'settled', ok: false });
      },
    );
  };

  const savePartial = () => {
    if (onSavePartial === undefined || !canSavePartial(state)) return;
    setOutcome(null);
    dispatch({ type: 'save_partial' });
    onSavePartial().then(
      () => {
        setOutcome('saved_partial');
        dispatch({ type: 'settled', ok: true });
      },
      () => {
        setOutcome('save_failed');
        dispatch({ type: 'settled', ok: false });
      },
    );
  };

  const summaryShown = isSummary(state);
  const stepTitle = summaryShown
    ? text('components_structure.wizard.summary')
    : `${text('components_structure.wizard.step')} ${String(position + 1)} ${text(
        'components_structure.wizard.of',
      )} ${String(steps.length)}: ${current?.title ?? ''}`;
  const showSavePartial =
    onSavePartial !== undefined && state.serverProgressRef !== null && !state.submitted;

  return (
    <section className={styles.wizard} aria-labelledby={headingId}>
      <h2 id={headingId} className={styles.title}>
        {title}
      </h2>
      <ol className={styles.steps} aria-label={text('components_structure.wizard.steps_label')}>
        {[...steps.map((step) => step.title), text('components_structure.wizard.summary')].map(
          (label, index) => {
            const isCurrent = index === position;
            const reachable = !isCurrent && !state.submitted && canGoTo(state, index);
            return (
              <li
                key={steps[index]?.id ?? 'summary'}
                aria-current={isCurrent ? 'step' : undefined}
                data-complete={state.completed[index] === true}
              >
                {reachable ? (
                  <button
                    type="button"
                    className={styles.link}
                    onClick={() => {
                      dispatch({ type: 'go_to', index });
                    }}
                  >
                    {label}
                  </button>
                ) : (
                  label
                )}
              </li>
            );
          },
        )}
      </ol>
      <h3 className={styles.stepTitle} data-part="step-title">
        {stepTitle}
      </h3>
      <div className={styles.body}>{summaryShown ? summary : current?.content}</div>
      {state.submitted ? (
        <p role="status" data-part="submitted">
          {text('components_structure.wizard.submitted')}
        </p>
      ) : (
        <div className={styles.actions}>
          <button
            type="button"
            className={styles.button}
            disabled={position === 0 || state.pending !== null}
            onClick={() => {
              setOutcome(null);
              dispatch({ type: 'back' });
            }}
          >
            {text('components_structure.wizard.back')}
          </button>
          {summaryShown ? (
            <button
              type="button"
              className={styles.button}
              disabled={!canSubmit(state)}
              onClick={submit}
            >
              {text(
                state.pending === 'submit'
                  ? 'components_structure.wizard.submitting'
                  : 'components_structure.wizard.submit',
              )}
            </button>
          ) : (
            <button
              type="button"
              className={styles.button}
              disabled={!canAdvance(state)}
              aria-describedby={canAdvance(state) ? undefined : `${headingId}-incomplete`}
              onClick={next}
            >
              {text('components_structure.wizard.next')}
            </button>
          )}
          {showSavePartial && (
            <button
              type="button"
              className={styles.button}
              disabled={!canSavePartial(state)}
              onClick={savePartial}
            >
              {text(
                state.pending === 'save_partial'
                  ? 'components_structure.wizard.saving_partial'
                  : 'components_structure.wizard.save_partial',
              )}
            </button>
          )}
          {!summaryShown && !canAdvance(state) && (
            <p id={`${headingId}-incomplete`} className={styles.hint} data-part="incomplete">
              {text('components_structure.wizard.step_incomplete')}
            </p>
          )}
        </div>
      )}
      {outcome !== null && (
        <p role={outcome === 'saved_partial' ? 'status' : 'alert'} data-part="outcome">
          {text(`components_structure.wizard.${outcome}`)}
        </p>
      )}
      {guard.confirming && <LeaveDialog onStay={guard.stay} onLeave={guard.leave} />}
    </section>
  );
}
