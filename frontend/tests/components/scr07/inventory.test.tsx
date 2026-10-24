import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import fc from 'fast-check';
import { HttpResponse, http } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import Page from '@/pages/scr07/inventory';
import Alarms from '@/pages/scr07/alarms';
import { FleetView, WARNINGS, warningLabel } from '@/pages/scr07/inventory/FleetView';
import { VirtualFleetTable } from '@/pages/scr07/inventory/VirtualFleetTable';
import { fleetPort, readFleet } from '@/ports/scr07Fleet';
import { text } from '@/texts';
import catalog from '@/texts/scr07.json';
import { API_DOCUMENT } from '../../generators';
import { server } from '../../msw/server';
import { settle } from '../../support/queryCache';
import { fleetFixture, INSTANT, node, plants, PLANT } from './support';

const WAIT = { timeout: 10_000 };

describe('SCR-07 inventario', { timeout: 30_000 }, () => {
  it('virtualiza 1 000 filas generadas y el teclado recorre todas sin perder encabezados', () => {
    const nodes = fc.sample(fc.integer({ min: 1000, max: 9000 }), 1000).map((value, index) => ({
      ...node(index + 1),
      code: `NODO-${String(value)}-${String(index)}`,
    }));
    const { container } = render(
      <VirtualFleetTable
        rows={nodes.map((value) => ({ id: value.node_id, cells: [value.code] }))}
        headings={[text('scr07.node')]}
        caption={text('scr07.title')}
      />,
    );
    expect(screen.getAllByRole('columnheader')).toHaveLength(1);
    expect(container.querySelectorAll('tr[data-index]').length).toBeLessThanOrEqual(9);
    const first = container.querySelector<HTMLElement>('[data-index="0"]');
    act(() => {
      first?.focus();
    });
    for (let index = 0; index < 1000; index++) {
      expect(document.activeElement?.getAttribute('data-index')).toBe(String(index));
      expect(container.querySelectorAll('tr[data-index]').length).toBeLessThanOrEqual(9);
      if (index < 999)
        fireEvent.keyDown(document.activeElement ?? document.body, { key: 'ArrowDown' });
    }
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'Home' });
    expect(document.activeElement?.getAttribute('data-index')).toBe('0');
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'End' });
    expect(document.activeElement?.getAttribute('data-index')).toBe('999');
    fireEvent.keyDown(document.activeElement ?? document.body, { key: 'ArrowUp' });
    expect(document.activeElement?.getAttribute('data-index')).toBe('998');
    expect(screen.getAllByRole('columnheader')).toHaveLength(1);
  }, 60_000);

  it.each([300, 301])('cuenta también cámaras al decidir la virtualización: %i filas', (total) => {
    const value = node();
    value.cameras = Array.from({ length: total - 1 }, (_, index) => ({
      camera_id: `CAMERA-${String(index)}`,
      code: null,
      connected: true,
      measured_fps: 10,
      declared_min_fps: 5,
      observability_state: 'observable',
    }));
    const { container } = render(
      <MemoryRouter>
        <FleetView nodes={[value]} plants={plants} />
      </MemoryRouter>,
    );
    expect(container.querySelectorAll('tr[data-index]').length).toBe(total === 300 ? 300 : 9);
  });

  it('el inventario real de 1 000 nodos mantiene solo nueve filas montadas', () => {
    const nodes = Array.from({ length: 1000 }, (_, index) => node(index + 1));
    const { container } = render(
      <MemoryRouter>
        <FleetView nodes={nodes} plants={plants} />
      </MemoryRouter>,
    );
    expect(container.querySelectorAll('tr[data-index]')).toHaveLength(9);
    expect(screen.getByRole('table').getAttribute('aria-rowcount')).toBe('1001');
    const first = container.querySelector<HTMLElement>('[data-index="0"]');
    fireEvent.keyDown(first ?? document.body, { key: 'End' });
    expect(document.activeElement?.getAttribute('data-index')).toBe('999');
    expect(screen.getByRole('link', { name: node(1000).code })).toBeDefined();
  });

  it('rechaza cursores repetidos sin entrar en un bucle de red', async () => {
    let count = 0;
    server.use(
      http.get('http://localhost/fleet/nodes', () => {
        count++;
        return HttpResponse.json({ nodes: [], next_after: count < 3 ? 'cursor-repetido' : null });
      }),
    );
    await fleetFixture();
    await expect(readFleet({}, new AbortController().signal)).rejects.toThrow(
      'Cursor de inventario repetido.',
    );
    expect(count).toBe(2);
  });

  it('las ocho clases de app.yaml coinciden exactamente con catálogo y etiquetas neutrales', () => {
    const schema = API_DOCUMENT.components.schemas.FleetAlarmKind as { enum: string[] };
    expect(new Set(WARNINGS)).toEqual(new Set(schema.enum));
    expect(
      new Set(
        Object.keys(catalog)
          .filter((key) => key.startsWith('scr07.alarm_warning.'))
          .map((key) => key.replace('scr07.alarm_warning.', '')),
      ),
    ).toEqual(new Set(WARNINGS));
    for (const warning of WARNINGS)
      expect(warningLabel(warning)).toBe(text(`scr07.alarm_warning.${warning}`));
    expect(warningLabel('future_warning')).toBe(text('scr07.unknown'));
    expect(fleetPort.operations.every((operation) => operation.status === 'fixed')).toBe(true);
  });

  it('envía los tres filtros juntos solo al aplicar, con paginación del mismo alcance', async () => {
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/fleet/nodes', ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          nodes: [node(url.searchParams.get('after') === null ? 1 : 2)],
          next_after: url.searchParams.get('after') === null ? 'cursor-sintetico' : null,
        });
      }),
    );
    const { Wrapper } = await fleetFixture();
    render(<Page />, { wrapper: Wrapper });
    await screen.findByRole('link', { name: node().code }, WAIT);
    await screen.findByRole('option', { name: 'Planta sintética' }, WAIT);
    const user = userEvent.setup();
    await user.selectOptions(screen.getByRole('combobox', { name: text('scr07.plant') }), PLANT);
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr07.communication') }),
      'mute',
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr07.warning') }),
      'clock_drift',
    );
    expect(requests).toHaveLength(2);
    await user.click(screen.getByRole('button', { name: text('scr07.apply') }));
    await waitFor(() => {
      expect(requests).toHaveLength(4);
    }, WAIT);
    for (const url of requests.slice(2)) {
      expect(url.searchParams.get('plant_id')).toBe(PLANT);
      expect(url.searchParams.get('communication_state')).toBe('mute');
      expect(url.searchParams.get('warning')).toBe('clock_drift');
      expect(url.searchParams.get('limit')).toBe('200');
    }
  });

  it('refresca a los 60 s y deja de consultar oculta o inactiva; vuelve al hacerse visible', async () => {
    let count = 0;
    server.use(
      http.get('http://localhost/fleet/nodes', () => {
        count++;
        return HttpResponse.json({ nodes: [node()], next_after: null });
      }),
    );
    const { Wrapper, clock, pause, cache } = await fleetFixture();
    render(<Page />, { wrapper: Wrapper });
    await screen.findByRole('link', { name: node().code }, WAIT);
    expect(count).toBe(1);
    await act(async () => {
      clock.advance(59_999);
      await settle();
    });
    expect(count).toBe(1);
    await act(async () => {
      clock.advance(1);
      await settle();
    });
    await waitFor(() => {
      expect(count).toBe(2);
    }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    // Drenar cada tic por separado: avanzar dos minutos de golpe podría abortar una petición
    // indebida antes de que MSW la cuente, y ocultaría la mutación de la puerta de visibilidad.
    for (let tick = 0; tick < 2; tick++) {
      await act(async () => {
        clock.advance(60_000);
        await settle();
      });
      await waitFor(() => {
        expect(cache.client.isFetching()).toBe(0);
      }, WAIT);
      expect(count).toBe(2);
    }
    visibility.mockReturnValue('visible');
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(count).toBe(3);
    }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    pause();
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(count).toBe(3);
  });

  it('estado vacío y axe sin violaciones en inventario', async () => {
    server.use(
      http.get('http://localhost/fleet/nodes', () =>
        HttpResponse.json({ nodes: [], next_after: null }),
      ),
    );
    const { Wrapper } = await fleetFixture();
    const { container } = render(<Page />, { wrapper: Wrapper });
    await screen.findByText(text('scr07.empty'), {}, WAIT);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('recorre rutas reales: inventario, detalle, umbrales y pestaña de alarmas', async () => {
    server.use(
      http.get('http://localhost/fleet/nodes', () =>
        HttpResponse.json({ nodes: [node()], next_after: null }),
      ),
      http.get('http://localhost/fleet/nodes/:node_id', () =>
        HttpResponse.json({
          node: node(),
          heartbeat_history: [
            {
              heartbeat_id: 'LATIDO-SINTETICO',
              received_at: INSTANT,
              sent_at: INSTANT,
              payload_summary: {},
            },
          ],
          next_history_after: null,
        }),
      ),
      http.get('http://localhost/plants/:plant_id/fleet-thresholds', () =>
        HttpResponse.json({
          plant_id: PLANT,
          queue_pending_threshold: 100,
          queue_age_threshold_minutes: 30,
          clock_drift_threshold_ms: 5000,
          configured: false,
          updated_by: null,
          updated_at: null,
        }),
      ),
      http.get('http://localhost/fleet/alarms', () =>
        HttpResponse.json({ alarms: [], next_after: null }),
      ),
    );
    const { Wrapper } = await fleetFixture();
    render(
      <Routes>
        <Route path="/fleet" element={<Page />} />
        <Route path="/fleet/nodes/:node_id" element={<Page />} />
        <Route path="/plants/:plant_id/fleet-thresholds" element={<Page />} />
        <Route path="/fleet/alarms" element={<Alarms />} />
      </Routes>,
      { wrapper: Wrapper },
    );
    const link = await screen.findByRole('link', { name: node().code }, WAIT);
    fireEvent.click(link);
    await screen.findByRole('heading', { name: text('scr07.history') }, WAIT);
    fireEvent.click(screen.getByRole('link', { name: text('scr07.thresholds') }));
    await screen.findByRole('spinbutton', { name: text('scr07.queue_threshold') }, WAIT);
    fireEvent.click(screen.getByRole('link', { name: text('scr07.alarms') }));
    await screen.findByText(text('scr07.alarms_empty'), {}, WAIT);
  });

  it('la tabla poblada conserva semáforo, cobertura, resultado failed y accesibilidad', async () => {
    const value = node();
    value.zones = ['ZONA-SINTETICA'];
    value.zone_states = [
      {
        zone_id: 'ZONA-SINTETICA',
        mode: 'productive',
        observability_state: 'degraded',
        catalog_version: 2,
        coverage_ok: false,
      },
    ];
    value.cameras = [
      {
        camera_id: 'CAMARA-SINTETICA',
        code: 'CAM-SINT',
        connected: false,
        measured_fps: 2,
        declared_min_fps: 10,
        observability_state: 'not_observable',
      },
    ];
    const { container } = render(
      <MemoryRouter>
        <FleetView nodes={[value]} plants={plants} />
      </MemoryRouter>,
    );
    expect(screen.getByText(text('scr07.coverage_not_met'))).toBeDefined();
    expect(
      screen.getByText(`${text('scr07.update')}: ${text('scr07.update.failed')}`),
    ).toBeDefined();
    expect(container.querySelectorAll('[data-part="legend"]')).toHaveLength(2);
    expect(await axe(container)).toHaveNoViolations();
  });
});
