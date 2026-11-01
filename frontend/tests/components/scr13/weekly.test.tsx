/** El resumen usa las mismas cifras y no convierte el programa en evaluación de personas. */
import { fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { fromZonedTime } from 'date-fns-tz';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr13/weekly';
import { text } from '@/texts';
import { apiError } from '../../msw/handlers';
import { server } from '../../msw/server';
import {
  API,
  OTHER,
  PLANT,
  WAIT,
  ZONE,
  fixture,
  consult,
  overdueData,
  adoptionData,
  snapshot,
} from './support';

describe('SCR-13 resumen semanal', { timeout: 30_000 }, () => {
  it('mismas cifras, vencidos 30/31 días, adopción del servidor y objetivo propio', async () => {
    const f = await fixture();
    const view = render(<Page />, { wrapper: f.Wrapper });
    await consult();
    const table = await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.querySelector('strong')?.textContent).toBe(text('scr13.highlighted'));
    expect(rows[1]?.querySelector('strong')?.textContent).toBe(text('scr13.highlighted'));
    expect(
      screen.getByText('Umbral de destacado del servidor: 30 días', { exact: true }),
    ).toBeDefined();
    expect(screen.queryByRole('alert')).toBeNull();
    expect(screen.getByText(adoptionData().usage_notice_es, { exact: true })).toBeDefined();
    expect(screen.getByText(text('scr13.goal'), { exact: true })).toBeDefined();
    expect(screen.getByText(text('scr13.monthly_notice'), { exact: true })).toBeDefined();
    expect(
      screen.getAllByText(text('scr13.not_computable'), { exact: true }).length,
    ).toBeGreaterThan(0);
    // VIG-365: el resumen abre con los 4 indicadores del servidor, en su orden, sin recalcular.
    expect(
      [...view.container.querySelectorAll('[data-indicator]')].map((node) =>
        node.getAttribute('data-indicator'),
      ),
    ).toEqual(['exposure', 'classified_in_time', 'primary_engineering_closures', 'overdue']);
    expect(
      within(screen.getByRole('table', { name: text('scr13.activity_count') })).getByRole('cell', {
        name: '9',
      }),
    ).toBeDefined();
    expect(await axe(view.container)).toHaveNoViolations();
  });
  it('VIG-363: el inicio del gerente llega cargado con la primera zona y el mes de la semana anterior', async () => {
    const f = await fixture();
    // 3 de febrero: la semana anterior empieza el 27 de enero, así que el periodo es enero.
    f.clock.advance(19 * 86_400_000);
    const requests: URL[] = [];
    server.events.on('request:start', ({ request }) => {
      requests.push(new URL(request.url));
    });
    render(<Page />, { wrapper: f.Wrapper });
    const table = await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    expect(within(table).getAllByRole('row').slice(1)).toHaveLength(2);
    expect(screen.queryByText(text('scr13.not_requested'), { exact: true })).toBeNull();
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.zone')).value).toBe(ZONE);
    expect(screen.getByLabelText<HTMLInputElement>(text('scr13.period')).value).toBe('2026-01');
    const metrics = requests.filter((url) => url.pathname === '/accreditation/metrics');
    expect(metrics.map((url) => url.searchParams.get('period'))).toEqual(['2026-01']);
    expect(metrics.map((url) => url.searchParams.get('zone_id'))).toEqual([ZONE]);
    server.events.removeAllListeners();
  });
  // VIG-369: el nightly del 01-11-2026 (domingo) cargaba un mes que el servidor rechazaba.
  const BOGOTA = 'America/Bogota';
  const NIGHTLY = fromZonedTime('2026-11-01T00:30:00', BOGOTA).getTime();
  function monthlyServer(organizationId: string, rejected: readonly string[] = []) {
    const periods: (string | null)[] = [];
    server.use(
      http.get(`${API}/accreditation/metrics`, ({ request }) => {
        const period = new URL(request.url).searchParams.get('period') ?? '';
        periods.push(period);
        // Como el servidor: un mes que terminó antes de crear la zona es invalid_request.
        if (rejected.includes(period)) return apiError('invalid_request', 400);
        return HttpResponse.json({
          plant_id: PLANT,
          period,
          zones: [{ ...snapshot(organizationId), period }],
        });
      }),
    );
    return periods;
  }
  const indicators = (container: HTMLElement) => container.querySelectorAll('[data-indicator]');
  it.each([
    ['01-11 a las 00:30 de la planta', NIGHTLY],
    // 21:00 del 07-11 en Bogotá ya es 08-11 en UTC: la semana anterior empieza el 31-10.
    ['07-11 a las 21:00 de la planta', Date.parse('2026-11-08T02:00:00.000Z')],
  ])(
    'VIG-369: %s carga octubre, el mes de la semana anterior en la hora de la planta',
    async (_, now) => {
      const f = await fixture(undefined, { timeZone: BOGOTA, now });
      const periods = monthlyServer(f.organizationId);
      const view = render(<Page />, { wrapper: f.Wrapper });
      await waitFor(() => {
        expect(indicators(view.container)).toHaveLength(4);
      }, WAIT);
      expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.period')).value).toBe('2026-10');
      expect(periods).toEqual(['2026-10']);
    },
  );
  it('VIG-369: si la zona no existía ese mes, la carga inicial cae al mes en curso de la planta', async () => {
    const f = await fixture(undefined, { timeZone: BOGOTA, now: NIGHTLY });
    const periods = monthlyServer(f.organizationId, ['2026-10']);
    const view = render(<Page />, { wrapper: f.Wrapper });
    await waitFor(() => {
      expect(indicators(view.container)).toHaveLength(4);
    }, WAIT);
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.period')).value).toBe('2026-11');
    expect(periods).toEqual(['2026-10', '2026-11']);
    expect(screen.queryByText(text('scr13.not_requested'), { exact: true })).toBeNull();
  });
  it('VIG-369: un mes que el gerente elige no se sustituye aunque el servidor lo rechace', async () => {
    const f = await fixture(undefined, { timeZone: BOGOTA, now: NIGHTLY });
    const periods = monthlyServer(f.organizationId, ['2026-10']);
    const view = render(<Page />, { wrapper: f.Wrapper });
    await waitFor(() => {
      expect(indicators(view.container)).toHaveLength(4);
    }, WAIT);
    fireEvent.change(screen.getByLabelText(text('scr13.period')), {
      target: { value: '2026-10' },
    });
    fireEvent.click(screen.getByRole('button', { name: text('scr13.consult') }));
    await screen.findAllByRole('alert', {}, WAIT);
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.period')).value).toBe('2026-10');
    expect(periods).toEqual(['2026-10', '2026-11', '2026-10']);
    expect(indicators(view.container)).toHaveLength(0);
  });
  it('VIG-363: una elección del gerente sustituye a la carga inicial', async () => {
    const f = await fixture();
    render(<Page />, { wrapper: f.Wrapper });
    await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    fireEvent.change(screen.getByLabelText(text('scr13.zone')), { target: { value: OTHER } });
    expect(screen.getByText(text('scr13.not_requested'), { exact: true })).toBeDefined();
    expect(screen.getByLabelText<HTMLSelectElement>(text('scr13.zone')).value).toBe(OTHER);
  });
  it('VIG-365: las 3 tarjetas del gerente (integridad, proveedores, paquete) solo con su permiso', async () => {
    const base = ['metrics.read', 'exposure.read', 'executive_summary.read', 'coverage.read'];
    const cards = (container: HTMLElement) =>
      [...container.querySelectorAll('[data-slot]')].map((card) => [
        card.getAttribute('data-slot'),
        card.querySelector('a')?.getAttribute('href'),
      ]);
    const full = await fixture([
      ...base,
      'integrity.verify',
      'concessions.read',
      'concessions.revoke',
      'review_package.read',
    ]);
    const view = render(<Page />, { wrapper: full.Wrapper });
    await screen.findByRole('region', { name: text('scr13.home.cards') }, WAIT);
    expect(cards(view.container)).toEqual([
      ['SCR-03:integrity', '/integrity'],
      ['SCR-02:*', '/concessions'],
      ['SCR-12:*', '/zones/monthly'],
    ]);
    expect(await axe(view.container)).toHaveNoViolations();
    view.unmount();
    const bare = await fixture(base);
    const other = render(<Page />, { wrapper: bare.Wrapper });
    await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    expect(other.container.querySelector('[data-slot]')).toBeNull();
  });
  it('respeta destacado y totales del servidor con umbral configurable, sin recalcular por días ni página', async () => {
    const f = await fixture(['executive_summary.read']);
    const data = overdueData();
    data.highlight_threshold_days = 15;
    data.zones = data.zones.map((zone) => ({ ...zone, overdue_count: 9, highlighted_count: 7 }));
    data.items = data.items.map((item, index) => ({
      ...item,
      overdue_days: index === 0 ? 20 : 31,
      highlighted: index === 0,
    }));
    server.use(http.get(`${API}/loop/overdue`, () => HttpResponse.json(data)));
    render(<Page />, { wrapper: f.Wrapper });
    await consult();
    const table = await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows[0]?.querySelector('strong')?.textContent).toBe(text('scr13.highlighted'));
    expect(rows[1]?.querySelector('strong')).toBeNull();
    expect(
      screen.getByText('Umbral de destacado del servidor: 15 días', { exact: true }),
    ).toBeDefined();
    const zone = screen.getByRole('region', { name: 'Vencidos: Zona sintética 1' });
    expect(
      within(zone).getByText(text('scr13.highlighted_count'), { exact: true }).nextElementSibling
        ?.textContent,
    ).toBe('7');
    expect(
      within(zone).getByText(text('scr13.overdue_count'), { exact: true }).nextElementSibling
        ?.textContent,
    ).toBe('9');
  });
  it.each(['projection_rebuilding', 'projection_diverged'] as const)(
    'declara %s junto a los vencidos, incluso sin filas',
    async (notice) => {
      const f = await fixture(['executive_summary.read']);
      server.use(
        http.get(`${API}/loop/overdue`, () =>
          HttpResponse.json({
            ...overdueData(),
            items: [],
            partition_notice: notice,
          }),
        ),
      );
      render(<Page />, { wrapper: f.Wrapper });
      await consult();
      const overdue = await screen.findByRole('region', { name: text('scr13.overdue') }, WAIT);
      expect(within(overdue).getByRole('alert').textContent).toBe(text(`scr13.${notice}`));
      expect(
        within(overdue).getByRole('table', { name: text('scr13.overdue_days') }),
      ).toBeDefined();
    },
  );
  it('executive_summary.read no concede por sí mismo métricas ni exposición', async () => {
    const f = await fixture(['executive_summary.read']);
    const view = render(<Page />, { wrapper: f.Wrapper });
    await consult();
    await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    expect(screen.queryByRole('heading', { name: text('scr13.activation') })).toBeNull();
    expect(view.container.querySelector('[data-section="exposure"]')).toBeNull();
  });
  it.each(['plant', 'zone', 'item_plant', 'item_zone'] as const)(
    'rechaza vencidos con otro %s',
    async (field) => {
      const f = await fixture(['executive_summary.read']);
      const data = overdueData();
      if (field === 'plant') data.plant_id = OTHER;
      if (field === 'zone') data.zones = data.zones.map((zone) => ({ ...zone, zone_id: OTHER }));
      if (field === 'item_plant')
        data.items = data.items.map((item) => ({ ...item, plant_id: OTHER }));
      if (field === 'item_zone')
        data.items = data.items.map((item) => ({ ...item, zone_id: OTHER }));
      server.use(http.get(`${API}/loop/overdue`, () => HttpResponse.json(data)));
      render(<Page />, { wrapper: f.Wrapper });
      await consult();
      await screen.findByRole('alert', {}, WAIT);
      expect(screen.queryByRole('table', { name: text('scr13.overdue_days') })).toBeNull();
    },
  );
  it('cargar más conserva las filas anteriores y usa el cursor opaco', async () => {
    const f = await fixture(['executive_summary.read']);
    const requests: URL[] = [];
    server.use(
      http.get(`${API}/loop/overdue`, ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        const data = overdueData();
        data.items = url.searchParams.has('cursor') ? data.items.slice(1) : data.items.slice(0, 1);
        data.next_cursor = url.searchParams.has('cursor') ? null : 'cursor-sintetico';
        return HttpResponse.json(data);
      }),
    );
    render(<Page />, { wrapper: f.Wrapper });
    await consult();
    const more = await screen.findByRole('button', { name: text('scr13.next') }, WAIT);
    fireEvent.click(more);
    await waitFor(() => {
      expect(screen.queryByRole('button', { name: text('scr13.next') })).toBeNull();
      expect(
        screen
          .getByRole('table', { name: text('scr13.overdue_days') })
          .querySelectorAll('tbody tr'),
      ).toHaveLength(2);
    }, WAIT);
    expect(requests[1]?.searchParams.get('cursor')).toBe('cursor-sintetico');
    // Un filtro de mes en due_at ocultaría justo los vencidos de más de 30 días.
    expect(requests[0]?.searchParams.has('from')).toBe(false);
    expect(requests[0]?.searchParams.has('to')).toBe(false);
  });
});
