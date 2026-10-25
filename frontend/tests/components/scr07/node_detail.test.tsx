import { fireEvent, render, screen, waitFor } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import { NodeDetail } from '@/pages/scr07/inventory/NodeDetail';
import { Thresholds } from '@/pages/scr07/inventory/Thresholds';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { apiError } from '../../msw/handlers';
import { fleetFixture, INSTANT, node, PLANT } from './support';

const WAIT = { timeout: 10_000 };
describe('SCR-07 detalle y umbrales', { timeout: 30_000 }, () => {
  it('muestra historia reciente y pagina sin pintar el payload abierto del latido', async () => {
    const cursors: (string | null)[] = [];
    server.use(
      http.get('http://localhost/fleet/nodes/:node_id', ({ request }) => {
        const after = new URL(request.url).searchParams.get('history_after');
        cursors.push(after);
        return HttpResponse.json({
          node: node(),
          heartbeat_history: [
            {
              heartbeat_id: '0190a1b2-0000-7000-8000-000000000002',
              received_at: INSTANT,
              sent_at: INSTANT,
              payload_summary: { person_id: 'IDENTIDAD-NO-RENDERIZABLE' },
            },
          ],
          next_history_after: after === null ? 'history-next' : null,
        });
      }),
    );
    const { Wrapper } = await fleetFixture();
    const { container } = render(<NodeDetail nodeId={node().node_id} />, { wrapper: Wrapper });
    await screen.findByRole('columnheader', { name: text('scr07.received') }, WAIT);
    expect(screen.queryByText('IDENTIDAD-NO-RENDERIZABLE')).toBeNull();
    expect(
      container.querySelectorAll('time[datetime="2026-01-01T12:00:00.000Z"]').length,
    ).toBeGreaterThanOrEqual(2);
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole('button', { name: text('scr07.load_more') }));
    await waitFor(() => {
      expect(cursors).toEqual([null, 'history-next']);
    }, WAIT);
  });

  it('sin latido no inventa datos ni URL local', async () => {
    const value = {
      ...node(),
      last_heartbeat_at: null,
      live_view_local_url: null,
      local_queue: null,
      clock: null,
      software_version: null,
    };
    server.use(
      http.get('http://localhost/fleet/nodes/:node_id', () =>
        HttpResponse.json({ node: value, heartbeat_history: [], next_history_after: null }),
      ),
    );
    const { Wrapper } = await fleetFixture();
    render(<NodeDetail nodeId={value.node_id} />, { wrapper: Wrapper });
    await screen.findByText(text('scr07.history_empty'), {}, WAIT);
    expect(screen.getByText(text('scr07.live_unavailable'))).toBeDefined();
  });

  it('not_found usa la misma pantalla de alcance y no filtra datos del recurso', async () => {
    server.use(http.get('http://localhost/fleet/nodes/:node_id', () => apiError('not_found', 404)));
    const { Wrapper } = await fleetFixture();
    render(<NodeDetail nodeId={node().node_id} />, { wrapper: Wrapper });
    await screen.findByRole('heading', { name: text('errors.not_available_in_scope') }, WAIT);
    expect(screen.queryByText(node().code)).toBeNull();
  });

  it('GET/PUT umbrales envía los tres valores a la planta y muestra recibo solo tras éxito', async () => {
    const initial = {
      queue_pending_threshold: 100,
      queue_age_threshold_minutes: 30,
      clock_drift_threshold_ms: 5000,
    };
    const bodies: unknown[] = [];
    server.use(
      http.get('http://localhost/plants/:plant_id/fleet-thresholds', () =>
        HttpResponse.json({
          ...initial,
          plant_id: PLANT,
          configured: true,
          updated_by: null,
          updated_at: null,
        }),
      ),
      http.put(
        'http://localhost/plants/:plant_id/fleet-thresholds',
        async ({ request, params }) => {
          expect(params.plant_id).toBe(PLANT);
          bodies.push(await request.json());
          return HttpResponse.json({
            ...initial,
            plant_id: PLANT,
            configured: true,
            updated_by: null,
            updated_at: INSTANT,
          });
        },
      ),
    );
    const { Wrapper } = await fleetFixture();
    const { container } = render(<Thresholds plantId={PLANT} />, { wrapper: Wrapper });
    const pending = await screen.findByRole(
      'spinbutton',
      { name: text('scr07.queue_threshold') },
      WAIT,
    );
    for (const input of screen.getAllByRole<HTMLInputElement>('spinbutton')) {
      for (const value of ['1', '2147483647']) {
        fireEvent.change(input, { target: { value } });
        expect(input.checkValidity()).toBe(true);
      }
      for (const value of ['0', '2147483648', '1.5', '']) {
        fireEvent.change(input, { target: { value } });
        expect(input.checkValidity()).toBe(false);
      }
      fireEvent.change(input, {
        target: {
          value:
            input.name === 'queue_pending_threshold'
              ? '100'
              : input.name === 'queue_age_threshold_minutes'
                ? '30'
                : '5000',
        },
      });
    }
    fireEvent.change(pending, { target: { value: '200' } });
    expect(screen.queryByText(text('scr07.saved'))).toBeNull();
    fireEvent.click(screen.getByRole('button', { name: text('scr07.save') }));
    await screen.findByText(text('scr07.saved'), {}, WAIT);
    expect(bodies).toEqual([{ ...initial, queue_pending_threshold: 200 }]);
    expect(await axe(container)).toHaveNoViolations();
  });
  it('un rechazo de escritura conserva los valores y nunca muestra guardado', async () => {
    const initial = {
      queue_pending_threshold: 100,
      queue_age_threshold_minutes: 30,
      clock_drift_threshold_ms: 5000,
    };
    server.use(
      http.get('http://localhost/plants/:plant_id/fleet-thresholds', () =>
        HttpResponse.json({
          ...initial,
          plant_id: PLANT,
          configured: true,
          updated_by: null,
          updated_at: null,
        }),
      ),
      http.put('http://localhost/plants/:plant_id/fleet-thresholds', () =>
        apiError('forbidden', 403),
      ),
    );
    const { Wrapper } = await fleetFixture();
    render(<Thresholds plantId={PLANT} />, { wrapper: Wrapper });
    const input = await screen.findByRole(
      'spinbutton',
      { name: text('scr07.queue_threshold') },
      WAIT,
    );
    fireEvent.change(input, { target: { value: '205' } });
    fireEvent.click(screen.getByRole('button', { name: text('scr07.save') }));
    await screen.findByRole('alert', {}, WAIT);
    expect(
      screen.getByRole<HTMLInputElement>('spinbutton', { name: text('scr07.queue_threshold') })
        .value,
    ).toBe('205');
    expect(screen.queryByText(text('scr07.saved'))).toBeNull();
  });
});
