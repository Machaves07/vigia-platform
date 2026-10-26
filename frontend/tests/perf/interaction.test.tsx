/**
 * Presupuesto de interacción (NFR-APP-03, PAT-APP-REN-04) con las marcas de VIG-296 (UI-14) y
 * VIG-297 (UI-11 y la composición de cobertura), medidas con el reloj real y datos generados al
 * tamaño máximo:
 *
 * - bandeja de 50 filas renderizada ≤ 200 ms tras la respuesta (`keyset_table.render`);
 * - línea de tiempo de 31 días ≤ 300 ms (`coverage_timeline.render`);
 * - composición de 12 tramos ≤ 100 ms (`coverage.compose`);
 * - respuesta visible a la interacción ≤ 100 ms: el esqueleto o el estado de carga en el mismo
 *   render que el clic.
 *
 * Los umbrales son tendencia en `ci` y bloqueantes en `nightly` (`./budget.ts`). En los dos perfiles
 * la prueba exige que la marca exista con el tamaño pedido: una marca que deja de emitirse falla.
 *
 * Tamaño máximo de la cobertura `[supuesto de esta prueba]`: el contrato no acota `intervals`, así
 * que se toma un cambio de estado cada 15 min durante todo el periodo (2 976 intervalos en 31 días,
 * 35 136 en los 12 tramos de 366 días), muy por encima de lo que produce un nodo sano.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { afterEach, describe, expect, it } from 'vitest';

import { CoverageTimeline } from '@/components/CoverageTimeline/CoverageTimeline';
import { KeysetTable, type KeysetColumn } from '@/components/KeysetTable/KeysetTable';
import type { KeysetPage } from '@/components/KeysetTable/keyset';
import { systemClock } from '@/state/clock';
import {
  CHUNK_MAX_MS,
  composeCoverage,
  loadCoverage,
  MAX_PERIOD_MS,
  partitionPeriod,
  SUMMARY_KEYS,
  type CoverageChunkResponse,
  type CoverageIntervalOut,
  type CoveragePeriod,
  type SummaryKey,
} from '@/state/coverage/compose';
import catalog from '@/texts/components_structure.json';

import { checkBudget } from './budget';

const WAIT = { timeout: 30_000 };
const INBOX_ROWS = 50;
const STEP_MS = 15 * 60 * 1000;
const TO_MS = Date.UTC(2026, 9, 1);

afterEach(() => {
  performance.clearMarks();
  performance.clearMeasures();
});

/** Última medida `name` (la de esta prueba: `afterEach` limpia las anteriores). */
function lastMeasure(name: string): PerformanceMeasure {
  const entries = performance.getEntriesByName(name, 'measure');
  const entry = entries.at(-1);
  if (entry === undefined) throw new Error(`No se emitió la marca ${name}.`);
  return entry as PerformanceMeasure;
}

// --- Bandeja (UI-14) ---------------------------------------------------------------------------

interface InboxRow {
  readonly finding_state_id: string;
  readonly zone: string;
  readonly family: string;
  readonly tier: string;
  readonly state: string;
  readonly observability: string;
  readonly occurred_at: string;
  readonly age_days: number;
  readonly due_at: string;
}

function inboxRow(index: number): InboxRow {
  return {
    finding_state_id: `0190a1b2-2222-7000-8000-${String(index).padStart(12, '0')}`,
    zone: `Zona sintética ${String(index % 7)}`,
    family: ['coexistence', 'restricted_area', 'ppe'][index % 3] ?? 'ppe',
    tier: index % 2 === 0 ? 'tier_1' : 'tier_2',
    state: ['received', 'in_review', 'action_assigned'][index % 3] ?? 'received',
    observability: ['observable', 'degraded'][index % 2] ?? 'observable',
    occurred_at: new Date(TO_MS - index * 60_000).toISOString(),
    age_days: index % 30,
    due_at: new Date(TO_MS + index * 86_400_000).toISOString(),
  };
}

const INBOX_COLUMNS: readonly KeysetColumn<InboxRow>[] = [
  { id: 'id', header: 'Hallazgo', cell: (row) => row.finding_state_id },
  { id: 'zone', header: 'Zona', cell: (row) => row.zone },
  { id: 'family', header: 'Familia', cell: (row) => row.family },
  { id: 'tier', header: 'Nivel', cell: (row) => row.tier },
  { id: 'state', header: 'Estado', cell: (row) => row.state },
  { id: 'observability', header: 'Observabilidad', cell: (row) => row.observability },
  { id: 'occurred', header: 'Ocurrió', cell: (row) => row.occurred_at },
  { id: 'age', header: 'Días', cell: (row) => String(row.age_days) },
  { id: 'due', header: 'Plazo', cell: (row) => row.due_at },
];

function inboxPage(): KeysetPage<InboxRow> {
  return {
    items: Array.from({ length: INBOX_ROWS }, (_, index) => inboxRow(index + 1)),
    nextCursor: { cursor: 'c3ludGhldGljLWN1cnNvcg' },
  };
}

// --- Cobertura (UI-11 y composición) -------------------------------------------------------------

type Spec = readonly [layer: string, state: string, causes: readonly string[]];
/** Ciclo de estados de las dos capas; cada uno cuenta en una clave distinta del resumen. */
const CYCLE: readonly Spec[] = [
  ['node_report', 'observable', []],
  ['node_report', 'degraded', ['focus']],
  ['platform_communication', 'not_observable', ['no_communication']],
  ['node_report', 'not_observable', ['camera_heartbeat_lost']],
];

function summaryKey([layer, state, causes]: Spec): SummaryKey {
  return (
    layer === 'platform_communication' ? `${causes[0] ?? ''}_ms` : `${state}_ms`
  ) as SummaryKey;
}

/** Respuesta de U-02 para `chunk`: intervalos contiguos de 15 min (el último, el resto). */
function denseChunk(chunk: CoveragePeriod): CoverageChunkResponse {
  const summary = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, 0])) as Record<
    SummaryKey,
    number
  >;
  const intervals: CoverageIntervalOut[] = [];
  for (let start = chunk.fromMs, index = 0; start < chunk.toMs; start += STEP_MS, index++) {
    const end = Math.min(start + STEP_MS, chunk.toMs);
    const spec = CYCLE[index % CYCLE.length] ?? CYCLE[0];
    if (spec === undefined) throw new Error('Ciclo vacío.');
    const [layer, state, causes] = spec;
    summary[summaryKey(spec)] += end - start;
    intervals.push({
      starts_at: new Date(start).toISOString(),
      ends_at: new Date(end).toISOString(),
      duration_ms: end - start,
      layer,
      state,
      causes,
      clock_basis: layer === 'node_report' ? 'node' : 'platform',
    });
  }
  return {
    period: { from: new Date(chunk.fromMs).toISOString(), to: new Date(chunk.toMs).toISOString() },
    intervals,
    summary,
  };
}

const MONTH: CoveragePeriod = { fromMs: TO_MS - CHUNK_MAX_MS, toMs: TO_MS };
const YEAR: CoveragePeriod = { fromMs: TO_MS - MAX_PERIOD_MS, toMs: TO_MS };

describe('NFR-APP-03: presupuesto de interacción', { timeout: 120_000 }, () => {
  it('bandeja de 50 filas: render ≤ 200 ms tras la respuesta', async () => {
    render(
      <main>
        <KeysetTable<InboxRow>
          caption="Bandeja"
          columns={INBOX_COLUMNS}
          rowKey={(row) => row.finding_state_id}
          fetchPage={() => Promise.resolve(inboxPage())}
          clock={systemClock}
          refreshPolicy="periodic_60s"
        />
      </main>,
    );
    await waitFor(() => {
      expect(screen.getAllByRole('row')).toHaveLength(INBOX_ROWS + 1);
    }, WAIT);
    const measure = lastMeasure('keyset_table.render');
    expect(measure.detail).toEqual({ rows: INBOX_ROWS });
    checkBudget('bandeja de 50 filas (keyset_table.render)', measure.duration, 200);
  });

  it('línea de tiempo de 31 días al tamaño máximo: render ≤ 300 ms', () => {
    const composite = composeCoverage(MONTH, [{ period: MONTH, response: denseChunk(MONTH) }]);
    expect(composite.status).toBe('complete');
    expect(composite.intervals.length).toBe(CHUNK_MAX_MS / STEP_MS);
    render(
      <CoverageTimeline
        composite={composite}
        timeZone="America/Bogota"
        clock={systemClock}
        onPeriodChange={() => undefined}
      />,
    );
    const measure = lastMeasure('coverage_timeline.render');
    expect(measure.detail).toEqual({ intervals: composite.intervals.length });
    checkBudget('línea de tiempo de 31 días (coverage_timeline.render)', measure.duration, 300);
  });

  it('composición de 12 tramos al tamaño máximo: ≤ 100 ms', async () => {
    const chunks = partitionPeriod(YEAR);
    expect(chunks).toHaveLength(12);
    // Las respuestas se generan antes: la marca mide solo la composición, no la generación.
    const responses = new Map(chunks.map((chunk) => [chunk.fromMs, denseChunk(chunk)]));
    const composite = await loadCoverage({
      period: YEAR,
      fetchChunk: (chunk) => {
        const response = responses.get(chunk.fromMs);
        return response === undefined
          ? Promise.reject(new Error('Tramo inesperado.'))
          : Promise.resolve(response);
      },
      clock: systemClock,
      signal: new AbortController().signal,
    });
    expect(composite.status).toBe('complete');
    const measure = lastMeasure('coverage.compose');
    expect(measure.detail).toEqual({ chunks: 12 });
    checkBudget('composición de 12 tramos (coverage.compose)', measure.duration, 100);
  });

  it('respuesta visible ≤ 100 ms: «Cargar más» muestra el estado de carga en el mismo render', async () => {
    let calls = 0;
    render(
      <main>
        <KeysetTable<InboxRow>
          caption="Bandeja"
          columns={INBOX_COLUMNS}
          rowKey={(row) => row.finding_state_id}
          // La segunda página no llega nunca: se mide solo la respuesta visible al clic.
          fetchPage={() =>
            calls++ === 0 ? Promise.resolve(inboxPage()) : new Promise<never>(() => undefined)
          }
          clock={systemClock}
        />
      </main>,
    );
    await waitFor(() => {
      expect(screen.getAllByRole('row')).toHaveLength(INBOX_ROWS + 1);
    }, WAIT);
    const section = screen.getByRole('region', { name: 'Bandeja' });
    expect(section.getAttribute('aria-busy')).toBe('false');
    const button = within(section).getByRole('button', {
      name: catalog['components_structure.keyset.load_more'],
    });
    const startedAt = systemClock.now();
    act(() => {
      fireEvent.click(button);
    });
    const visibleAt = systemClock.now();
    expect(section.getAttribute('aria-busy')).toBe('true');
    expect(calls).toBe(2);
    checkBudget('respuesta visible a «Cargar más»', visibleAt - startedAt, 100);
  });
});
