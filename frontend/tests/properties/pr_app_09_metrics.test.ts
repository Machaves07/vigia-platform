/**
 * PR-APP-09 (BR-APP-28, 80; P7): para todo conjunto de métricas por zona y periodo con
 * dimensiones inyectadas, las visualizaciones que UI-23 acepta pertenecen a la lista cerrada y
 * ninguna serie tiene dimensión de persona, equipo o turno: toda serie con una de esas
 * dimensiones (en la lista, en un punto o en la raíz, con mayúsculas, separadores o caracteres
 * invisibles) se rechaza con `MetricsContractError`.
 */
import { render } from '@testing-library/react';
import fc from 'fast-check';
import { createElement } from 'react';
import { describe, expect, it } from 'vitest';

import { MetricsChart } from '@/components/MetricsChart/MetricsChart';
import {
  DIMENSIONS,
  MetricsContractError,
  parseMetricsSeries,
  VISUALIZATIONS,
} from '@/components/MetricsChart/metrics';

import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

const PERSON_TEAM_SHIFT = [
  'user',
  'user_id',
  'usuario',
  'person',
  'operator',
  'operario',
  'worker',
  'employee',
  'team',
  'team_id',
  'equipo',
  'crew',
  'cuadrilla',
  'shift',
  'shift_name',
  'turno',
  'jornada',
] as const;

/** Variantes que no deben escapar: mayúsculas, separadores y marcas invisibles. */
const forbiddenName: fc.Arbitrary<string> = fc
  .tuple(
    fc.constantFrom(...PERSON_TEAM_SHIFT),
    fc.constantFrom(
      (s: string) => s,
      (s: string) => s.toUpperCase(),
      (s: string) => `${s.slice(0, 1)}\u200b${s.slice(1)}`,
      (s: string) => `${s.slice(0, 1)}\u00ad${s.slice(1)}`,
      (s: string) => s.replace('_', '-'),
      (s: string) => `zone_${s}`,
      (s: string) => s.replace(/./g, (c) => String.fromCharCode(c.charCodeAt(0) + 0xfee0)),
    ),
  )
  .map(([name, variant]) => variant(name));

const zone = fc.constantFrom('Línea 1', 'Muelle de carga', 'Prensa 3');
const period = fc.constantFrom('2026-W40', '2026-W41', '2026-10');
const value = fc.double({ min: 0, max: 1e6, noNaN: true });

/** Series válidas de la lista cerrada. */
const validSeries: fc.Arbitrary<Record<string, unknown>> = fc.oneof(
  fc.record({
    visualization: fc.constant('bars_by_zone_and_period'),
    dimensions: fc.constantFrom(['zone', 'period'], ['period', 'zone']),
    points: fc.array(fc.record({ zone, period, value }), { maxLength: 12 }),
  }),
  fc.record({
    visualization: fc.constant('hour_of_day_distribution'),
    dimensions: fc.constant(['hour_of_day']),
    points: fc.array(fc.record({ hour_of_day: fc.integer({ min: 0, max: 23 }), value }), {
      maxLength: 24,
    }),
  }),
  fc.subarray([...DIMENSIONS], { minLength: 1 }).chain((dimensions) =>
    fc.record({
      visualization: fc.constant('table'),
      dimensions: fc.constant(dimensions),
      points: fc.array(
        fc
          .record({ zone, period, hour_of_day: fc.integer({ min: 0, max: 23 }), value })
          .map((point) =>
            Object.fromEntries(
              Object.entries(point).filter(
                ([key]) => key === 'value' || dimensions.includes(key as never),
              ),
            ),
          ),
        { maxLength: 8 },
      ),
    }),
  ),
);

/** Este archivo es `.ts` (nombre del Test Plan): el componente se crea sin JSX. */
function chart(series: unknown) {
  return createElement(MetricsChart, { title: 'Métrica', valueLabel: 'Hallazgos', series });
}

type Injection = 'dimension' | 'point' | 'root';

/** Inyecta una dimensión de persona, equipo o turno en la lista, en un punto o en la raíz. */
function inject(series: Record<string, unknown>, name: string, where: Injection) {
  const dimensions = series.dimensions as string[];
  const points = series.points as Record<string, unknown>[];
  if (where === 'dimension') return { ...series, dimensions: [...dimensions, name] };
  if (where === 'root') return { ...series, [name]: ['Equipo A'] };
  const first = points[0] ?? { value: 1 };
  return { ...series, points: [{ ...first, [name]: 'Turno de noche' }, ...points.slice(1)] };
}

describe('PR-APP-09 lista cerrada de visualizaciones', () => {
  it(
    'toda serie aceptada pertenece a la lista cerrada y sus puntos solo llevan zona, periodo y hora',
    () => {
      fc.assert(
        fc.property(validSeries, (series) => {
          const parsed = parseMetricsSeries(series);
          expect(VISUALIZATIONS).toContain(parsed.visualization);
          for (const dimension of parsed.dimensions) expect(DIMENSIONS).toContain(dimension);
          for (const point of parsed.points) {
            for (const key of Object.keys(point)) {
              expect(['value', ...parsed.dimensions]).toContain(key);
            }
          }
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'una dimensión de persona, equipo o turno inyectada se rechaza siempre',
    () => {
      fc.assert(
        fc.property(
          validSeries,
          forbiddenName,
          fc.constantFrom<Injection>('dimension', 'point', 'root'),
          (series, name, where) => {
            const injected = inject(series, name, where);
            let error: unknown = null;
            try {
              parseMetricsSeries(injected);
            } catch (caught) {
              error = caught;
            }
            expect(error).toBeInstanceOf(MetricsContractError);
            expect((error as MetricsContractError).code).toBe('forbidden_dimension');
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'cualquier otra dimensión o visualización fuera de la lista también se rechaza',
    () => {
      const unknownVisualization = fc
        .string({ minLength: 1, maxLength: 20 })
        .filter((name) => !(VISUALIZATIONS as readonly string[]).includes(name));
      fc.assert(
        fc.property(
          validSeries,
          fc.oneof(
            unknownVisualization,
            fc.constantFrom('heatmap', 'heat_map', 'cycle_time', 'line_by_user', 'pie'),
          ),
          (series, visualization) => {
            expect(() => parseMetricsSeries({ ...series, visualization })).toThrow(
              MetricsContractError,
            );
          },
        ),
      );
      fc.assert(
        fc.property(
          validSeries,
          fc.constantFrom('machine', 'cycle_time_ms', 'camera_id', 'x', 'line'),
          (series, name) => {
            expect(() => parseMetricsSeries(inject(series, name, 'dimension'))).toThrow(
              MetricsContractError,
            );
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );

  it(
    'el componente dibuja solo series de la lista y lanza error de contrato con una por turno',
    () => {
      fc.assert(
        fc.property(validSeries, forbiddenName, (series, name) => {
          const { container, unmount } = render(chart(series));
          const shown = container.querySelector('figure')?.getAttribute('data-visualization');
          expect(VISUALIZATIONS).toContain(shown);
          unmount();
          expect(() => render(chart(inject(series, name, 'dimension')))).toThrow(
            MetricsContractError,
          );
        }),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
