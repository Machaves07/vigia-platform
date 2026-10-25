/**
 * Horas de la sesión, **por zona** (H-53): el total y el desglose por tipo de paso que da el
 * servidor (`steps_summary`, `StepKindDurationOut`), sin responsable en ninguna parte. La consola
 * no suma duraciones ni agrupa por persona: el servidor no expone esa cifra y la pantalla no la
 * construye.
 */
import { useId } from 'react';

import { formatDurationMs } from '@/pages/scr06/common/format';
import type { WalkTestSession } from '@/ports/scr06Commissioning';
import { label } from '@/texts/labels';
import { text } from '@/texts';

import styles from './session.module.css';

/** Lo que el panel pinta: la sesión en curso o el acta cerrada traen los mismos dos campos. */
type ZoneHours = Pick<WalkTestSession, 'steps_summary' | 'total_duration_ms'>;

export function HoursPanel({ session }: { readonly session: ZoneHours }) {
  const headingId = useId();
  return (
    <section className={styles.hours} aria-labelledby={headingId}>
      <h3 id={headingId} className={styles.subtitle}>
        {text('scr06.hours.title')}
      </h3>
      {session.steps_summary.length === 0 ? (
        <p className={styles.empty} data-part="hours_empty">
          {text('scr06.hours.none')}
        </p>
      ) : (
        <>
          <h4 className={styles.subtitle}>{text('scr06.hours.by_step_kind')}</h4>
          <ul className={styles.hoursList} data-part="hours_by_step_kind">
            {session.steps_summary.map((entry, index) => (
              <li key={`${entry.step_kind}-${String(index)}`}>
                <span>{label('step_kind', entry.step_kind)}</span>{' '}
                <span className={styles.duration} data-part="step_kind_duration">
                  {formatDurationMs(entry.duration_ms)}
                </span>
              </li>
            ))}
          </ul>
          <p className={styles.total} data-part="total_duration">
            {`${text('scr06.hours.total')}: ${formatDurationMs(session.total_duration_ms)}`}
          </p>
        </>
      )}
    </section>
  );
}
