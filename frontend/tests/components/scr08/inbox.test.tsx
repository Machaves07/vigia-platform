import { act, fireEvent, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import { describe, expect, it, vi } from 'vitest';
import { axe } from 'vitest-axe';
import Inbox from '@/pages/scr08/inbox';
import { readInbox } from '@/ports/scr08Findings';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { settle } from '../../support/queryCache';
import { finding, fixture, gate, INSTANT, page, PLANT, zone } from './support';

const WAIT = { timeout: 10_000 };

describe('SCR-08 bandeja', { timeout: 40_000 }, () => {
  it('una fecha fuera del rango de Date no lanza RangeError ni consulta el servidor', async () => {
    const { Wrapper, cache } = await fixture({ path: '/findings' });
    let calls = 0;
    server.use(
      http.get('http://localhost/loop/findings', () => {
        calls++;
        return HttpResponse.json(page());
      }),
    );
    render(<Inbox />, { wrapper: Wrapper });
    await screen.findByText(text('scr08.inbox.empty'), {}, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    fireEvent.change(screen.getByLabelText(text('scr08.inbox.to')), {
      target: { value: '999999-12-31' },
    });
    fireEvent.submit(screen.getByRole('form', { name: text('scr08.inbox.filters') }));
    expect(screen.getByRole('alert').textContent).toBe(text('scr08.inbox.invalid_period'));
    expect(calls).toBe(1);
  });
  it('propaga el cursor opaco del servidor sin fabricar desplazamiento y acota limit a 50', async () => {
    await fixture();
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/loop/findings', ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          ...page(),
          next_cursor: url.searchParams.get('cursor') === null ? 'cursor-recibido' : null,
        });
      }),
    );
    await readInbox({}, 2, new AbortController().signal);
    expect(requests.map((url) => url.searchParams.get('cursor'))).toEqual([
      null,
      'cursor-recibido',
    ]);
    expect(requests.map((url) => url.searchParams.get('limit'))).toEqual(['50', '50']);
    expect(
      requests.some((url) => url.searchParams.has('offset') || url.searchParams.has('page')),
    ).toBe(false);
  });
  it('zona no productiva: sustituye el episodio completo por su compuerta, sin cifras', async () => {
    const { Wrapper, cache } = await fixture({ path: '/findings' });
    const value = gate();
    value.mounting.status = 'pending';
    value.resulting_mode = 'no_capture';
    server.use(
      http.get('http://localhost/loop/findings', () => HttpResponse.json(page([finding()]))),
      http.get('http://localhost/zones/:id/gates', () => HttpResponse.json(value)),
    );
    const { container } = render(<Inbox />, { wrapper: Wrapper });
    await screen.findByRole('group', { name: text('components_status.gate.group_label') }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(container.querySelector('tbody td')?.getAttribute('colspan')).toBe('11');
    expect(container.querySelector('tbody td')?.textContent).toBe(
      `${zone().name} ${text('components_status.gate.no_scope_record')}`,
    );
    expect(screen.queryByRole('link', { name: zone().name })).toBeNull();
    expect(container.querySelector('tbody [data-part="legend"]')).toBeNull();
  });
  it('aplica filtros en bloque; un filtro que excluye vencidos no quita su cabecera', async () => {
    const { Wrapper, cache } = await fixture({ path: '/findings' });
    const requests: URL[] = [];
    const overdue: URL[] = [];
    server.use(
      http.get('http://localhost/loop/findings', ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json(page(url.searchParams.has('lifecycle_state') ? [] : [finding()]));
      }),
      http.get('http://localhost/loop/overdue', ({ request }) => {
        overdue.push(new URL(request.url));
        return HttpResponse.json({
          plant_id: PLANT,
          highlight_threshold_days: 3,
          items: [],
          zones: [{ zone_id: zone().zone_id, overdue_count: 7, highlighted_count: 0 }],
          partition_notice: null,
          next_cursor: null,
        });
      }),
    );
    const { container } = render(<Inbox />, { wrapper: Wrapper });
    await screen.findByRole('link', { name: zone().name }, WAIT);
    await screen.findByText('7', { selector: '[data-part="overdue"] strong' }, WAIT);
    await screen.findByRole('option', { name: zone().name }, WAIT);
    const user = userEvent.setup();
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr08.inbox.zone') }),
      zone().zone_id,
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr08.inbox.family') }),
      'dwell',
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr08.inbox.tier') }),
      'tier_2',
    );
    await user.selectOptions(
      screen.getByRole('combobox', { name: text('scr08.inbox.state') }),
      'closed',
    );
    fireEvent.change(screen.getByLabelText(text('scr08.inbox.from')), {
      target: { value: '2025-12-01' },
    });
    fireEvent.change(screen.getByLabelText(text('scr08.inbox.to')), {
      target: { value: '2025-12-31' },
    });
    expect(requests).toHaveLength(1);
    await user.click(screen.getByRole('button', { name: text('scr08.inbox.apply') }));
    await screen.findByText(text('scr08.inbox.empty'), {}, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(requests).toHaveLength(2);
    const query = requests[1]?.searchParams;
    expect(query?.getAll('zone_id')).toEqual([zone().zone_id]);
    expect(query?.get('family')).toBe('dwell');
    expect(query?.get('tier')).toBe('tier_2');
    expect(query?.getAll('lifecycle_state')).toEqual(['closed']);
    expect(query?.get('from')).toBe('2025-12-01T00:00:00.000Z');
    expect(query?.get('to')).toBe(INSTANT.replace('T12:00', 'T00:00'));
    expect(container.querySelector('[data-part="overdue"] strong')?.textContent).toBe('7');
    expect(overdue).toHaveLength(1);
    expect(overdue[0]?.searchParams.get('plant_id')).toBe(PLANT);
    expect(Array.from(overdue[0]?.searchParams.keys() ?? []).sort()).toEqual(['limit', 'plant_id']);
    expect(requests[0]?.searchParams.has('lifecycle_state')).toBe(false);
  });

  it('pagina con el cursor opaco exacto y limit 50, nunca desplazamiento; axe sin violaciones', async () => {
    const { Wrapper, cache } = await fixture({ path: '/findings' });
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/loop/findings', ({ request }) => {
        const url = new URL(request.url);
        requests.push(url);
        return HttpResponse.json({
          ...page([finding(url.searchParams.get('cursor') === null ? 1 : 2)]),
          next_cursor: url.searchParams.get('cursor') === null ? 'cursor-opaco-del-servidor' : null,
        });
      }),
    );
    const { container } = render(<Inbox />, { wrapper: Wrapper });
    const more = await screen.findByRole('button', { name: text('scr08.inbox.more') }, WAIT);
    fireEvent.click(more);
    await waitFor(() => {
      expect(screen.getAllByRole('link', { name: zone().name })).toHaveLength(2);
    }, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(requests.map((url) => url.searchParams.get('cursor'))).toEqual([
      null,
      null,
      'cursor-opaco-del-servidor',
    ]);
    for (const request of requests) {
      expect(request.searchParams.get('limit')).toBe('50');
      expect(request.searchParams.has('offset')).toBe(false);
      expect(request.searchParams.has('page')).toBe(false);
    }
    expect(screen.queryByRole('button', { name: text('scr08.inbox.more') })).toBeNull();
    expect(await axe(container)).toHaveNoViolations();
  });

  it('refresca cada 60 s solo visible y activa, conservando los filtros', async () => {
    const { Wrapper, clock, cache, pause } = await fixture({ path: '/findings' });
    const requests: URL[] = [];
    server.use(
      http.get('http://localhost/loop/findings', ({ request }) => {
        requests.push(new URL(request.url));
        return HttpResponse.json(page([finding()]));
      }),
    );
    render(<Inbox />, { wrapper: Wrapper });
    await screen.findByRole('link', { name: zone().name }, WAIT);
    fireEvent.change(screen.getByRole('combobox', { name: text('scr08.inbox.state') }), {
      target: { value: 'overdue' },
    });
    fireEvent.submit(screen.getByRole('form', { name: text('scr08.inbox.filters') }));
    await waitFor(() => {
      expect(requests).toHaveLength(2);
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    await act(async () => {
      clock.advance(59_999);
      await settle();
    });
    expect(requests).toHaveLength(2);
    await act(async () => {
      clock.advance(1);
      await settle();
    });
    await waitFor(() => {
      expect(requests).toHaveLength(3);
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(requests[2]?.searchParams.getAll('lifecycle_state')).toEqual(['overdue']);
    const visibility = vi.spyOn(document, 'visibilityState', 'get').mockReturnValue('hidden');
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(requests).toHaveLength(3);
    visibility.mockReturnValue('visible');
    pause();
    await act(async () => {
      clock.advance(60_000);
      await settle();
    });
    expect(requests).toHaveLength(3);
  });

  it('declara avisos y conserva la hora agregada recibida; no lee detalle ni inventa clips', async () => {
    const { Wrapper, cache } = await fixture({ path: '/findings' });
    const value = finding();
    value.time_aggregated = true;
    value.aggregation_window_minutes = 60;
    value.notices = [
      { code: 'agreement_revoked', since: INSTANT, cause: null },
      { code: 'walk_test_regression_pending', since: INSTANT, cause: 'catalog_change' },
    ];
    const revokedGate = gate();
    revokedGate.resulting_mode = 'commissioning';
    revokedGate.usage.status = 'revoked';
    let details = 0;
    server.use(
      http.get('http://localhost/zones/:id/gates', () => HttpResponse.json(revokedGate)),
      http.get('http://localhost/loop/findings', () =>
        HttpResponse.json({
          ...page([value]),
          zones: [
            {
              plant_id: PLANT,
              zone_id: zone().zone_id,
              notice: 'coverage_unavailable',
              node_id: null,
              node_code: null,
              communication_state: null,
            },
          ],
          partitions: [{ plant_id: PLANT, notice: 'projection_rebuilding' }],
        }),
      ),
      http.get('http://localhost/loop/findings/:id', () => {
        details++;
        return HttpResponse.error();
      }),
    );
    const { container } = render(<Inbox />, { wrapper: Wrapper });
    await screen.findByText(text('scr08.inbox.aggregated'), {}, WAIT);
    await waitFor(() => {
      expect(cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(container.querySelector('tbody td:nth-child(10) time')?.getAttribute('datetime')).toBe(
      INSTANT,
    );
    expect(screen.getAllByText(text('scr08.inbox.data_unavailable'))).toHaveLength(2);
    expect(screen.getByText(text('scr08.inbox.notice.agreement_revoked'))).toBeDefined();
    expect(screen.getByText(text('scr08.inbox.notice.walk_test_regression_pending'))).toBeDefined();
    expect(
      screen.getByRole('group', { name: text('components_status.gate.group_label') }),
    ).toBeDefined();
    expect(
      screen.getByText(`Planta sintética: ${text('scr08.inbox.notice.projection_rebuilding')}`),
    ).toBeDefined();
    expect(
      screen.getByText(`${zone().name}: ${text('scr08.inbox.notice.coverage_unavailable')}`),
    ).toBeDefined();
    expect(details).toBe(0);
    expect(await axe(container)).toHaveNoViolations();
  });

  it('la reconstrucción no convierte un recuento desconocido en cero', async () => {
    const { Wrapper } = await fixture({ path: '/findings' });
    server.use(
      http.get('http://localhost/loop/overdue', () =>
        HttpResponse.json({
          plant_id: PLANT,
          highlight_threshold_days: 3,
          items: [],
          zones: [],
          partition_notice: 'projection_rebuilding',
          next_cursor: null,
        }),
      ),
    );
    const { container } = render(<Inbox />, { wrapper: Wrapper });
    await screen.findByText(text('scr08.inbox.count_unavailable'), {}, WAIT);
    expect(container.querySelector('[data-part="overdue"]')).toBeNull();
  });

  it('rechaza cursor repetido, vacío o de más de 128 caracteres sin bucle de red', async () => {
    await fixture();
    for (const cursor of ['repetido', '', 'x'.repeat(129)]) {
      let count = 0;
      server.use(
        http.get('http://localhost/loop/findings', () => {
          count++;
          return HttpResponse.json({ ...page(), next_cursor: cursor });
        }),
      );
      await expect(readInbox({}, 3, new AbortController().signal)).rejects.toThrow(
        'Cursor de bandeja inválido o repetido.',
      );
      expect(count).toBe(cursor === 'repetido' ? 2 : 1);
    }
  });

  it('un error de proyección se presenta con ScreenState y permite repetir', async () => {
    const { Wrapper } = await fixture({ path: '/findings' });
    server.use(
      http.get('http://localhost/loop/findings', () =>
        HttpResponse.json(
          {
            code: 'invalid_request',
            message: 'Petición sintética',
            correlation_id: 'correlacion-sintetica',
            retryable: false,
          },
          { status: 400 },
        ),
      ),
    );
    render(<Inbox />, { wrapper: Wrapper });
    await screen.findByRole('button', { name: text('screen_state.retry') }, WAIT);
    server.use(http.get('http://localhost/loop/findings', () => HttpResponse.json(page())));
    fireEvent.click(screen.getByRole('button', { name: text('screen_state.retry') }));
    await screen.findByText(text('scr08.inbox.empty'), {}, WAIT);
  });
});
