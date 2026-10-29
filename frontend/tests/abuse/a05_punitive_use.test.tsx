import { cleanup, fireEvent, render, screen, within } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { expect, it } from 'vitest';
import Metrics from '@/pages/scr13/metrics';
import { MetricView } from '@/pages/scr13/Views';
import type { ExportRequest } from '@/ports/scr14Exports';
import { text } from '@/texts';
import { fixture as metricsFixture, snapshot, WAIT } from '../components/scr13/support';
import {
  API,
  fillRequest,
  fixture as exportsFixture,
  job,
  PLANT,
} from '../components/scr14/support';
import { server } from '../msw/server';

it('A-5 · BR-APP-21, 22, 28, 33, 59: filtros cerrados sin persona y exportación solo por planta, zonas y periodo', async () => {
  const metrics = await metricsFixture();
  const view = render(<Metrics />, { wrapper: metrics.Wrapper });
  await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
  const filters = screen.getByRole('button', { name: text('scr13.consult') }).closest('form');
  if (filters === null) throw new Error('Sin formulario de filtros');
  // Lista positiva: cualquier filtro añadido (aunque se llame distinto de «persona») falla.
  expect(
    [...filters.querySelectorAll('input, select, textarea')].map(
      (e) => e.getAttribute('type') ?? e.tagName,
    ),
  ).toEqual(['SELECT', 'SELECT']);
  expect(within(filters).getByLabelText(text('scr13.zone'))).toBeDefined();
  expect(within(filters).getByLabelText(text('scr13.period'))).toBeDefined();
  expect(view.container.querySelector('[data-person-count], [data-tracking-id]')).toBeNull();
  const hostile = {
    ...snapshot(metrics.organizationId),
    person_name: 'PERSONA-OBSERVADA-SINTETICA',
    person_count: 987654321,
    tracking_id: 'SEGUIMIENTO-SINTETICO',
  };
  const displayed = render(
    <MetricView data={hostile} zoneName="Zona sintética" organizationId={metrics.organizationId} />,
  );
  expect(displayed.container.textContent).not.toMatch(
    /PERSONA-OBSERVADA-SINTETICA|987654321|SEGUIMIENTO-SINTETICO/,
  );
  cleanup();
  await exportsFixture();
  const bodies: ExportRequest[] = [];
  server.use(
    http.post(`${API}/accreditation/exports`, async ({ request }) => {
      bodies.push((await request.json()) as ExportRequest);
      return HttpResponse.json(job('requested'), { status: 202 });
    }),
  );
  fillRequest();
  fireEvent.click(screen.getByRole('button', { name: text('scr14.request') }));
  await screen.findByRole('region', { name: text('scr14.manifest') }, WAIT);
  expect(bodies).toHaveLength(1);
  expect(bodies[0]).toEqual({
    plant_id: PLANT,
    zone_ids: [job().zone_ids[0]],
    period: job().period,
    package_form: 'without_clips',
  });
}, 30_000);
