/** RV-17: estados honestos, filtros cerrados, consulta real y accesibilidad. */
import { fireEvent, render, screen, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr13/metrics';
import { MetricView, SeriesView } from '@/pages/scr13/Views';
import { MetricsContractError } from '@/components/MetricsChart/metrics';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { API, OTHER, PLANT, WAIT, ZONE, fixture, snapshot, consult, adoptionData } from './support';
describe('SCR-13 métricas', { timeout: 30_000 }, () => {
  it('VIG-363: las métricas del programa no se cargan solas; solo el inicio del gerente', async () => {
    const f = await fixture();
    const requests: string[] = [];
    server.events.on('request:start', ({ request }) => {
      requests.push(new URL(request.url).pathname);
    });
    render(<Page />, { wrapper: f.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
    expect(screen.getByText(text('scr13.not_requested'), { exact: true })).toBeDefined();
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.zone')).value).toBe('');
    expect(requests).toEqual(['/hierarchy']);
    server.events.removeAllListeners();
  });
  it('periodo vacío permitido por el espejo no provoca una gráfica inválida', () => {
    const data = { ...snapshot('synthetic'), period: '' };
    render(<MetricView data={data} zoneName="Zona sintética" organizationId="synthetic" />);
    expect(screen.getByRole('alert')).toBeDefined();
  });
  it('not_computable nunca se convierte en cero ni en una barra', () => {
    const data = snapshot('synthetic');
    data.values = data.values.map((metric) => ({ ...metric, value: 0 }));
    const view = render(
      <MetricView data={data} zoneName="Zona sintética" organizationId="synthetic" />,
    );
    expect(screen.getByText(text('scr13.not_computable')).textContent).toBe(
      text('scr13.not_computable'),
    );
    expect(view.container.querySelector('[data-visualization]')).toBeNull();
    expect(screen.queryByText('0', { exact: true })).toBeNull();
  });
  it.each(['not_computable', 'partial'] as const)(
    'muestra el motivo de métrica %s del servidor',
    (status) => {
      const data = snapshot('synthetic');
      data.values = data.values.map((metric) => ({
        ...metric,
        status,
        value: status === 'partial' ? 0.5 : null,
        not_computable_reason_es: status === 'not_computable' ? 'Sin denominador declarado.' : null,
        partial_reason_es: status === 'partial' ? 'Periodo incompleto.' : null,
      }));
      render(<MetricView data={data} zoneName="Zona sintética" organizationId="synthetic" />);
      expect(
        screen.getByText(
          status === 'not_computable' ? 'Sin denominador declarado.' : 'Periodo incompleto.',
          { exact: true },
        ),
      ).toBeDefined();
    },
  );
  it.each(['user', 'team', 'shift', 'S\u200bhift', 'usuario', 'equipo', 'turno'])(
    'rechaza serie con dimensión %s',
    (dimension) => {
      expect(() =>
        render(
          <SeriesView
            series={{
              visualization: 'table',
              dimensions: ['zone', dimension],
              points: [{ zone: 'Zona sintética', [dimension]: 'No permitido', value: 5 }],
            }}
          />,
        ),
      ).toThrow(MetricsContractError);
    },
  );
  it('solo ofrece zona y periodo, consulta por botón, muestra frescura y pasa axe', async () => {
    const f = await fixture();
    const requests: URL[] = [];
    server.use(
      http.get(`${API}/accreditation/metrics`, ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({
          plant_id: PLANT,
          period: '2026-01',
          zones: [snapshot(f.organizationId)],
        });
      }),
    );
    const view = render(<Page />, { wrapper: f.Wrapper });
    await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
    expect(requests).toHaveLength(0);
    const form = screen.getByRole('button', { name: text('scr13.consult') }).closest('form');
    if (form === null) throw new Error('Falta el formulario.');
    // Zona y periodo; el periodo es una lista de meses en español (VIG-366).
    expect([...form.querySelectorAll('input,select')].map((element) => element.tagName)).toEqual([
      'SELECT',
      'SELECT',
    ]);
    expect(within(form).getAllByRole('combobox')).toEqual([
      screen.getByLabelText(text('scr13.zone')),
      screen.getByLabelText(text('scr13.period')),
    ]);
    await consult();
    await screen.findByText(text('scr13.not_computable'), {}, WAIT);
    expect(requests).toHaveLength(1);
    expect(Object.fromEntries(requests[0]?.searchParams ?? [])).toEqual({
      zone_id: ZONE,
      period: '2026-01',
    });
    expect(view.container.querySelectorAll('[data-visualization]')).toHaveLength(0);
    expect(await axe(view.container)).toHaveNoViolations();
    fireEvent.change(screen.getByLabelText(text('scr13.zone')), { target: { value: OTHER } });
    expect(screen.queryByText(text('scr13.not_computable'))).toBeNull();
    expect(requests).toHaveLength(1);
  });
  it.each(['organization', 'plant', 'zone', 'period'] as const)(
    'rechaza métricas de otro %s',
    async (field) => {
      const f = await fixture(['metrics.read']);
      const data = snapshot(f.organizationId);
      if (field === 'organization') data.organization_id = OTHER;
      if (field === 'plant') data.plant_id = OTHER;
      if (field === 'zone') data.zone_id = OTHER;
      if (field === 'period') data.period = '2025-12';
      server.use(
        http.get(`${API}/accreditation/metrics`, () =>
          HttpResponse.json({ plant_id: PLANT, period: '2026-01', zones: [data] }),
        ),
      );
      render(<Page />, { wrapper: f.Wrapper });
      await consult();
      await screen.findByRole('alert', {}, WAIT);
      expect(screen.queryByText(text('scr13.not_computable'))).toBeNull();
    },
  );
  it.each(['plant', 'zone'] as const)('rechaza adopción de otra %s', async (field) => {
    const f = await fixture(['metrics.read']);
    const data = adoptionData();
    if (field === 'plant') data.plant_id = OTHER;
    if (field === 'zone') data.zones = data.zones.map((zone) => ({ ...zone, zone_id: OTHER }));
    server.use(http.get(`${API}/accreditation/adoption`, () => HttpResponse.json(data)));
    render(<Page />, { wrapper: f.Wrapper });
    await consult();
    await screen.findByRole('alert', {}, WAIT);
    expect(screen.queryByRole('table', { name: text('scr13.activity_count') })).toBeNull();
  });
});
