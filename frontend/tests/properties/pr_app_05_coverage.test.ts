/**
 * PR-APP-05 (BR-APP-37, 39): para todo periodo ≤ 12 meses, la partición de la interfaz cubre el
 * periodo con tramos contiguos de ≤ 31 días; y para toda línea de tiempo servida por tramos, la
 * concatenación produce intervalos sin solape ni hueco, sin fusionar ni omitir ninguno, y un
 * resumen cuyas seis duraciones suman exactamente `to − from`. Si falta un tramo o uno no es
 * coherente, el compuesto es `incomplete` y no tiene resumen.
 *
 * El oráculo es una línea de tiempo global del periodo: el «servidor» de la prueba responde a
 * cada tramo con los intervalos globales recortados a ese tramo, como U-02. También se prueba la
 * corrección de `period_too_long`: el servidor rechaza los tramos más largos que un tope
 * generado y la interfaz los parte hasta que caben.
 */
import fc from 'fast-check';
import { describe, expect, it, vi } from 'vitest';

import { PortError } from '@/ports/base';
import { behaviourFor } from '@/ports/errors/map';
import {
  CHUNK_MAX_MS,
  composeCoverage,
  CoveragePeriodError,
  DEFAULT_PERIOD_MS,
  loadCoverage,
  MAX_PERIOD_MS,
  partitionPeriod,
  periodEndingAt,
  periodQuery,
  SUMMARY_KEYS,
  type CoverageChunkResponse,
  type CoverageChunkResult,
  type CoverageIntervalOut,
  type CoveragePeriod,
  type SummaryKey,
} from '@/state/coverage/compose';

import { FakeClock } from '../support/fakeClock';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const DAY_MS = 24 * 60 * 60 * 1000;
const EPOCH_MIN = Date.UTC(2026, 0, 1);
const EPOCH_MAX = Date.UTC(2027, 11, 31);

interface Piece {
  readonly startMs: number;
  readonly endMs: number;
  readonly layer: 'node_report' | 'platform_communication';
  readonly state: string;
  readonly causes: readonly string[];
}

const KINDS = [
  { layer: 'node_report', state: 'observable', causes: [] },
  { layer: 'node_report', state: 'degraded', causes: ['obstruction'] },
  { layer: 'node_report', state: 'not_observable', causes: ['camera_heartbeat_lost'] },
  { layer: 'platform_communication', state: 'not_observable', causes: ['no_communication'] },
  { layer: 'platform_communication', state: 'not_observable', causes: ['never_reported'] },
  { layer: 'platform_communication', state: 'not_observable', causes: ['zone_not_active'] },
] as const;

/** Longitudes de periodo con los bordes de los tramos y del tope muy representados. */
const periodLength = fc.oneof(
  fc.integer({ min: 1, max: MAX_PERIOD_MS }),
  fc.constantFrom(1, 2, 7 * DAY_MS, CHUNK_MAX_MS, CHUNK_MAX_MS + 1, MAX_PERIOD_MS),
  fc
    .tuple(fc.integer({ min: 1, max: 11 }), fc.integer({ min: -2, max: 2 }))
    .map(([chunks, delta]) => chunks * CHUNK_MAX_MS + delta),
);

const period: fc.Arbitrary<CoveragePeriod> = fc
  .tuple(fc.integer({ min: EPOCH_MIN, max: EPOCH_MAX }), periodLength)
  .map(([fromMs, length]) => ({ fromMs, toMs: fromMs + length }));

/** Una línea de tiempo global del periodo: cortes dispersos, en racimos y junto a los tramos. */
const timeline: fc.Arbitrary<{ period: CoveragePeriod; pieces: Piece[] }> = period.chain(
  (chosen) => {
    const length = chosen.toMs - chosen.fromMs;
    const offset = fc.integer({ min: 1, max: Math.max(1, length - 1) });
    const cuts =
      length < 2
        ? fc.constant<number[]>([])
        : fc
            .tuple(
              fc.array(offset, { maxLength: 30 }),
              fc.array(
                fc.tuple(offset, fc.array(fc.integer({ min: 1, max: 2000 }), { maxLength: 6 })),
                { maxLength: 4 },
              ),
              fc.array(fc.tuple(fc.integer({ min: 1, max: 11 }), fc.integer({ min: -1, max: 1 })), {
                maxLength: 4,
              }),
            )
            .map(([spread, clusters, nearChunks]) => [
              ...spread,
              ...clusters.flatMap(([base, steps]) => {
                let at = base;
                return [base, ...steps.map((step) => (at += step))];
              }),
              ...nearChunks.map(([chunks, delta]) => chunks * CHUNK_MAX_MS + delta),
            ]);
    return fc
      .tuple(cuts, fc.array(fc.constantFrom(...KINDS), { minLength: 64, maxLength: 64 }))
      .map(([offsets, kinds]) => {
        const points = [
          ...new Set([0, length, ...offsets.filter((at) => at > 0 && at < length)]),
        ].sort((a, b) => a - b);
        const pieces: Piece[] = [];
        for (let index = 1; index < points.length; index += 1) {
          const kind = kinds[index % kinds.length] ?? KINDS[0];
          pieces.push({
            startMs: chosen.fromMs + (points[index - 1] ?? 0),
            endMs: chosen.fromMs + (points[index] ?? 0),
            ...kind,
          });
        }
        return { period: chosen, pieces };
      });
  },
);

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

/** Contador de U-02: la causa en la capa de la plataforma, el estado en la del nodo. */
function bucket(piece: Pick<Piece, 'layer' | 'state' | 'causes'>): SummaryKey {
  return (
    piece.layer === 'platform_communication' ? `${piece.causes[0] ?? ''}_ms` : `${piece.state}_ms`
  ) as SummaryKey;
}

/** Lo que U-02 respondería a `chunk`: los intervalos globales recortados y su resumen. */
function serve(pieces: readonly Piece[], chunk: CoveragePeriod): CoverageChunkResponse {
  const summary = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, 0])) as Record<
    SummaryKey,
    number
  >;
  const intervals: CoverageIntervalOut[] = [];
  for (const piece of pieces) {
    const startMs = Math.max(piece.startMs, chunk.fromMs);
    const endMs = Math.min(piece.endMs, chunk.toMs);
    if (endMs <= startMs) continue;
    summary[bucket(piece)] += endMs - startMs;
    intervals.push({
      starts_at: iso(startMs),
      ends_at: iso(endMs),
      duration_ms: endMs - startMs,
      layer: piece.layer,
      state: piece.state,
      causes: [...piece.causes],
      clock_basis: piece.layer === 'node_report' ? 'node' : 'platform',
    });
  }
  return { period: { from: iso(chunk.fromMs), to: iso(chunk.toMs) }, intervals, summary };
}

function total(summary: Readonly<Record<SummaryKey, number>>): number {
  return SUMMARY_KEYS.reduce((sum, key) => sum + summary[key], 0);
}

function expectExactCover(
  composite: ReturnType<typeof composeCoverage>,
  chosen: CoveragePeriod,
): void {
  let cursor = chosen.fromMs;
  for (const interval of composite.intervals) {
    expect(interval.startMs).toBe(cursor);
    expect(interval.endMs).toBeGreaterThan(interval.startMs);
    cursor = interval.endMs;
  }
  expect(cursor).toBe(chosen.toMs);
}

type Mutable<T> = { -readonly [K in keyof T]: T[K] };

/** Cambia el primer intervalo de la respuesta. */
function withFirst(
  response: CoverageChunkResponse,
  change: (interval: Mutable<CoverageIntervalOut>) => void,
): CoverageChunkResponse {
  const [first, ...rest] = response.intervals;
  if (first === undefined) throw new Error('Un tramo servido tiene al menos un intervalo.');
  const copy = { ...first };
  change(copy);
  return { ...response, intervals: [copy, ...rest] };
}

/**
 * Respuestas incoherentes con su tramo; cada una la detecta una comprobación distinta de
 * `composeCoverage` (además, `missing` es un tramo sin respuesta y `absent`, uno que falta).
 */
const DAMAGES = {
  missing: (response: CoverageChunkResponse) => response,
  absent: (response: CoverageChunkResponse) => response,
  wrong_period: (response: CoverageChunkResponse, chunk: CoveragePeriod) => ({
    ...response,
    period: { from: iso(chunk.fromMs + 1), to: iso(chunk.toMs) },
  }),
  summary_inflated: (response: CoverageChunkResponse) => ({
    ...response,
    summary: { ...response.summary, observable_ms: response.summary.observable_ms + 1 },
  }),
  first_dropped: (response: CoverageChunkResponse) => ({
    ...response,
    intervals: response.intervals.slice(1),
  }),
  last_dropped: (response: CoverageChunkResponse) => ({
    ...response,
    intervals: response.intervals.slice(0, -1),
  }),
  /** `duration_ms` no es `ends_at − starts_at`, con el resumen ajustado a esa duración. */
  duration_mismatch: (response: CoverageChunkResponse) => {
    const first = response.intervals[0];
    if (first === undefined) throw new Error('Un tramo servido tiene al menos un intervalo.');
    const key = bucket(first as Pick<Piece, 'layer' | 'state' | 'causes'>);
    return {
      ...withFirst(response, (interval) => {
        interval.duration_ms += 1;
      }),
      summary: { ...response.summary, [key]: response.summary[key] + 1 },
    };
  },
  unknown_state: (response: CoverageChunkResponse) =>
    withFirst(response, (interval) => {
      interval.layer = 'node_report';
      interval.state = 'clear';
    }),
  unknown_layer: (response: CoverageChunkResponse) =>
    withFirst(response, (interval) => {
      interval.layer = 'composite';
    }),
  /** Un intervalo de duración cero al principio: no rompe la contigüidad ni el resumen. */
  empty_interval: (response: CoverageChunkResponse) => {
    const first = response.intervals[0];
    if (first === undefined) throw new Error('Un tramo servido tiene al menos un intervalo.');
    const empty = { ...first, ends_at: first.starts_at, duration_ms: 0 };
    return { ...response, intervals: [empty, ...response.intervals] };
  },
} as const;
type Damage = keyof typeof DAMAGES;

function periodTooLong(): PortError {
  const error = { code: 'period_too_long', status: 422 };
  return new PortError({ kind: 'http', error, behaviour: behaviourFor(error) });
}

describe('PR-APP-05 composición de cobertura por tramos', () => {
  it(
    'la partición cubre el periodo con tramos contiguos de ≤ 31 días (≤ 12)',
    () => {
      fc.assert(
        fc.property(period, (chosen) => {
          const chunks = partitionPeriod(chosen);
          const length = chosen.toMs - chosen.fromMs;
          expect(chunks.length).toBe(Math.ceil(length / CHUNK_MAX_MS));
          expect(chunks.length).toBeLessThanOrEqual(12);
          let cursor = chosen.fromMs;
          for (const chunk of chunks) {
            expect(chunk.fromMs).toBe(cursor);
            expect(chunk.toMs - chunk.fromMs).toBeGreaterThan(0);
            expect(chunk.toMs - chunk.fromMs).toBeLessThanOrEqual(CHUNK_MAX_MS);
            cursor = chunk.toMs;
          }
          expect(cursor).toBe(chosen.toMs);
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it('rechaza periodos vacíos, invertidos o de más de 12 meses', () => {
    fc.assert(
      fc.property(
        fc.integer({ min: EPOCH_MIN, max: EPOCH_MAX }),
        fc.oneof(
          fc.integer({ min: -MAX_PERIOD_MS, max: 0 }),
          fc.integer({ min: MAX_PERIOD_MS + 1, max: 2 * MAX_PERIOD_MS }),
        ),
        (fromMs, length) => {
          expect(() => partitionPeriod({ fromMs, toMs: fromMs + length })).toThrow(
            CoveragePeriodError,
          );
        },
      ),
    );
  });

  it(
    'la concatenación no solapa, no deja hueco, no fusiona y su resumen suma to − from',
    () => {
      fc.assert(
        fc.property(timeline, ({ period: chosen, pieces }) => {
          const results = partitionPeriod(chosen).map((chunk) => ({
            period: chunk,
            response: serve(pieces, chunk),
          }));
          const composite = composeCoverage(chosen, results);
          expect(composite.status).toBe('complete');
          if (composite.status !== 'complete') return;
          // Cada intervalo de cada tramo, en orden, sin fusionar ni omitir ninguno.
          const served = results.flatMap(({ response }) => response.intervals);
          expect(composite.intervals.map((interval) => interval.interval)).toEqual(served);
          expectExactCover(composite, chosen);
          expect(total(composite.summary)).toBe(chosen.toMs - chosen.fromMs);
          for (const key of SUMMARY_KEYS) {
            const expected = pieces
              .filter((piece) => bucket(piece) === key)
              .reduce((sum, piece) => sum + piece.endMs - piece.startMs, 0);
            expect(composite.summary[key]).toBe(expected);
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'si falta un tramo o uno no es coherente, el compuesto es incompleto y sin resumen',
    () => {
      fc.assert(
        fc.property(
          timeline,
          fc.nat(),
          fc.constantFrom(...(Object.keys(DAMAGES) as Damage[])),
          ({ period: chosen, pieces }, pick, damage) => {
            const chunks = partitionPeriod(chosen);
            const broken = pick % chunks.length;
            const results = chunks.flatMap((chunk, index): CoverageChunkResult[] => {
              const response = serve(pieces, chunk);
              if (index !== broken) return [{ period: chunk, response }];
              if (damage === 'missing') {
                return [{ period: chunk, response: null, error: new Error('sin respuesta') }];
              }
              if (damage === 'absent') return [];
              return [{ period: chunk, response: DAMAGES[damage](response, chunk) }];
            });
            const composite = composeCoverage(chosen, results);
            expect(composite.status).toBe('incomplete');
            expect('summary' in composite).toBe(false);
            if (composite.status !== 'incomplete') return;
            const expected =
              damage === 'missing'
                ? 'missing_chunk'
                : damage === 'absent'
                  ? 'not_contiguous'
                  : 'inconsistent_chunk';
            expect(composite.problems.map((problem) => problem.kind)).toContain(expected);
            if (expected !== 'not_contiguous') {
              expect(composite.problems).toContainEqual({ kind: expected, period: chunks[broken] });
            }
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'period_too_long: la interfaz parte el tramo hasta que cabe y el resultado sigue exacto',
    async () => {
      await fc.assert(
        fc.asyncProperty(
          timeline,
          fc.integer({ min: DAY_MS, max: CHUNK_MAX_MS }),
          async ({ period: chosen, pieces }, limit) => {
            const asked: CoveragePeriod[] = [];
            const fetchChunk = (chunk: CoveragePeriod) => {
              asked.push(chunk);
              return chunk.toMs - chunk.fromMs > limit
                ? Promise.reject(periodTooLong())
                : Promise.resolve(serve(pieces, chunk));
            };
            const composite = await loadCoverage({
              period: chosen,
              fetchChunk,
              clock: new FakeClock(chosen.toMs),
              signal: new AbortController().signal,
            });
            expect(composite.status).toBe('complete');
            if (composite.status !== 'complete') return;
            expect(composite.chunks.every((c) => c.toMs - c.fromMs <= limit)).toBe(true);
            expectExactCover(composite, chosen);
            expect(total(composite.summary)).toBe(chosen.toMs - chosen.fromMs);
            const served = composite.chunks.flatMap((chunk) => serve(pieces, chunk).intervals);
            expect(composite.intervals.map((interval) => interval.interval)).toEqual(served);
            expect(asked.length).toBeGreaterThanOrEqual(composite.chunks.length);
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});

describe('PR-APP-05 ejemplos de carga', () => {
  const chosen = { fromMs: EPOCH_MIN, toMs: EPOCH_MIN + 40 * DAY_MS };
  const pieces: Piece[] = [{ startMs: chosen.fromMs, endMs: chosen.toMs, ...KINDS[0] }];

  it('el periodo por omisión es de 7 días, cabe en un tramo y se consulta en ISO con ms y Z', () => {
    const fallback = periodEndingAt(chosen.toMs, DEFAULT_PERIOD_MS);
    expect(fallback).toEqual({ fromMs: chosen.toMs - 7 * DAY_MS, toMs: chosen.toMs });
    expect(partitionPeriod(fallback)).toEqual([fallback]);
    expect(periodQuery(fallback)).toEqual({
      from: '2026-02-03T00:00:00.000Z',
      to: '2026-02-10T00:00:00.000Z',
    });
  });

  it('un tramo de más de 31 días no se acepta aunque su respuesta sea coherente', () => {
    const composite = composeCoverage(chosen, [
      { period: chosen, response: serve(pieces, chosen) },
    ]);
    expect(composite.status).toBe('incomplete');
    if (composite.status !== 'incomplete') return;
    expect(composite.problems).toEqual([{ kind: 'inconsistent_chunk', period: chosen }]);
  });

  it('un fallo distinto de period_too_long deja el tramo como missing_chunk con su error', async () => {
    const failure = new Error('red caída');
    const composite = await loadCoverage({
      period: chosen,
      fetchChunk: (chunk) =>
        chunk.fromMs === chosen.fromMs
          ? Promise.resolve(serve(pieces, chunk))
          : Promise.reject(failure),
      clock: new FakeClock(chosen.toMs),
      signal: new AbortController().signal,
    });
    expect(composite.status).toBe('incomplete');
    if (composite.status !== 'incomplete') return;
    expect(composite.problems).toEqual([
      {
        kind: 'missing_chunk',
        period: { fromMs: chosen.fromMs + CHUNK_MAX_MS, toMs: chosen.toMs },
      },
    ]);
  });

  it('cancelar la carga rechaza con el error de cancelación', async () => {
    const controller = new AbortController();
    const aborted = new Error('cancelada');
    const promise = loadCoverage({
      period: chosen,
      fetchChunk: () => {
        controller.abort();
        return Promise.reject(aborted);
      },
      clock: new FakeClock(chosen.toMs),
      signal: controller.signal,
    });
    await expect(promise).rejects.toBe(aborted);
  });

  it('marca de rendimiento coverage.compose con el reloj inyectado', async () => {
    const measure = vi.spyOn(performance, 'measure');
    const clock = new FakeClock(chosen.toMs);
    await loadCoverage({
      period: chosen,
      fetchChunk: (chunk) => Promise.resolve(serve(pieces, chunk)),
      clock,
      signal: new AbortController().signal,
    });
    expect(measure).toHaveBeenCalledWith('coverage.compose', {
      start: chosen.toMs,
      end: chosen.toMs,
      detail: { chunks: 2 },
    });
  });
});
