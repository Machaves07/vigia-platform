/**
 * UI-11 `CoverageTimeline` (BR-APP-37, 39, 41; PAT-APP-REN-03): todos los intervalos dibujados,
 * marcas de menos de un píxel cuyo detalle lista cada intervalo con su duración (con el cursor y
 * con el teclado), «cobertura incompleta» sin ningún porcentaje cuando falta un tramo, base de
 * reloj por capa, selector de periodo y `axe` sin violaciones serias. Se comparan textos exactos
 * del catálogo, roles y nombres accesibles.
 */
import { render, screen, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactElement } from 'react';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';

import { CoverageTimeline } from '@/components/CoverageTimeline/CoverageTimeline';
import {
  composeCoverage,
  SUMMARY_KEYS,
  type CoverageChunkResponse,
  type CoverageComposite,
  type CoverageIntervalOut,
  type CoveragePeriod,
  type SummaryKey,
} from '@/state/coverage/compose';
import catalog from '@/texts/coverage.json';
import status from '@/texts/components_status.json';

import { FakeClock } from '../support/fakeClock';

const DAY_MS = 24 * 60 * 60 * 1000;
const FROM = Date.UTC(2026, 9, 1);
const WIDTH = 700;

type Spec = readonly [
  durationMs: number,
  layer: 'node_report' | 'platform_communication',
  state: string,
  causes: readonly string[],
];

function iso(ms: number): string {
  return new Date(ms).toISOString();
}

/** Respuesta de U-02 para `chunk` con los intervalos de `specs`, contiguos desde su inicio. */
function response(chunk: CoveragePeriod, specs: readonly Spec[]): CoverageChunkResponse {
  const summary = Object.fromEntries(SUMMARY_KEYS.map((key) => [key, 0])) as Record<
    SummaryKey,
    number
  >;
  const intervals: CoverageIntervalOut[] = [];
  let cursor = chunk.fromMs;
  for (const [durationMs, layer, state, causes] of specs) {
    const key = (
      layer === 'platform_communication' ? `${causes[0] ?? ''}_ms` : `${state}_ms`
    ) as SummaryKey;
    summary[key] += durationMs;
    intervals.push({
      starts_at: iso(cursor),
      ends_at: iso(cursor + durationMs),
      duration_ms: durationMs,
      layer,
      state,
      causes,
      clock_basis: layer === 'node_report' ? 'node' : 'platform',
    });
    cursor += durationMs;
  }
  if (cursor !== chunk.toMs) throw new Error('El ejemplo no cubre el tramo.');
  return { period: { from: iso(chunk.fromMs), to: iso(chunk.toMs) }, intervals, summary };
}

/**
 * Siete días en 700 px (1 px ≈ 14,4 min): tres intervalos breves del nodo en la misma columna
 * (una marca de 3), uno breve de la plataforma entre ellos (una marca de 1) y barras largas.
 */
const WEEK: CoveragePeriod = { fromMs: FROM, toMs: FROM + 7 * DAY_MS };
const SHORT = 40 + 1_500 + 300_000 + 2_000;
const WEEK_SPECS: readonly Spec[] = [
  [2 * DAY_MS, 'node_report', 'observable', []],
  [40, 'node_report', 'degraded', ['obstruction']],
  [1_500, 'platform_communication', 'not_observable', ['no_communication']],
  [300_000, 'node_report', 'not_observable', ['camera_heartbeat_lost']],
  [2_000, 'node_report', 'degraded', ['focus']],
  [3 * DAY_MS - SHORT, 'node_report', 'observable', []],
  [2 * DAY_MS, 'platform_communication', 'not_observable', ['zone_not_active']],
];

function week(): CoverageComposite {
  return composeCoverage(WEEK, [{ period: WEEK, response: response(WEEK, WEEK_SPECS) }]);
}

/** 40 días en dos tramos; el segundo no llegó. */
function missingChunk(): CoverageComposite {
  const period = { fromMs: FROM, toMs: FROM + 40 * DAY_MS };
  const first = { fromMs: FROM, toMs: FROM + 31 * DAY_MS };
  const second = { fromMs: first.toMs, toMs: period.toMs };
  return composeCoverage(period, [
    { period: first, response: response(first, [[31 * DAY_MS, 'node_report', 'observable', []]]) },
    { period: second, response: null, error: new Error('sin respuesta') },
  ]);
}

function timeline(composite: CoverageComposite, onPeriodChange = vi.fn()): ReactElement {
  return (
    <CoverageTimeline
      composite={composite}
      timeZone="America/Bogota"
      clock={new FakeClock(composite.period.toMs)}
      onPeriodChange={onPeriodChange}
      widthPx={WIDTH}
    />
  );
}

function detail(): HTMLElement {
  const heading = screen.getByRole('heading', { name: catalog['coverage.detail.title'] });
  const container = heading.parentElement;
  if (container === null) throw new Error('El detalle no tiene contenedor.');
  return container;
}

function durationsIn(element: HTMLElement): string[] {
  return within(element)
    .getAllByRole('listitem')
    .map((item) => item.querySelector('[data-part="duration"]')?.textContent ?? '');
}

async function expectNoSeriousViolations(ui: ReactElement) {
  const { container } = render(<main>{ui}</main>);
  const results = await axe(container);
  const serious = results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
  expect(serious).toEqual([]);
  expect(results).toHaveNoViolations();
}

describe('UI-11 CoverageTimeline', () => {
  it.each([
    ['completa', week],
    ['incompleta', missingChunk],
  ])('axe sin violaciones: %s', async (_name, build) => {
    await expectNoSeriousViolations(timeline(build()));
  });

  it('dibuja todos los intervalos: barras y marcas suman los siete', () => {
    render(timeline(week()));
    const lanes = screen.getAllByRole('list', { name: catalog['coverage.timeline.lane_items'] });
    const items = lanes.flatMap((lane) => within(lane).getAllByRole('listitem'));
    const drawn = items.reduce((sum, item) => sum + Number(item.getAttribute('data-count')), 0);
    expect(drawn).toBe(WEEK_SPECS.length);
    expect(items.map((item) => item.getAttribute('aria-label'))).toEqual([
      'Lo que el nodo informa: Observable, 2 d',
      'Lo que el nodo informa: 3 intervalos de menos de un píxel',
      'Lo que el nodo informa: Observable, 2 d 23 h 54 min 56 s 460 ms',
      'Lo que la plataforma supo: 1 intervalo de menos de un píxel',
      'Lo que la plataforma supo: Zona no activa: no se observó, 2 d',
    ]);
  });

  it('el resumen completo da los seis contadores en minutos y porcentaje', () => {
    render(timeline(week()));
    const table = screen.getByRole('table', { name: catalog['coverage.summary.title'] });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => within(row).getByRole('rowheader').textContent)).toEqual(
      SUMMARY_KEYS.map((key) => catalog[`coverage.summary.${key}`]),
    );
    const zoneNotActive = rows[SUMMARY_KEYS.indexOf('zone_not_active_ms')];
    if (zoneNotActive === undefined) throw new Error('Falta la fila.');
    expect(
      within(zoneNotActive)
        .getAllByRole('cell')
        .map((cell) => cell.textContent),
    ).toEqual(['2.880', '28,6 %']);
  });

  it('con un tramo que falta muestra «cobertura incompleta» y ningún porcentaje', () => {
    const composite = missingChunk();
    expect(composite.status).toBe('incomplete');
    render(timeline(composite));
    const notice = screen.getByRole('status');
    expect(within(notice).getByText(catalog['coverage.incomplete.title'])).toBeTruthy();
    expect(within(notice).getByRole('listitem').getAttribute('data-kind')).toBe('missing_chunk');
    expect(screen.queryByRole('table')).toBeNull();
    expect(screen.queryAllByText(/%/)).toEqual([]);
    // El tramo que llegó sigue dibujado; el que falta, como hueco visible.
    expect(screen.getAllByRole('listitem', { name: /^Lo que el nodo informa:/ })).toHaveLength(1);
  });

  it('el hueco de un tramo que se sale del periodo se recorta al ancho de la línea', () => {
    const period = { fromMs: FROM, toMs: FROM + 40 * DAY_MS };
    const first = { fromMs: FROM, toMs: FROM + 31 * DAY_MS };
    const overrun = { fromMs: first.toMs, toMs: period.toMs + DAY_MS };
    const composite = composeCoverage(period, [
      {
        period: first,
        response: response(first, [[31 * DAY_MS, 'node_report', 'observable', []]]),
      },
      {
        period: overrun,
        response: response(overrun, [[10 * DAY_MS, 'node_report', 'observable', []]]),
      },
    ]);
    const { container } = render(timeline(composite));
    const holes = [...container.querySelectorAll('[data-part="missing"]')].map((hole) => [
      Number(hole.getAttribute('x')),
      Number(hole.getAttribute('width')),
    ]);
    expect(holes).toHaveLength(2);
    for (const [x = 0, width = 0] of holes) {
      expect(x).toBeCloseTo((31 / 40) * WIDTH);
      expect(x + width).toBeCloseTo(WIDTH);
    }
  });

  it('el detalle de una marca lista cada intervalo con su duración, por teclado', async () => {
    const user = userEvent.setup();
    render(timeline(week()));
    await user.tab();
    expect(document.activeElement).toBe(screen.getByRole('combobox'));
    await user.tab();
    expect(document.activeElement?.getAttribute('aria-label')).toBe(
      'Lo que el nodo informa: Observable, 2 d',
    );
    await user.keyboard('{ArrowRight}');
    const mark = document.activeElement;
    expect(mark?.getAttribute('data-kind')).toBe('mark');
    expect(mark?.getAttribute('aria-label')).toBe(
      'Lo que el nodo informa: 3 intervalos de menos de un píxel',
    );
    expect(durationsIn(detail())).toEqual(['40 ms', '5 min', '2 s']);
    const labels = within(detail())
      .getAllByRole('listitem')
      .map((item) => item.querySelector('[data-part="interval-label"]')?.textContent);
    expect(labels).toEqual([
      'Degradada (obstrucción)',
      'No observable: no se observó (latido de cámara perdido)',
      'Degradada (desenfoque)',
    ]);
    await user.keyboard('{End}');
    expect(document.activeElement?.getAttribute('aria-label')).toBe(
      'Lo que la plataforma supo: Zona no activa: no se observó, 2 d',
    );
    await user.keyboard('{Home}');
    expect(document.activeElement?.getAttribute('aria-label')).toBe(
      'Lo que el nodo informa: Observable, 2 d',
    );
  });

  it('al pasar el cursor por una marca de la plataforma, el detalle lista su intervalo', async () => {
    const user = userEvent.setup();
    render(timeline(week()));
    expect(within(detail()).getByText(catalog['coverage.detail.hint'])).toBeTruthy();
    await user.hover(
      screen.getByRole('listitem', {
        name: 'Lo que la plataforma supo: 1 intervalo de menos de un píxel',
      }),
    );
    expect(durationsIn(detail())).toEqual(['1 s 500 ms']);
    expect(
      within(detail()).getByText(catalog['coverage.cause.no_communication'], {
        selector: '[data-part="interval-label"]',
      }),
    ).toBeTruthy();
  });

  it('cada capa muestra su base de reloj y la leyenda es fija', () => {
    render(timeline(week()));
    const bases = [...document.querySelectorAll('[data-part="clock-basis"]')].map(
      (element) => element.textContent,
    );
    expect(bases).toEqual([
      catalog['coverage.clock_basis.node'],
      catalog['coverage.clock_basis.platform'],
    ]);
    const legend = document.querySelector('[data-part="legend"]');
    if (!(legend instanceof HTMLElement)) throw new Error('Falta la leyenda.');
    expect(within(legend).getByText(catalog['coverage.legend.not_observed'])).toBeTruthy();
    expect(within(legend).getByText(status['components_status.observability.legend'])).toBeTruthy();
  });

  it('el selector de periodo emite el nuevo periodo con el mismo final', async () => {
    const user = userEvent.setup();
    const onPeriodChange = vi.fn();
    render(timeline(week(), onPeriodChange));
    const select = screen.getByRole('combobox', { name: catalog['coverage.period.label'] });
    expect((select as HTMLSelectElement).value).toBe('last_7_days');
    await user.selectOptions(select, 'last_31_days');
    expect(onPeriodChange).toHaveBeenCalledWith({
      fromMs: WEEK.toMs - 31 * DAY_MS,
      toMs: WEEK.toMs,
    });
  });

  it('los textos de cobertura dicen «no se observó», nunca «no ocurrió» (BR-APP-41)', () => {
    const values: string[] = Object.values(catalog);
    expect(values.filter((value) => /no ocurri/i.test(value))).toEqual([]);
    for (const key of [
      'coverage.state.not_observable',
      'coverage.cause.no_communication',
      'coverage.cause.never_reported',
      'coverage.cause.zone_not_active',
    ] as const) {
      expect(catalog[key].endsWith('no se observó')).toBe(true);
    }
  });

  it('marca de rendimiento coverage_timeline.render con el reloj inyectado', () => {
    const measure = vi.spyOn(performance, 'measure');
    const composite = week();
    render(timeline(composite));
    expect(measure).toHaveBeenCalledWith('coverage_timeline.render', {
      start: WEEK.toMs,
      end: WEEK.toMs,
      detail: { intervals: WEEK_SPECS.length },
    });
  });
});
