/** RV-17 / PR-APP-09: la vista real no convierte un estado incalculable en cifra. */
import fc from 'fast-check';
import { createElement } from 'react';
import { render } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { SeriesView } from '@/pages/scr13/Views';
import { MetricsContractError } from '@/components/MetricsChart/metrics';
import type { MetricSnapshot } from '@/ports/scr13Metrics';
import { text } from '@/texts';
import { readView } from '../generators';
import registered from '../views/scr13';
import { PROPERTY_TIMEOUT_MS } from '../support/timeouts';

describe('SCR-13 RV-17', () => {
  it(
    'not_computable no tiene cifra aunque el servidor envíe un número',
    () => {
      fc.assert(
        fc.property(
          readView('ProgramMetricSnapshot'),
          fc.double({ noNaN: true, noDefaultInfinity: true }),
          (raw, value) => {
            const data = raw as unknown as MetricSnapshot;
            const view = render(
              registered.programMetrics.render(
                {
                  ...data,
                  period: '2026-01',
                  values: [
                    {
                      key: 'activation',
                      status: 'not_computable',
                      value,
                      unit: 'count',
                      excluded_unclosable_count: 0,
                    },
                  ],
                },
                {
                  organizationId: data.organization_id,
                  concessionOrganizationId: null,
                  reportContractError: () => undefined,
                },
              ),
            );
            try {
              expect(view.getByText(text('scr13.not_computable')).textContent).toBe(
                text('scr13.not_computable'),
              );
              expect(view.container.querySelector('[data-visualization]')).toBeNull();
            } finally {
              view.unmount();
            }
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
  it(
    'rechaza dimensión personal en la misma composición usada por RV-17',
    () => {
      fc.assert(
        fc.property(
          fc.constantFrom('user', 'team', 'shift', 'usuario', 'equipo', 'turno'),
          fc.string(),
          (dimension, label) => {
            expect(() =>
              render(
                createElement(SeriesView, {
                  series: {
                    visualization: 'table',
                    dimensions: ['zone', dimension],
                    points: [{ zone: 'Zona sintética', [dimension]: label, value: 1 }],
                  },
                }),
              ),
            ).toThrow(MetricsContractError);
          },
        ),
      );
    },
    PROPERTY_TIMEOUT_MS,
  );
});
