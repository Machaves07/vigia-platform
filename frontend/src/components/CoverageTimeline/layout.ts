/**
 * Disposición de UI-11 (BR-APP-37, PAT-APP-REN-03, PR-APP-27): qué se dibuja de cada intervalo
 * en una línea de `widthPx` píxeles que representa el periodo.
 *
 * - Un intervalo de 1 px o más es una barra propia.
 * - Los de menos de un píxel no se omiten: los consecutivos de la misma capa que caen en la misma
 *   columna de píxel forman una **marca contada** que conserva cada intervalo, con su duración,
 *   para el detalle. Una marca de un solo intervalo también es marca.
 *
 * La agrupación es solo visual: el número de intervalos y la suma de sus duraciones de una capa
 * son los mismos antes y después, y ningún intervalo se fusiona con otro.
 */
import type { CoverageLayer, CoveragePeriod, TimedInterval } from '@/state/coverage/compose';

interface TimelineBar {
  readonly kind: 'bar';
  readonly layer: CoverageLayer;
  /** Posición y anchura en píxeles (anchura ≥ 1). */
  readonly x: number;
  readonly width: number;
  readonly intervals: readonly [TimedInterval];
}

interface TimelineMark {
  readonly kind: 'mark';
  readonly layer: CoverageLayer;
  /** Columna de píxel de la marca. */
  readonly x: number;
  /** Cada intervalo agrupado, en orden; nunca vacío. */
  readonly intervals: readonly TimedInterval[];
}

export type TimelineItem = TimelineBar | TimelineMark;

/** Barras y marcas de los intervalos de una capa, en orden de tiempo. */
export function layoutLane(
  intervals: readonly TimedInterval[],
  period: CoveragePeriod,
  widthPx: number,
): TimelineItem[] {
  const span = period.toMs - period.fromMs;
  if (!(span > 0) || !(widthPx > 0)) {
    throw new RangeError('El periodo y la anchura de la línea deben ser positivos.');
  }
  const scale = widthPx / span;
  const items: TimelineItem[] = [];
  for (const interval of intervals) {
    const x = (interval.startMs - period.fromMs) * scale;
    const width = (interval.endMs - interval.startMs) * scale;
    if (width >= 1) {
      items.push({ kind: 'bar', layer: interval.layer, x, width, intervals: [interval] });
      continue;
    }
    const column = Math.floor(x);
    const last = items.at(-1);
    if (last?.kind === 'mark' && last.layer === interval.layer && last.x === column) {
      (last.intervals as TimedInterval[]).push(interval);
    } else {
      items.push({ kind: 'mark', layer: interval.layer, x: column, intervals: [interval] });
    }
  }
  return items;
}
