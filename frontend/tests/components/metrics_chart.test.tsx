/**
 * UI-23 `MetricsChart` (BR-APP-28, 80): una gráfica por zona con la misma escala, distribución
 * por hora de 00 a 23 sin nombre de turno, tabla con las cabeceras del catálogo y `axe` sin
 * violaciones serias. Se comparan nombres accesibles y textos exactos.
 */
import { render, screen } from '@testing-library/react';
import type { ReactElement } from 'react';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { MetricsChart } from '@/components/MetricsChart/MetricsChart';
import { MetricsContractError } from '@/components/MetricsChart/metrics';
import catalog from '@/texts/components_structure.json';

const bars = {
  visualization: 'bars_by_zone_and_period',
  dimensions: ['zone', 'period'],
  points: [
    { zone: 'Línea 1', period: '2026-W40', value: 3 },
    { zone: 'Línea 1', period: '2026-W41', value: 5 },
    { zone: 'Prensa 3', period: '2026-W40', value: 1 },
  ],
};
const hours = {
  visualization: 'hour_of_day_distribution',
  dimensions: ['hour_of_day'],
  points: [
    { hour_of_day: 22, value: 2 },
    { hour_of_day: 3, value: 4 },
  ],
};
const table = {
  visualization: 'table',
  dimensions: ['period', 'zone'],
  points: [{ zone: 'Línea 1', period: '2026-10', value: 0.75 }],
};

async function expectNoSeriousViolations(ui: ReactElement) {
  const { container } = render(<main>{ui}</main>);
  const results = await axe(container);
  const serious = results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
  expect(serious).toEqual([]);
  expect(results).toHaveNoViolations();
}

describe('UI-23 MetricsChart', () => {
  it.each([
    ['barras', bars],
    ['horas', hours],
    ['tabla', table],
  ])('axe sin violaciones: %s', async (_name, series) => {
    await expectNoSeriousViolations(
      <MetricsChart title="Hallazgos" valueLabel="Hallazgos" series={series} />,
    );
  });

  it('una gráfica por zona, nombrada por la zona', () => {
    render(<MetricsChart title="Hallazgos" valueLabel="Hallazgos" series={bars} />);
    expect(screen.getAllByRole('application')).toHaveLength(2);
    expect(screen.getByRole('application', { name: 'Hallazgos: Línea 1' })).toBeTruthy();
    expect(screen.getByRole('application', { name: 'Hallazgos: Prensa 3' })).toBeTruthy();
  });

  it('la distribución por hora usa horas 00 a 23 y ningún nombre de turno', () => {
    const { container } = render(
      <MetricsChart title="Por hora" valueLabel="Hallazgos" series={hours} />,
    );
    const ticks = [
      ...container.querySelectorAll(
        '.recharts-xAxis-tick-labels .recharts-cartesian-axis-tick-value',
      ),
    ].map((tick) => tick.textContent);
    expect(ticks).toEqual(['03', '22']);
  });

  it('la tabla lleva las cabeceras del catálogo en el orden de la lista cerrada', () => {
    render(<MetricsChart title="Tabla" valueLabel="En plazo" series={table} />);
    expect(screen.getAllByRole('columnheader').map((header) => header.textContent)).toEqual([
      catalog['components_structure.metrics.zone'],
      catalog['components_structure.metrics.period'],
      'En plazo',
    ]);
    expect(screen.getAllByRole('cell').map((cell) => cell.textContent)).toEqual([
      'Línea 1',
      '2026-10',
      '0,75',
    ]);
  });

  it('sin puntos muestra el texto vacío del catálogo', () => {
    const { container } = render(
      <MetricsChart title="Vacío" valueLabel="Hallazgos" series={{ ...bars, points: [] }} />,
    );
    expect(container.querySelector('[data-part="empty"]')?.textContent).toBe(
      catalog['components_structure.metrics.empty'],
    );
  });

  it('una serie con mapa de calor o por turno es un error de contrato', () => {
    expect(() =>
      render(
        <MetricsChart title="x" valueLabel="x" series={{ ...hours, visualization: 'heatmap' }} />,
      ),
    ).toThrow(MetricsContractError);
    expect(() =>
      render(
        <MetricsChart
          title="x"
          valueLabel="x"
          series={{ ...bars, dimensions: ['zone', 'period', 'shift'] }}
        />,
      ),
    ).toThrow(MetricsContractError);
  });
});
