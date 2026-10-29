/**
 * Paso a paso del ciclo del hallazgo (sistema visual v1, «Detalle del hallazgo»): recibido →
 * confirmado → acción → evidencia y cierre → cerrado, con el paso actual marcado
 * (`aria-current="step"`). La posición sale solo del estado que da el servidor: un descartado no
 * tiene ciclo de cierre y no se dibuja; un incerrable o un suspendido se quedan en su paso y lo
 * dicen con su chip, nunca como avance.
 */
import { text } from '@/texts';
import styles from './LifecycleSteps.module.css';

const LIFECYCLE_STEPS = ['received', 'confirmed', 'action', 'closure', 'closed'] as const;

/** Índice del paso actual de cada estado; `null` sin ciclo (descartado o fuera de la lista). */
const POSITION: Readonly<Record<string, number>> = {
  received: 0,
  in_review: 0,
  open: 1,
  suspended_by_reclassification: 1,
  action_assigned: 2,
  overdue: 2,
  unclosable: 2,
  closed: 4,
};

function stepPosition(state: string): number | null {
  return Object.hasOwn(POSITION, state) ? (POSITION[state] ?? null) : null;
}

export function LifecycleSteps({ state }: { readonly state: string }) {
  const position = stepPosition(state);
  if (position === null) return null;
  return (
    <ol className={styles.steps} aria-label={text('lifecycle_steps.label')}>
      {LIFECYCLE_STEPS.map((step, index) => (
        <li
          key={step}
          className={styles.step}
          data-step={step}
          data-done={index < position || (index === position && step === 'closed')}
          {...(index === position ? { 'aria-current': 'step' as const } : {})}
        >
          <span className={styles.marker} aria-hidden="true">
            {index + 1}
          </span>
          {text(`lifecycle_steps.${step}`)}
        </li>
      ))}
    </ol>
  );
}
