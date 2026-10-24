/**
 * UI-20 `RefreshIndicator`: «Actualizado hace X» desde `fetchedAt` medido con el `Clock`
 * inyectado, el botón «Actualizar» y la marca de consulta a demanda o periódica (BR-APP-54, 56;
 * NFR-APP-53). El texto relativo se recalcula cada 10 s con un temporizador del mismo reloj.
 */
import { differenceInSeconds } from 'date-fns';
import { useEffect, useState } from 'react';

import type { Clock } from '@/state/clock';
import { text } from '@/texts';

import styles from './RefreshIndicator.module.css';

/** `refresh_policy` (domain-entities §1). */
type RefreshPolicy = 'on_demand' | 'periodic_60s';

export interface RefreshIndicatorProps {
  /** Milisegundos desde la época de la última respuesta; `null` si aún no hubo ninguna. */
  readonly fetchedAt: number | null;
  readonly refreshPolicy: RefreshPolicy;
  /** Hay una consulta en curso: el botón queda deshabilitado. */
  readonly isFetching?: boolean;
  readonly onRefresh: () => void;
  readonly clock: Clock;
}

const TICK_MS = 10_000;

const relative = new Intl.RelativeTimeFormat('es-CO', { numeric: 'auto' });

/** «hace 5 minutos» para `elapsedSeconds ≥ 0`; un `fetchedAt` futuro cuenta como «ahora». */
function elapsedText(elapsedSeconds: number): string {
  const seconds = Math.max(0, elapsedSeconds);
  if (seconds < 60) return relative.format(-seconds, 'second');
  if (seconds < 3600) return relative.format(-Math.floor(seconds / 60), 'minute');
  if (seconds < 86_400) return relative.format(-Math.floor(seconds / 3600), 'hour');
  return relative.format(-Math.floor(seconds / 86_400), 'day');
}

export function RefreshIndicator({
  fetchedAt,
  refreshPolicy,
  isFetching = false,
  onRefresh,
  clock,
}: RefreshIndicatorProps) {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const handle = clock.setTimeout(() => {
      setTick((value) => value + 1);
    }, TICK_MS);
    return () => {
      clock.clearTimeout(handle);
    };
  }, [clock, tick]);

  const updated =
    fetchedAt === null
      ? text('components_status.refresh.never')
      : `${text('components_status.refresh.updated')} ${elapsedText(
          differenceInSeconds(clock.now(), fetchedAt),
        )}`;
  return (
    <div className={styles.indicator} data-policy={refreshPolicy}>
      <span data-part="updated">{updated}</span>
      <span className={styles.policy} data-part="policy">
        {text(`components_status.refresh.${refreshPolicy}`)}
      </span>
      <button type="button" className={styles.button} onClick={onRefresh} disabled={isFetching}>
        {text('components_status.refresh.button')}
      </button>
    </div>
  );
}
