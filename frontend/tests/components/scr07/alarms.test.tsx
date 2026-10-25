import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Alarms from '@/pages/scr07/alarms';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { fleetFixture, INSTANT, node, PLANT } from './support';

const WAIT = { timeout: 10_000 };
describe('SCR-07 alarmas', { timeout: 30_000 }, () => {
  it('muestra transición activa y finalizada, pagina y mantiene los filtros aplicados', async () => {
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/fleet/alarms', ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          alarms: [null, INSTANT].map((cleared_at, index) => ({
            alarm_id: `ALARMA-${String(index)}`,
            alarm_kind: 'node_mute',
            plant_id: PLANT,
            node_id: node().node_id,
            zone_id: null,
            raised_at: INSTANT,
            cleared_at,
          })),
          next_after: url.searchParams.has('after') ? null : 'alarms-next',
        });
      }),
    );
    const { Wrapper } = await fleetFixture('/fleet/alarms');
    const { container } = render(<Alarms />, { wrapper: Wrapper });
    await screen.findByRole('table', { name: text('scr07.alarms') }, WAIT);
    expect(screen.getAllByRole('row')).toHaveLength(3);
    expect(screen.getAllByText(text('scr07.alarm_warning.node_mute'))).toHaveLength(3);
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.change(screen.getByRole('combobox', { name: text('scr07.alarm_status') }), {
      target: { value: 'cleared' },
    });
    expect(requests).toHaveLength(1);
    fireEvent.click(screen.getByRole('button', { name: text('scr07.apply') }));
    await waitFor(() => {
      expect(requests).toHaveLength(2);
    }, WAIT);
    expect(requests[1]?.searchParams.get('status')).toBe('cleared');
    await screen.findByRole('button', { name: text('scr07.load_more') }, WAIT);
    fireEvent.click(screen.getByRole('button', { name: text('scr07.load_more') }));
    await waitFor(() => {
      expect(requests).toHaveLength(3);
    }, WAIT);
    expect(requests[2]?.searchParams.get('after')).toBe('alarms-next');
    expect(requests[2]?.searchParams.get('status')).toBe('cleared');
  });

  it('estado vacío con actualizado hace X', async () => {
    server.use(
      http.get('http://localhost/fleet/alarms', () =>
        HttpResponse.json({ alarms: [], next_after: null }),
      ),
    );
    const { Wrapper } = await fleetFixture();
    const { container } = render(<Alarms />, { wrapper: Wrapper });
    await screen.findByText(text('scr07.alarms_empty'), {}, WAIT);
    expect(container.querySelector('[data-part="updated"]')).not.toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });
});
