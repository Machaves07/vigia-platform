/**
 * Composición pura de `CoverageComposite` (domain-entities §2.16; BR-APP-37, 39; PAT-APP-REN-03;
 * PR-APP-05).
 *
 * `GET /zones/{zone_id}/coverage?from&to` acepta como mucho 31 días, así que un periodo (7 días
 * por omisión, tope de 12 meses `[objetivo propio]`) se pide en tramos contiguos de ≤ 31 días
 * (`partitionPeriod`) y se concatena aquí (`composeCoverage`):
 *
 * - Cada tramo se comprueba antes de usarlo: su `period` es el pedido, sus intervalos son
 *   contiguos, empiezan y acaban en sus extremos, cada `duration_ms` es `ends_at − starts_at`, los
 *   valores pertenecen a las listas cerradas y su resumen reparte cada milisegundo en el contador
 *   de su intervalo.
 * - Los intervalos se concatenan tal como llegan: **ninguno se omite ni se fusiona**, tampoco dos
 *   del mismo estado a ambos lados del corte entre tramos (BR-APP-37).
 * - El resumen es la suma de los seis contadores de los tramos y solo se da si suma exactamente
 *   `to − from`. Si falta un tramo, uno no es coherente o la suma no cuadra, el compuesto queda
 *   `incomplete` con la lista de problemas y **sin resumen**: la interfaz muestra «cobertura
 *   incompleta», nunca un resumen parcial (BR-APP-39).
 *
 * `loadCoverage` pide los tramos con el puerto de la pantalla; si el servidor responde
 * `period_too_long`, parte ese tramo en dos y los pide de nuevo (la interfaz corrige el tramo).
 * Marca de rendimiento `coverage.compose` alrededor de la composición (NFR-APP-03; la mide
 * VIG-330: 12 tramos en ≤ 100 ms).
 *
 * Todo en milisegundos enteros desde la época (UTC); ningún cálculo lee la hora del sistema.
 */
import { isPortError } from '@/ports/base';
import type { Clock } from '@/state/clock';

const DAY_MS = 24 * 60 * 60 * 1000;

/** Tope de un tramo: el de `GET /zones/{zone_id}/coverage` (PAT-NUC-REN-04). */
export const CHUNK_MAX_MS = 31 * DAY_MS;
/** Periodo por omisión de la línea de tiempo. */
export const DEFAULT_PERIOD_MS = 7 * DAY_MS;
/** Tope del periodo, 12 meses `[objetivo propio]`: 366 días caben en 12 tramos de 31. */
export const MAX_PERIOD_MS = 366 * DAY_MS;
/** Veces que un tramo se parte en dos tras `period_too_long` antes de darlo por perdido. */
const MAX_SPLITS = 6;

export const LAYERS = ['node_report', 'platform_communication'] as const;
export type CoverageLayer = (typeof LAYERS)[number];

const STATES = ['observable', 'degraded', 'not_observable'] as const;
const PLATFORM_CAUSES = ['no_communication', 'never_reported', 'zone_not_active'] as const;

/** Los seis contadores del resumen, en el orden del diseño. */
export const SUMMARY_KEYS = [
  'observable_ms',
  'degraded_ms',
  'not_observable_ms',
  'no_communication_ms',
  'never_reported_ms',
  'zone_not_active_ms',
] as const;
export type SummaryKey = (typeof SUMMARY_KEYS)[number];
export type CoverageSummary = Readonly<Record<SummaryKey, number>>;

/** `IntervalOut` de U-02 (domain-entities §3.8); la pantalla lo recibe ya validado por su puerto. */
export interface CoverageIntervalOut {
  readonly starts_at: string;
  readonly ends_at: string;
  readonly duration_ms: number;
  readonly layer: string;
  readonly state: string;
  readonly causes: readonly string[];
  readonly clock_basis: string;
  readonly clock_offset_ms?: number | null;
}

/** `CoverageTimelineOut` de U-02: la respuesta de un tramo. */
export interface CoverageChunkResponse {
  readonly period: Readonly<Record<string, string>>;
  readonly intervals: readonly CoverageIntervalOut[];
  readonly summary: CoverageSummary;
}

/** `[fromMs, toMs)` en milisegundos desde la época. */
export interface CoveragePeriod {
  readonly fromMs: number;
  readonly toMs: number;
}

/** Un tramo pedido y lo que devolvió: `response` es `null` si la petición falló. */
export interface CoverageChunkResult {
  readonly period: CoveragePeriod;
  readonly response: CoverageChunkResponse | null;
  /** Error del puerto, para que la pantalla aplique su comportamiento (BR-APP-71). */
  readonly error?: unknown;
}

/** Un intervalo del compuesto, con sus extremos ya en milisegundos. */
export interface TimedInterval {
  readonly startMs: number;
  readonly endMs: number;
  readonly layer: CoverageLayer;
  readonly interval: CoverageIntervalOut;
}

export type CoverageProblemKind =
  /** La petición del tramo falló. */
  | 'missing_chunk'
  /** La respuesta del tramo no es coherente consigo misma o con lo pedido. */
  | 'inconsistent_chunk'
  /** Los tramos no cubren el periodo sin hueco ni solape. */
  | 'not_contiguous'
  /** Los resúmenes no suman exactamente `to − from`. */
  | 'summary_mismatch';

export interface CoverageProblem {
  readonly kind: CoverageProblemKind;
  readonly period: CoveragePeriod;
}

interface CompositeBase {
  readonly period: CoveragePeriod;
  /** Los tramos en que se pidió el periodo, en orden. */
  readonly chunks: readonly CoveragePeriod[];
  /** Todos los intervalos de los tramos coherentes, en orden y sin fusionar. */
  readonly intervals: readonly TimedInterval[];
}

export type CoverageComposite = CompositeBase &
  (
    | { readonly status: 'complete'; readonly summary: CoverageSummary }
    | { readonly status: 'incomplete'; readonly problems: readonly CoverageProblem[] }
  );

export class CoveragePeriodError extends RangeError {
  override readonly name = 'CoveragePeriodError';
}

function checkPeriod(period: CoveragePeriod, maxMs: number): void {
  const { fromMs, toMs } = period;
  if (!Number.isSafeInteger(fromMs) || !Number.isSafeInteger(toMs)) {
    throw new CoveragePeriodError('Los extremos del periodo deben ser milisegundos enteros.');
  }
  if (fromMs >= toMs) {
    throw new CoveragePeriodError('El inicio del periodo debe ser anterior al fin.');
  }
  if (toMs - fromMs > maxMs) {
    throw new CoveragePeriodError('El periodo supera el tope de la línea de tiempo.');
  }
}

/** Periodo de `durationMs` que acaba en `toMs` (selector de periodo). */
export function periodEndingAt(toMs: number, durationMs: number): CoveragePeriod {
  const period = { fromMs: toMs - durationMs, toMs };
  checkPeriod(period, MAX_PERIOD_MS);
  return period;
}

/** Tramos contiguos de ≤ 31 días que cubren `period` exactamente, en orden. */
export function partitionPeriod(period: CoveragePeriod): CoveragePeriod[] {
  checkPeriod(period, MAX_PERIOD_MS);
  const chunks: CoveragePeriod[] = [];
  for (let start = period.fromMs; start < period.toMs; start += CHUNK_MAX_MS) {
    chunks.push({ fromMs: start, toMs: Math.min(start + CHUNK_MAX_MS, period.toMs) });
  }
  return chunks;
}

/** Las dos mitades de un tramo (corrección tras `period_too_long`). */
export function splitChunk(chunk: CoveragePeriod): [CoveragePeriod, CoveragePeriod] {
  checkPeriod(chunk, Number.MAX_SAFE_INTEGER);
  if (chunk.toMs - chunk.fromMs < 2) {
    throw new CoveragePeriodError('Un tramo de 1 ms no se puede partir.');
  }
  const middle = chunk.fromMs + Math.floor((chunk.toMs - chunk.fromMs) / 2);
  return [
    { fromMs: chunk.fromMs, toMs: middle },
    { fromMs: middle, toMs: chunk.toMs },
  ];
}

/** `from` y `to` de la consulta, ISO 8601 UTC con milisegundos y `Z`. */
export function periodQuery(period: CoveragePeriod): { from: string; to: string } {
  return {
    from: new Date(period.fromMs).toISOString(),
    to: new Date(period.toMs).toISOString(),
  };
}

function instantMs(value: unknown): number | null {
  if (typeof value !== 'string') return null;
  const ms = Date.parse(value);
  return Number.isSafeInteger(ms) ? ms : null;
}

function isLayer(value: string): value is CoverageLayer {
  return (LAYERS as readonly string[]).includes(value);
}

/** Contador del resumen en que cuenta un intervalo (el mismo reparto que U-02). */
export function summaryKeyOf(interval: CoverageIntervalOut): SummaryKey | null {
  if (interval.layer === 'platform_communication') {
    const cause = interval.causes[0];
    if (interval.state !== 'not_observable' || interval.causes.length !== 1) return null;
    return (PLATFORM_CAUSES as readonly (string | undefined)[]).includes(cause)
      ? (`${cause ?? ''}_ms` as SummaryKey)
      : null;
  }
  if (interval.layer !== 'node_report') return null;
  return (STATES as readonly string[]).includes(interval.state)
    ? (`${interval.state}_ms` as SummaryKey)
    : null;
}

/** Intervalos del tramo si su respuesta es coherente con él; `null` si no. */
function chunkIntervals(
  chunk: CoveragePeriod,
  response: CoverageChunkResponse,
): TimedInterval[] | null {
  if (
    instantMs(response.period.from) !== chunk.fromMs ||
    instantMs(response.period.to) !== chunk.toMs
  ) {
    return null;
  }
  const counted: Record<SummaryKey, number> = Object.fromEntries(
    SUMMARY_KEYS.map((key) => [key, 0]),
  ) as Record<SummaryKey, number>;
  const timed: TimedInterval[] = [];
  let cursor = chunk.fromMs;
  for (const interval of response.intervals) {
    const startMs = instantMs(interval.starts_at);
    const endMs = instantMs(interval.ends_at);
    const key = summaryKeyOf(interval);
    if (
      startMs !== cursor ||
      endMs === null ||
      endMs <= startMs ||
      interval.duration_ms !== endMs - startMs ||
      key === null ||
      !isLayer(interval.layer)
    ) {
      return null;
    }
    counted[key] += interval.duration_ms;
    timed.push({ startMs, endMs, layer: interval.layer, interval });
    cursor = endMs;
  }
  if (cursor !== chunk.toMs) return null;
  const consistent = SUMMARY_KEYS.every((key) => response.summary[key] === counted[key]);
  return consistent ? timed : null;
}

/**
 * Compone el periodo a partir de sus tramos. Un compuesto `complete` cubre `period` sin hueco ni
 * solape y su resumen suma exactamente `to − from`; cualquier otra cosa es `incomplete`.
 */
export function composeCoverage(
  period: CoveragePeriod,
  results: readonly CoverageChunkResult[],
): CoverageComposite {
  checkPeriod(period, MAX_PERIOD_MS);
  const ordered = [...results].sort((a, b) => a.period.fromMs - b.period.fromMs);
  const problems: CoverageProblem[] = [];
  const intervals: TimedInterval[] = [];
  const summary: Record<SummaryKey, number> = Object.fromEntries(
    SUMMARY_KEYS.map((key) => [key, 0]),
  ) as Record<SummaryKey, number>;

  let cursor = period.fromMs;
  for (const { period: chunk, response } of ordered) {
    if (chunk.fromMs !== cursor || chunk.toMs <= chunk.fromMs || chunk.toMs > period.toMs) {
      problems.push({ kind: 'not_contiguous', period: chunk });
    }
    cursor = Math.max(cursor, chunk.toMs);
    if (response === null) {
      problems.push({ kind: 'missing_chunk', period: chunk });
      continue;
    }
    const timed = chunk.toMs - chunk.fromMs <= CHUNK_MAX_MS ? chunkIntervals(chunk, response) : null;
    if (timed === null) {
      problems.push({ kind: 'inconsistent_chunk', period: chunk });
      continue;
    }
    intervals.push(...timed);
    for (const key of SUMMARY_KEYS) summary[key] += response.summary[key];
  }
  if (cursor !== period.toMs) {
    problems.push({ kind: 'not_contiguous', period: { fromMs: cursor, toMs: period.toMs } });
  }

  const total = SUMMARY_KEYS.reduce((sum, key) => sum + summary[key], 0);
  if (problems.length === 0 && total !== period.toMs - period.fromMs) {
    problems.push({ kind: 'summary_mismatch', period });
  }

  const chunks = ordered.map((result) => result.period);
  if (problems.length > 0) {
    return { status: 'incomplete', period, chunks, intervals, problems };
  }
  return { status: 'complete', period, chunks, intervals, summary };
}

/** Pide un tramo con el puerto de la pantalla; rechaza con `PortError` si falla. */
export type FetchCoverageChunk = (
  chunk: CoveragePeriod,
  signal: AbortSignal,
) => Promise<CoverageChunkResponse>;

function isPeriodTooLong(error: unknown): boolean {
  return (
    isPortError(error) &&
    error.failure.kind === 'http' &&
    error.failure.error.code === 'period_too_long'
  );
}

function isAbort(error: unknown, signal: AbortSignal): boolean {
  return signal.aborted || (isPortError(error) && error.failure.kind === 'aborted');
}

function measureCompose(clock: Clock, start: number, chunks: number): void {
  if (typeof performance === 'undefined' || typeof performance.measure !== 'function') return;
  performance.measure('coverage.compose', { start, end: clock.now(), detail: { chunks } });
}

/**
 * Pide el periodo por tramos, uno tras otro, y lo compone. Un tramo con `period_too_long` se
 * parte en dos y se vuelve a pedir; cualquier otro fallo deja el tramo como `missing_chunk` (con
 * su error) y el compuesto `incomplete`. Cancelar `signal` rechaza con el error de cancelación.
 */
export async function loadCoverage(options: {
  readonly period: CoveragePeriod;
  readonly fetchChunk: FetchCoverageChunk;
  readonly clock: Clock;
  readonly signal: AbortSignal;
}): Promise<CoverageComposite> {
  const { period, fetchChunk, clock, signal } = options;
  const pending = partitionPeriod(period).map((chunk) => ({ chunk, splits: 0 }));
  const results: CoverageChunkResult[] = [];
  for (let next = pending.shift(); next !== undefined; next = pending.shift()) {
    const { chunk, splits } = next;
    try {
      results.push({ period: chunk, response: await fetchChunk(chunk, signal) });
    } catch (error) {
      if (isAbort(error, signal)) throw error;
      if (isPeriodTooLong(error) && splits < MAX_SPLITS && chunk.toMs - chunk.fromMs >= 2) {
        const [first, second] = splitChunk(chunk);
        pending.unshift({ chunk: first, splits: splits + 1 }, { chunk: second, splits: splits + 1 });
      } else {
        results.push({ period: chunk, response: null, error });
      }
    }
  }
  const start = clock.now();
  const composite = composeCoverage(period, results);
  measureCompose(clock, start, results.length);
  return composite;
}
