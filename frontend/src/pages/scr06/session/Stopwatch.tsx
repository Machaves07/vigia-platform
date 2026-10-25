/**
 * Cronómetro de presentación de un paso abierto (IF-13 paso 2, BR-APP-45): el valor sale del
 * `started_at` del servidor y del `Clock` inyectado; `data-elapsed-ms` deja el valor exacto para
 * las pruebas, sin depender del texto formateado.
 */
import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import { formatElapsed, useElapsedMs } from './stopwatch';
import styles from './session.module.css';

export function Stopwatch({
  startedAt,
  clock,
}: {
  readonly startedAt: string;
  readonly clock: Clock;
}) {
  const elapsed = useElapsedMs(startedAt, clock);
  const formatted = formatElapsed(elapsed);
  return (
    <span
      className={styles.stopwatch}
      data-part="stopwatch"
      data-elapsed-ms={String(elapsed)}
      role="timer"
      aria-label={`${text('scr06.steps.elapsed')}: ${formatted}`}
    >
      {formatted}
    </span>
  );
}
