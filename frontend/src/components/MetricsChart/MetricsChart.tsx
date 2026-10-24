/**
 * UI-23 `MetricsChart`: visualizaciones de la lista cerrada (BR-APP-28, 80; PR-APP-09) con
 * `recharts`.
 *
 * - `bars_by_zone_and_period`: una gráfica de barras pequeña por zona (eje x el periodo) con la
 *   misma escala en todas; un solo tono, así la identidad la da el título de cada zona y no un
 *   color.
 * - `hour_of_day_distribution`: barras de 00 a 23, sin nombre de turno.
 * - `table`: tabla con las dimensiones de la serie y el valor.
 *
 * La serie se valida al renderizar: una visualización fuera de la lista o una dimensión de
 * usuario, equipo o turno lanza `MetricsContractError` (error de contrato), que recoge el
 * manejador de errores de la pantalla. Sin mapas de calor ni color por valor.
 */
import { Bar, BarChart, CartesianGrid, Tooltip, XAxis, YAxis } from 'recharts';

import { text } from '@/texts';

import { parseMetricsSeries, type Dimension, type MetricsPoint } from './metrics';
import styles from './MetricsChart.module.css';

export interface MetricsChartProps {
  readonly title: string;
  /** Serie tal como llega del puerto; se valida contra la lista cerrada. */
  readonly series: unknown;
  /** Nombre de la métrica, ya resuelto desde el catálogo por la pantalla. */
  readonly valueLabel: string;
}

/** Paso 450 de la rampa azul (`dataviz`); contraste con la superficie ≥ 3:1. */
const BAR_FILL = '#2a78d6';
const SMALL = { width: 320, height: 180 };
const WIDE = { width: 720, height: 240 };
const NUMBER = new Intl.NumberFormat('es-CO');
const MARGIN = { top: 8, right: 8, bottom: 8, left: 8 };

function hourLabel(hour: number): string {
  return String(hour).padStart(2, '0');
}

function dimensionHeader(dimension: Dimension): string {
  return text(`components_structure.metrics.${dimension}`);
}

function cellOf(point: MetricsPoint, dimension: Dimension): string {
  if (dimension === 'hour_of_day') return hourLabel(point.hour_of_day ?? 0);
  return point[dimension] ?? '';
}

function maxValue(points: readonly MetricsPoint[]): number {
  return points.reduce((max, point) => Math.max(max, point.value), 0);
}

function minValue(points: readonly MetricsPoint[]): number {
  return points.reduce((min, point) => Math.min(min, point.value), 0);
}

/** Barras de un solo tono; `recharts` da a la superficie su nombre (`title`) y el teclado. */
function Bars({
  name,
  data,
  valueLabel,
  size,
  domain,
}: {
  name: string;
  data: readonly { readonly x: string; readonly value: number }[];
  valueLabel: string;
  size: { width: number; height: number };
  domain: readonly [number, number];
}) {
  return (
    <BarChart
      width={size.width}
      height={size.height}
      data={[...data]}
      margin={MARGIN}
      title={name}
      accessibilityLayer
    >
      <CartesianGrid vertical={false} strokeDasharray="2 4" />
      <XAxis dataKey="x" tickLine={false} />
      <YAxis
        domain={[domain[0], Math.max(domain[1], domain[0] + 1)]}
        allowDecimals
        tickLine={false}
        width={40}
      />
      <Tooltip />
      <Bar dataKey="value" name={valueLabel} fill={BAR_FILL} radius={[4, 4, 0, 0]} />
    </BarChart>
  );
}

export function MetricsChart({ title, series, valueLabel }: MetricsChartProps) {
  const parsed = parseMetricsSeries(series);
  const { visualization, points, dimensions } = parsed;

  let body;
  if (points.length === 0) {
    body = <p data-part="empty">{text('components_structure.metrics.empty')}</p>;
  } else if (visualization === 'table') {
    body = (
      <table className={styles.table}>
        <thead>
          <tr>
            {dimensions.map((dimension) => (
              <th key={dimension} scope="col">
                {dimensionHeader(dimension)}
              </th>
            ))}
            <th scope="col">{valueLabel}</th>
          </tr>
        </thead>
        <tbody>
          {points.map((point, index) => (
            // Las filas no tienen identidad propia: el orden es el del servidor.
            <tr key={index}>
              {dimensions.map((dimension) => (
                <td key={dimension}>{cellOf(point, dimension)}</td>
              ))}
              <td>{NUMBER.format(point.value)}</td>
            </tr>
          ))}
        </tbody>
      </table>
    );
  } else if (visualization === 'hour_of_day_distribution') {
    const data = [...points]
      .sort((a, b) => (a.hour_of_day ?? 0) - (b.hour_of_day ?? 0))
      .map((point) => ({ x: hourLabel(point.hour_of_day ?? 0), value: point.value }));
    body = (
      <Bars
        name={`${title}: ${dimensionHeader('hour_of_day')}`}
        data={data}
        valueLabel={valueLabel}
        size={WIDE}
        domain={[minValue(points), maxValue(points)]}
      />
    );
  } else {
    const zones = [...new Set(points.map((point) => point.zone ?? ''))];
    const domain = [minValue(points), maxValue(points)] as const;
    body = (
      <div className={styles.multiples} data-visualization={visualization}>
        {zones.map((zone) => (
          <figure key={zone} className={styles.multiple}>
            <figcaption>
              {dimensionHeader('zone')}: {zone}
            </figcaption>
            <Bars
              name={`${title}: ${zone}`}
              data={points
                .filter((point) => point.zone === zone)
                .map((point) => ({ x: point.period ?? '', value: point.value }))}
              valueLabel={valueLabel}
              size={SMALL}
              domain={domain}
            />
          </figure>
        ))}
      </div>
    );
  }

  return (
    <figure className={styles.chart} data-visualization={visualization}>
      <figcaption className={styles.title}>{title}</figcaption>
      {body}
    </figure>
  );
}
