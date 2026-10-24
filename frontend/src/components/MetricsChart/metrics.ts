/**
 * Lista cerrada de visualizaciones de UI-23 (BR-APP-28, 80; P7; PR-APP-09).
 *
 * Solo existen barras por zona y periodo, distribución por hora del día (sin nombre de turno) y
 * tabla; la línea de tiempo de cobertura es UI-11. Las dimensiones admitidas son también una
 * lista cerrada (`zone`, `period`, `hour_of_day`): cualquier otra se rechaza con un error de
 * contrato, y las que nombran a una persona, un equipo o un turno con `forbidden_dimension`. No
 * hay mapas de calor ni escalas de color por valor.
 */

export const VISUALIZATIONS = [
  'bars_by_zone_and_period',
  'hour_of_day_distribution',
  'table',
] as const;
type Visualization = (typeof VISUALIZATIONS)[number];

export const DIMENSIONS = ['zone', 'period', 'hour_of_day'] as const;
export type Dimension = (typeof DIMENSIONS)[number];

/** Dimensiones de cada visualización: exactas en las gráficas, subconjunto no vacío en la tabla. */
const DIMENSIONS_OF: Readonly<Record<Visualization, readonly Dimension[]>> = {
  bars_by_zone_and_period: ['zone', 'period'],
  hour_of_day_distribution: ['hour_of_day'],
  table: DIMENSIONS,
};

/** Nombres que atribuyen a persona, equipo o turno (también en español y con separadores). */
const FORBIDDEN_NAME =
  /(user|usuario|person|persona|people|operator|operador|operario|worker|trabajador|employee|empleado|staff|team|equipo|crew|cuadrilla|squad|group|grupo|shift|turno|jornada)/i;

const MAX_LABEL = 200;

export type MetricsContractCode =
  | 'invalid_series'
  | 'unknown_visualization'
  | 'forbidden_dimension'
  | 'dimension_not_allowed'
  | 'invalid_point';

export class MetricsContractError extends Error {
  override readonly name = 'MetricsContractError';
  constructor(
    readonly code: MetricsContractCode,
    message: string,
  ) {
    super(message);
  }
}

export interface MetricsPoint {
  readonly zone?: string;
  readonly period?: string;
  readonly hour_of_day?: number;
  readonly value: number;
}

export interface MetricsSeries {
  readonly visualization: Visualization;
  readonly dimensions: readonly Dimension[];
  readonly points: readonly MetricsPoint[];
}

function isRecord(value: unknown): value is Readonly<Record<string, unknown>> {
  return typeof value === 'object' && value !== null && !Array.isArray(value);
}

/** Quita marcas invisibles y compara sin mayúsculas, para que `S​hift` no escape. */
function normalizedName(name: string): string {
  return name.normalize('NFKC').replace(/[^\p{L}\p{N}]/gu, '');
}

function rejectName(name: string, allowed: readonly string[]): void {
  if (FORBIDDEN_NAME.test(normalizedName(name))) {
    throw new MetricsContractError('forbidden_dimension', `Dimensión prohibida: «${name}».`);
  }
  if (!allowed.includes(name)) {
    throw new MetricsContractError('dimension_not_allowed', `Dimensión no admitida: «${name}».`);
  }
}

function isVisualization(value: unknown): value is Visualization {
  return typeof value === 'string' && (VISUALIZATIONS as readonly string[]).includes(value);
}

function label(value: unknown, field: string): string {
  if (typeof value !== 'string' || value.trim() === '' || value.length > MAX_LABEL) {
    throw new MetricsContractError('invalid_point', `«${field}» debe ser un texto no vacío.`);
  }
  return value;
}

function point(raw: unknown, dimensions: readonly Dimension[]): MetricsPoint {
  if (!isRecord(raw)) throw new MetricsContractError('invalid_point', 'Punto no es un objeto.');
  for (const key of Object.keys(raw)) {
    if (key !== 'value') rejectName(key, dimensions);
  }
  const value = raw.value;
  if (typeof value !== 'number' || !Number.isFinite(value)) {
    throw new MetricsContractError('invalid_point', '«value» debe ser un número finito.');
  }
  const result: { zone?: string; period?: string; hour_of_day?: number; value: number } = {
    value,
  };
  for (const dimension of dimensions) {
    if (dimension === 'hour_of_day') {
      const hour = raw.hour_of_day;
      if (typeof hour !== 'number' || !Number.isInteger(hour) || hour < 0 || hour > 23) {
        throw new MetricsContractError('invalid_point', '«hour_of_day» debe ser 0 a 23.');
      }
      result.hour_of_day = hour;
    } else {
      result[dimension] = label(raw[dimension], dimension);
    }
  }
  return result;
}

/** Valida una serie contra la lista cerrada; lanza `MetricsContractError` si no pertenece. */
export function parseMetricsSeries(raw: unknown): MetricsSeries {
  if (!isRecord(raw)) throw new MetricsContractError('invalid_series', 'La serie no es un objeto.');
  for (const key of Object.keys(raw)) {
    if (key !== 'visualization' && key !== 'dimensions' && key !== 'points') {
      rejectName(key, []);
    }
  }
  const { visualization, dimensions, points } = raw;
  if (!isVisualization(visualization)) {
    throw new MetricsContractError('unknown_visualization', 'Visualización fuera de la lista.');
  }
  if (!Array.isArray(dimensions) || !dimensions.every((d) => typeof d === 'string')) {
    throw new MetricsContractError('invalid_series', '«dimensions» debe ser una lista de textos.');
  }
  const allowed = DIMENSIONS_OF[visualization];
  for (const dimension of dimensions) rejectName(dimension, allowed);
  const unique = new Set(dimensions);
  const exact = visualization !== 'table';
  if (
    unique.size !== dimensions.length ||
    unique.size === 0 ||
    (exact && unique.size !== allowed.length)
  ) {
    throw new MetricsContractError(
      'dimension_not_allowed',
      `Dimensiones no válidas para «${visualization}».`,
    );
  }
  if (!Array.isArray(points)) {
    throw new MetricsContractError('invalid_series', '«points» debe ser una lista.');
  }
  const ordered = DIMENSIONS.filter((dimension) => unique.has(dimension));
  return {
    visualization,
    dimensions: ordered,
    points: points.map((item) => point(item, ordered)),
  };
}
