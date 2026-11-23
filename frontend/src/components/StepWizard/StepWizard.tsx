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
 * - Barra legible (TASK-581, O-5): cada paso en su `<li>`, un separador `aria-hidden` entre pasos
 *   y «Paso x de n» con sus números en `data-part="progress-current"` y `"progress-total"`.
 * - Pasos dinámicos (TASK-581): si la pantalla cambia la lista, el reductor la sigue por `id`
 *   (`reshape`). Un paso con `onNext` deja pulsar «Siguiente» sin estar completo: la pantalla lo
 *   guarda y el flujo avanza solo si, tras guardarlo, el paso queda completo.
 */
import { useEffect, useId, useLayoutEffect, useReducer, useRef, useState } from 'react';
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
  /**
   * «Siguiente» con el paso sin completar: la pantalla lo guarda (y muestra sus errores si no
   * puede). El flujo avanza solo si, al resolverse, el paso ya está completo.
   */
  readonly onNext?: (() => Promise<void>) | undefined;
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
  /** «Siguiente» guardando el paso `id`; `saved` cuando la pantalla terminó de guardarlo. */
  const [advancing, setAdvancing] = useState<{ id: string; saved: boolean } | null>(null);
  /** El último guardado con «Siguiente» ya resuelto: se atiende una sola vez. */
  const handled = useRef<object | null>(null);
  const headingId = useId();
  const guard = useLeaveGuard(dirty && !state.submitted);

  // La lista cambió (p. ej. un porqué guardado añade el siguiente): cada paso conserva su estado
  // por `id`. Antes de pintar, para que la posición y la lista nunca se desfasen en pantalla.
  const ids = steps.map((step) => step.id);
  const idsKey = JSON.stringify(ids);
  const previousIds = useRef(idsKey);
  useLayoutEffect(() => {
    if (previousIds.current === idsKey) return;
    const before = JSON.parse(previousIds.current) as string[];
    previousIds.current = idsKey;
    const from = (JSON.parse(idsKey) as string[]).map((id) => {
      const index = before.indexOf(id);
      return index >= 0 ? index : null;
    });
    dispatch({ type: 'reshape', from });
  }, [idsKey]);

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

  // Tras guardar con «Siguiente», el paso avanza solo si la pantalla ya lo da por completo.
  useEffect(() => {
    if (advancing === null || !advancing.saved || handled.current === advancing) return;
    handled.current = advancing;
    if (advancing.id !== current?.id || !currentComplete) return;
    dispatch({ type: 'complete_step', index: position });
    onStepCompleted?.(position);
    dispatch({ type: 'next' });
  }, [advancing, current?.id, position, currentComplete, onStepCompleted]);

  const saving = advancing !== null && !advancing.saved;
  const idle = state.pending === null && !state.submitted && !saving;
  const saveFirst = idle && !canAdvance(state) && current?.onNext !== undefined;
  const canNext = (idle && canAdvance(state)) || saveFirst;

  const next = () => {
    if (saveFirst) {
      const { id } = current;
      setOutcome(null);
      setAdvancing({ id, saved: false });
      current.onNext().then(
        () => {
          setAdvancing({ id, saved: true });
        },
        () => {
          setAdvancing(null);
        },
      );
      return;
    }
    if (!canNext) return;
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
  const stepTitle = summaryShown ? (
    text('components_structure.wizard.summary')
  ) : (
    <>
      <span data-part="progress">
        {text('components_structure.wizard.step')}{' '}
        <span data-part="progress-current">{String(position + 1)}</span>{' '}
        {text('components_structure.wizard.of')}{' '}
        <span data-part="progress-total">{String(steps.length)}</span>
      </span>
      {`: ${current?.title ?? ''}`}
    </>
  );
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
                className={styles.stepItem}
                aria-current={isCurrent ? 'step' : undefined}
                data-complete={state.completed[index] === true}
              >
                {index > 0 ? (
                  <span className={styles.separator} aria-hidden="true" data-part="step-separator">
                    ›
                  </span>
                ) : null}
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
              disabled={!canNext}
              aria-describedby={canNext ? undefined : `${headingId}-incomplete`}
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
          {!summaryShown && !canNext && (
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
