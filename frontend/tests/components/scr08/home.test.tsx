import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Home from '@/pages/scr08/home';
import Inbox from '@/pages/scr08/inbox';
import { ZoneCard } from '@/pages/scr08/home/HomeZones';
import { gateDisplay, findingsPort } from '@/ports/scr08Findings';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { settle } from '../../support/queryCache';
import { fixture, gate, INSTANT, plant, zone } from './support';

const WAIT = { timeout: 20_000 };

describe('SCR-08 inicio', { timeout: 40_000 }, () => {
  it.each([0, 50, 51])(
    'borde de %i zonas: no excede las 50 tarjetas ni consulta automáticamente',
    async (count) => {
      const { Wrapper, cache } = await fixture({
        plants: [plant(Array.from({ length: count }, (_, i) => zone(i + 1)))],
      });
      let calls = 0;
      server.use(
        http.get('http://localhost/zones/:id/coverage/at', () => {
          calls++;
          return HttpResponse.error();
        }),
      );
      const { container } = render(<Home />, { wrapper: Wrapper });
      await waitFor(() => {
        expect(cache.client.isFetching()).toBe(0);
        expect(screen.queryAllByRole('article')).toHaveLength(Math.min(count, 50));
      }, WAIT);
      expect(screen.queryAllByRole('article')).toHaveLength(Math.min(count, 50));
      expect(container.querySelectorAll('[data-part="grouped-zones"] li')).toHaveLength(
        Math.max(count - 50, 0),
      );
      expect(calls).toBe(0);
    },
  );

  it('un cambio de zonas no reutiliza la intención de consultar cobertura del alcance anterior', async () => {
    const { Wrapper, cache } = await fixture();
    let calls = 0;
    server.use(
      http.get('http://localhost/zones/:zone_id/coverage/at', ({ params }) => {
        calls++;
        return HttpResponse.json({
          zone_id: params.zone_id,
          instant: INSTANT,
          state: 'observable',
          causes: [],
          layer: 'platform',
          clock_basis: 'platform',
          source_record_ids: [],
        });
      }),
    );
    render(<Home />, { wrapper: Wrapper });
    fireEvent.click(
      await screen.findByRole('button', { name: text('scr08.home.load_coverage') }, WAIT),
    );
    await waitFor(() => {
      expect(calls).toBe(1);
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    server.use(
      http.get('http://localhost/hierarchy', () =>
        HttpResponse.json({ plants: [plant([zone(), zone(2)])], nodes: [], organization: {} }),
      ),
    );
    const section = screen.getByRole('region', { name: text('scr08.home.zones') });
    const refresh = within(section).getAllByRole('button', {
      name: text('components_status.refresh.button'),
    })[0];
    expect(refresh).toBeDefined();
    if (refresh !== undefined) fireEvent.click(refresh);
    await waitFor(() => {
      expect(screen.getAllByRole('article')).toHaveLength(2);
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(calls).toBe(1);
  });

  it('120 zonas: consulta cobertura solo a demanda, máximo 50, y agrupa las otras 70 por planta', async () => {
    const second = {
      ...plant(),
      plant_id: '0190a1b2-0000-7000-8000-000000000002',
      name: 'Otra planta sintética',
    };
    second.zones = Array.from({ length: 60 }, (_, i) => ({
      ...zone(i + 61),
      plant_id: second.plant_id,
    }));
    const { Wrapper, clock, cache } = await fixture({
      plants: [plant(Array.from({ length: 60 }, (_, i) => zone(i + 1))), second],
    });
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/zones/:zone_id/coverage/at', ({ params, request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json({
          zone_id: params.zone_id,
          instant: INSTANT,
          state: 'observable',
          causes: [],
          layer: 'platform',
          clock_basis: 'platform',
          source_record_ids: [],
        });
      }),
    );
    const { container } = render(<Home />, { wrapper: Wrapper });
    const button = await screen.findByRole(
      'button',
      { name: text('scr08.home.load_coverage') },
      WAIT,
    );
    expect(requests).toHaveLength(0);
    fireEvent.click(button);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
      expect(requests).toHaveLength(50);
    }, WAIT);
    expect(screen.getAllByRole('article')).toHaveLength(50);
    expect(container.querySelectorAll('[data-part="grouped-zones"] li')).toHaveLength(70);
    expect(container.querySelectorAll('[data-part="grouped-zones"]')).toHaveLength(2);
    expect(new Set(requests.map((url) => url.pathname)).size).toBe(50);
    for (const request of requests) expect(request.searchParams.get('instant')).toBe(INSTANT);
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(requests).toHaveLength(50);
  });

  it.each(['no_capture', 'commissioning'] as const)(
    'zona no productiva %s: compuerta sin contadores ni números en el árbol',
    (mode) => {
      const value = gate();
      value.resulting_mode = mode;
      value.mounting.status = mode === 'no_capture' ? 'pending' : 'approved';
      value.usage = { status: 'pending', decided_at: null, decided_by: null, agreement_id: null };
      const { container } = render(
        <MemoryRouter>
          <ZoneCard
            zone={{ ...zone(), name: 'Zona de prueba' }}
            gate={value}
            regression="current"
            coverage={null}
            timeZone="America/Bogota"
          />
        </MemoryRouter>,
      );
      const article = screen.getByRole('article', { name: 'Zona de prueba' });
      expect(
        within(article).getByRole('group', { name: text('components_status.gate.group_label') }),
      ).toBeDefined();
      expect(article.textContent).not.toMatch(/\d/);
      expect(container.querySelector('[data-part="overdue"]')).toBeNull();
      expect(container.querySelector('[data-part="legend"]')).toBeNull();
    },
  );

  it('acta pendiente y revocación se etiquetan desde estados del servidor, sin inventar productividad', () => {
    const value = gate();
    expect(gateDisplay(value, 'pending').state).toBe('regression_pending');
    value.usage.status = 'revoked';
    expect(gateDisplay(value, 'pending')).toEqual({
      state: 'agreement_revoked',
      agreement_revoked_at: INSTANT,
    });
    expect(
      findingsPort.operations.every(
        (operation) => operation.status === 'fixed' && operation.kind === 'read',
      ),
    ).toBe(true);
  });

  it('sin permisos de sección no pide cobertura, compuertas ni vencidos', async () => {
    const { Wrapper, cache } = await fixture({ permissions: ['findings.read'] });
    let calls = 0;
    server.use(
      http.get('http://localhost/loop/overdue', () => {
        calls++;
        return HttpResponse.json({
          plant_id: plant().plant_id,
          zones: [],
          items: [],
          partition_notice: null,
          next_cursor: null,
        });
      }),
      http.get('http://localhost/zones/:zone_id/gates', () => {
        calls++;
        return HttpResponse.json(gate());
      }),
    );
    render(<Home />, { wrapper: Wrapper });
    await screen.findByRole('heading', { name: text('scr08.home.queue') }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(calls).toBe(0);
    expect(screen.queryByRole('button', { name: text('scr08.home.load_coverage') })).toBeNull();
  });

  it('recorrido real de inicio a bandeja y axe del panel sin violaciones', async () => {
    const { Wrapper, cache } = await fixture();
    const { container } = render(
      <Routes>
        <Route path="/home" element={<Home />} />
        <Route path="/findings" element={<Inbox />} />
      </Routes>,
      { wrapper: Wrapper },
    );
    await screen.findByRole('button', { name: text('scr08.home.load_coverage') }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
    fireEvent.click(screen.getByRole('link', { name: text('scr08.inbox.title') }));
    await screen.findByRole('heading', { name: text('scr08.inbox.title'), level: 1 }, WAIT);
    expect(screen.getByRole('form', { name: text('scr08.inbox.filters') })).toBeDefined();
  });
});
