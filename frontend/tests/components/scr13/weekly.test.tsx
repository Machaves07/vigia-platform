/** El resumen usa las mismas cifras y no convierte el programa en evaluación de personas. */
import { fireEvent, render, screen, within, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr13/weekly';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { API, OTHER, WAIT, fixture, consult, overdueData } from './support';

describe('SCR-13 resumen semanal', { timeout: 30_000 }, () => {
  it('mismas cifras, vencidos 30/31 días, adopción del servidor y objetivo propio', async () => {
    const f = await fixture();
    const view = render(<Page />, { wrapper: f.Wrapper });
    await consult();
    const table = await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows).toHaveLength(2);
    expect(rows[0]?.querySelector('strong')).toBeNull();
    expect(rows[1]?.querySelector('strong')?.textContent).toBe(text('scr13.over_30'));
    expect(screen.getByText(text('scr13.goal'), { exact: true })).toBeDefined();
    expect(screen.getByText(text('scr13.monthly_notice'), { exact: true })).toBeDefined();
    expect(screen.getByText(text('scr13.not_computable'), { exact: true })).toBeDefined();
    expect(
      within(screen.getByRole('table', { name: text('scr13.activity_count') })).getByRole('cell', {
        name: '9',
      }),
    ).toBeDefined();
    expect(await axe(view.container)).toHaveNoViolations();
  });
  it('executive_summary.read no concede por sí mismo métricas ni exposición', async () => {
    const f = await fixture(['executive_summary.read']);
    const view = render(<Page />, { wrapper: f.Wrapper });
    await consult();
    await screen.findByRole('table', { name: text('scr13.overdue_days') }, WAIT);
    expect(screen.queryByRole('heading', { name: text('scr13.activation') })).toBeNull();
    expect(view.container.querySelector('dd')).toBeNull();
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
