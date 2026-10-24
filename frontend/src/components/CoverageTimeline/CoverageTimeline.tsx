/**
 * UI-11 `CoverageTimeline`: la respuesta honesta a «¿se podía ver la zona?» (BR-APP-37, 39, 41;
 * PAT-APP-REN-03; PR-APP-05, 27).
 *
 * - Dos capas en SVG propio: «lo que el nodo informa» (`node_report`) y las marcas de «lo que la
 *   plataforma supo» (`platform_communication`), cada una con su base de reloj (`clock_basis`).
 * - **Todos** los intervalos se dibujan: los de 1 px o más como barra; los de menos, agrupados en
 *   marcas contadas que listan cada intervalo con su duración en el detalle (`layout.ts`).
 * - Resumen en minutos y porcentaje del periodo solo si el compuesto está completo; si no,
 *   «cobertura incompleta» y ningún porcentaje (BR-APP-39).
 * - Textos «no se observó», nunca «no ocurrió»; leyenda fija; selector de periodo (7 días por
 *   omisión, tramos de 31, tope de 12 meses `[objetivo propio]`).
 *
 * Teclado: barras y marcas forman un solo punto de tabulación; las flechas, Inicio y Fin recorren
 * los elementos en orden de tiempo y el detalle muestra el enfocado, igual que al pasar el cursor.
 *
 * Marca de rendimiento `coverage_timeline.render` (NFR-APP-03, la mide VIG-330: 31 días en
 * ≤ 300 ms) desde que empieza la disposición hasta que el SVG está en el DOM, con el `Clock`.
 */
import { useId, useLayoutEffect, useMemo, useRef, useState } from 'react';
import type { ChangeEvent, KeyboardEvent } from 'react';

import { TimestampWithOffset } from '@/components/TimestampWithOffset/TimestampWithOffset';
import type { Clock } from '@/state/clock';
import {
  LAYERS,
  periodEndingAt,
  SUMMARY_KEYS,
  type CoverageComposite,
  type CoverageLayer,
  type CoveragePeriod,
} from '@/state/coverage/compose';
import { text } from '@/texts';

import styles from './CoverageTimeline.module.css';
import { clockBasisLabel, formatDuration, intervalLabel } from './format';
import { layoutLane, type TimelineItem } from './layout';

export interface CoverageTimelineProps {
  readonly composite: CoverageComposite;
  /** Zona horaria IANA de la planta, para las fechas del detalle. */
  readonly timeZone: string;
  readonly clock: Clock;
  /** `periodChanged`: la pantalla pide y compone el nuevo periodo. */
  readonly onPeriodChange: (period: CoveragePeriod) => void;
  /** Anchura de la línea en píxeles; decide qué intervalos son de menos de un píxel. */
  readonly widthPx?: number;
}

const DAY_MS = 24 * 60 * 60 * 1000;
const PRESETS = [
  ['last_7_days', 7],
  ['last_31_days', 31],
  ['last_3_months', 92],
  ['last_12_months', 365],
] as const;
const LANE_HEIGHT = 24;
const OVERHANG = 4;
const MARK_WIDTH = 2;
const DEFAULT_LAYER_BASIS: Readonly<Record<CoverageLayer, string>> = {
  node_report: 'node',
  platform_communication: 'platform',
};
const NUMBER = new Intl.NumberFormat('es-CO', { maximumFractionDigits: 1 });

function markText(count: number): string {
  return `${String(count)} ${text(count === 1 ? 'coverage.mark.one' : 'coverage.mark.many')}`;
}

function itemLabel(item: TimelineItem): string {
  const layer = text(`coverage.layer.${item.layer}`);
  if (item.kind === 'mark') return `${layer}: ${markText(item.intervals.length)}`;
  const [only] = item.intervals;
  return `${layer}: ${intervalLabel(only.interval)}, ${formatDuration(only.interval.duration_ms)}`;
}

function laneBasis(composite: CoverageComposite, layer: CoverageLayer): string {
  const bases = new Set(
    composite.intervals.filter((i) => i.layer === layer).map((i) => i.interval.clock_basis),
  );
  if (bases.size === 0) bases.add(DEFAULT_LAYER_BASIS[layer]);
  return [...bases].map(clockBasisLabel).join(', ');
}

function presetOf(period: CoveragePeriod): string {
  const days = (period.toMs - period.fromMs) / DAY_MS;
  return PRESETS.find(([, presetDays]) => presetDays === days)?.[0] ?? 'custom';
}

function measure(clock: Clock, start: number, intervals: number): void {
  if (typeof performance === 'undefined' || typeof performance.measure !== 'function') return;
  performance.measure('coverage_timeline.render', {
    start,
    end: clock.now(),
    detail: { intervals },
  });
}

function Summary({ composite }: { composite: CoverageComposite }) {
  if (composite.status === 'incomplete') {
    return (
      <div className={styles.incomplete} role="status" data-part="incomplete">
        <strong>{text('coverage.incomplete.title')}</strong>
        <p>{text('coverage.incomplete.body')}</p>
        <ul>
          {composite.problems.map((problem, index) => (
            // Los problemas no tienen identidad propia: el orden es el de los tramos.
            <li key={index} data-part="problem" data-kind={problem.kind}>
              {text(`coverage.incomplete.${problem.kind}`)}
            </li>
          ))}
        </ul>
      </div>
    );
  }
  const span = composite.period.toMs - composite.period.fromMs;
  return (
    <table className={styles.summary} data-part="summary">
      <caption>{text('coverage.summary.title')}</caption>
      <thead>
        <tr>
          <th scope="col">{text('coverage.summary.counter')}</th>
          <th scope="col">{text('coverage.summary.minutes')}</th>
          <th scope="col">{text('coverage.summary.percent')}</th>
        </tr>
      </thead>
      <tbody>
        {SUMMARY_KEYS.map((key) => (
          <tr key={key} data-counter={key}>
            <th scope="row">{text(`coverage.summary.${key}`)}</th>
            <td data-part="minutes">{NUMBER.format(composite.summary[key] / 60_000)}</td>
            <td data-part="percent">{`${NUMBER.format((composite.summary[key] * 100) / span)} %`}</td>
          </tr>
        ))}
      </tbody>
    </table>
  );
}

function Detail({
  item,
  timeZone,
  id,
}: {
  item: TimelineItem | null;
  timeZone: string;
  id: string;
}) {
  return (
    <div className={styles.detail} id={id} aria-live="polite" data-part="detail">
      <h3>{text('coverage.detail.title')}</h3>
      {item === null ? (
        <p className={styles.note}>{text('coverage.detail.hint')}</p>
      ) : (
        <>
          <p data-part="detail-heading">
            {item.kind === 'mark'
              ? `${text(`coverage.layer.${item.layer}`)}: ${markText(item.intervals.length)}`
              : text(`coverage.layer.${item.layer}`)}
          </p>
          <ul>
            {item.intervals.map(({ startMs, interval }) => (
              <li key={startMs} data-part="detail-interval">
                <span data-part="interval-label">{intervalLabel(interval)}</span>.{' '}
                {text('coverage.detail.from')}{' '}
                <TimestampWithOffset instant={interval.starts_at} timeZone={timeZone} />{' '}
                {text('coverage.detail.to')}{' '}
                <TimestampWithOffset instant={interval.ends_at} timeZone={timeZone} />.{' '}
                {text('coverage.detail.duration')}:{' '}
                <span data-part="duration">{formatDuration(interval.duration_ms)}</span>.{' '}
                {text('coverage.clock_basis.label')}: {clockBasisLabel(interval.clock_basis)}.
              </li>
            ))}
          </ul>
        </>
      )}
    </div>
  );
}

function Legend() {
  return (
    <div>
      <h3 className={styles.note}>{text('coverage.legend.title')}</h3>
      <ul className={styles.legend} data-part="legend">
        {(['observable', 'degraded', 'not_observable'] as const).map((state) => (
          <li key={state}>
            <span className={styles.swatch} data-state={state} aria-hidden="true" />
            {text(`coverage.state.${state}`)}
          </li>
        ))}
        <li>
          <span className={styles.swatch} data-state="mark" aria-hidden="true" />
          {text('coverage.legend.mark')}
        </li>
        <li>{text('coverage.legend.not_observed')}</li>
        <li>{text('components_status.observability.legend')}</li>
      </ul>
    </div>
  );
}

export function CoverageTimeline({
  composite,
  timeZone,
  clock,
  onPeriodChange,
  widthPx = 720,
}: CoverageTimelineProps) {
  const titleId = useId();
  const selectId = useId();
  const detailId = useId();
  const laneIds = useId();
  const [activeIndex, setActiveIndex] = useState<number | null>(null);
  const refs = useRef<(SVGRectElement | null)[]>([]);

  const layout = useMemo(() => {
    const startedAt = clock.now();
    const lanes = LAYERS.map((layer) => ({
      layer,
      items: layoutLane(
        composite.intervals.filter((interval) => interval.layer === layer),
        composite.period,
        widthPx,
      ),
    }));
    const ordered = lanes
      .flatMap(({ items }) => items)
      .sort((a, b) => a.intervals[0].startMs - b.intervals[0].startMs);
    const indexOf = new Map(ordered.map((item, index) => [item, index]));
    return { startedAt, lanes, ordered, indexOf };
  }, [composite, widthPx, clock]);

  useLayoutEffect(() => {
    measure(clock, layout.startedAt, composite.intervals.length);
  }, [layout, clock, composite]);

  const { ordered } = layout;
  const active = activeIndex !== null && activeIndex < ordered.length ? activeIndex : null;
  const tabStop = active ?? 0;
  const scale = widthPx / (composite.period.toMs - composite.period.fromMs);

  const moveTo = (index: number) => {
    const target = Math.min(Math.max(index, 0), ordered.length - 1);
    setActiveIndex(target);
    refs.current[target]?.focus();
  };

  const onKeyDown = (event: KeyboardEvent<SVGRectElement>, index: number) => {
    switch (event.key) {
      case 'ArrowRight':
      case 'ArrowDown':
        moveTo(index + 1);
        break;
      case 'ArrowLeft':
      case 'ArrowUp':
        moveTo(index - 1);
        break;
      case 'Home':
        moveTo(0);
        break;
      case 'End':
        moveTo(ordered.length - 1);
        break;
      default:
        return;
    }
    event.preventDefault();
  };

  const onPresetChange = (event: ChangeEvent<HTMLSelectElement>) => {
    const preset = PRESETS.find(([key]) => key === event.target.value);
    if (preset === undefined) return;
    setActiveIndex(null);
    onPeriodChange(periodEndingAt(composite.period.toMs, preset[1] * DAY_MS));
  };

  const missing =
    composite.status === 'incomplete'
      ? composite.problems.filter((problem) => problem.kind !== 'summary_mismatch')
      : [];

  return (
    <section className={styles.timeline} aria-labelledby={titleId} data-status={composite.status}>
      <header className={styles.header}>
        <h2 id={titleId} className={styles.title}>
          {text('coverage.timeline.title')}
        </h2>
        <div className={styles.selector}>
          <label htmlFor={selectId}>{text('coverage.period.label')}</label>
          <select id={selectId} value={presetOf(composite.period)} onChange={onPresetChange}>
            {PRESETS.map(([key]) => (
              <option key={key} value={key}>
                {text(`coverage.period.${key}`)}
              </option>
            ))}
            <option value="custom" disabled>
              {text('coverage.period.custom')}
            </option>
          </select>
          <p className={styles.note}>{text('coverage.period.limit')}</p>
        </div>
      </header>

      <Summary composite={composite} />

      <div className={styles.lanes}>
        {layout.lanes.map(({ layer, items }) => {
          const labelId = `${laneIds}-${layer}`;
          return (
            <div className={styles.lane} key={layer} data-layer={layer}>
              <span id={labelId} className={styles.laneLabel}>
                {text(`coverage.layer.${layer}`)} ({text('coverage.clock_basis.label')}:{' '}
                <span data-part="clock-basis">{laneBasis(composite, layer)}</span>)
              </span>
              <svg
                className={styles.svg}
                width={widthPx}
                height={LANE_HEIGHT + 2 * OVERHANG}
                viewBox={`0 0 ${String(widthPx)} ${String(LANE_HEIGHT + 2 * OVERHANG)}`}
                role="group"
                aria-labelledby={labelId}
              >
                <rect
                  className={styles.track}
                  x={0}
                  y={OVERHANG}
                  width={widthPx}
                  height={LANE_HEIGHT}
                  aria-hidden="true"
                />
                {missing.map((problem, index) => (
                  <rect
                    key={index}
                    className={styles.missing}
                    data-part="missing"
                    x={(problem.period.fromMs - composite.period.fromMs) * scale}
                    y={OVERHANG}
                    width={Math.max((problem.period.toMs - problem.period.fromMs) * scale, 1)}
                    height={LANE_HEIGHT}
                    aria-hidden="true"
                  />
                ))}
                <g role="list" aria-label={text('coverage.timeline.lane_items')}>
                  {items.map((item) => {
                    const index = layout.indexOf.get(item) ?? 0;
                    const first = item.intervals[0];
                    const isMark = item.kind === 'mark';
                    return (
                      <rect
                        key={first.startMs}
                        ref={(element) => {
                          refs.current[index] = element;
                        }}
                        className={styles.item}
                        role="listitem"
                        tabIndex={index === tabStop ? 0 : -1}
                        aria-label={itemLabel(item)}
                        aria-describedby={detailId}
                        data-kind={item.kind}
                        data-count={item.intervals.length}
                        data-state={isMark ? undefined : first.interval.state}
                        data-active={index === active ? 'true' : undefined}
                        x={isMark ? Math.min(item.x, widthPx - MARK_WIDTH) : item.x}
                        y={isMark ? 0 : OVERHANG}
                        width={isMark ? MARK_WIDTH : item.width}
                        height={isMark ? LANE_HEIGHT + 2 * OVERHANG : LANE_HEIGHT}
                        onFocus={() => {
                          setActiveIndex(index);
                        }}
                        onMouseEnter={() => {
                          setActiveIndex(index);
                        }}
                        onKeyDown={(event) => {
                          onKeyDown(event, index);
                        }}
                      />
                    );
                  })}
                </g>
              </svg>
            </div>
          );
        })}
      </div>

      <Detail
        item={active === null ? null : (ordered[active] ?? null)}
        timeZone={timeZone}
        id={detailId}
      />
      <Legend />
    </section>
  );
}
