/**
 * Inicio del coordinador (VIG-365, S3-02 a S3-10): lo urgente antes que la cobertura, la cobertura
 * cargada al abrir (auditada, una vez por apertura, nunca «Desconocido»), los 5 hallazgos nuevos
 * con su siguiente paso y una sola actualización. Los hallazgos de zonas no productivas ni se
 * muestran ni se cuentan (P9). Las aserciones comparan atributos, claves del catálogo y textos
 * exactos, nunca subcadenas con datos (VIG-194). Datos sintéticos.
 */
import { act, fireEvent, render, screen, waitFor, within } from '@testing-library/react';
import { HttpResponse, http, type HttpHandler } from 'msw';
import { MemoryRouter, Route, Routes } from 'react-router';
import { describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';
import Home from '@/pages/scr08/home';
import Inbox from '@/pages/scr08/inbox';
import { ZoneCard } from '@/pages/scr08/home/HomeZones';
import { homeSummary } from '@/pages/scr08/home/summary';
import { gateDisplay, findingsPort, type Finding } from '@/ports/scr08Findings';
import { text } from '@/texts';
import { server } from '../../msw/server';
import { settle } from '../../support/queryCache';
import { finding, fixture, gate, INSTANT, page, plant, zone } from './support';

const WAIT = { timeout: 20_000 };
const COORDINATOR = [
  'findings.read',
  'coverage.read',
  'catalog.read',
  'executive_summary.read',
  'findings.classify',
  'review_queue.resolve',
  'actions.manage',
  'findings.close_and_sign',
  'closure_attachments.read',
];
const DAY = 86_400_000;

function at(offsetDays: number): string {
  return new Date(Date.parse(INSTANT) + offsetDays * DAY).toISOString();
}

function inState(
  index: number,
  lifecycle_state: Finding['lifecycle_state'],
  extra: Partial<Finding> = {},
): Finding {
  return { ...finding(index), lifecycle_state, ...extra };
}

/** Bandeja sintética: 2 vencidas, 3 por revisar, 1 que vence esta semana y otra después. */
function inbox(): Finding[] {
  return [
    inState(1, 'overdue', { overdue_days: 2, due_at: at(-2) }),
    inState(2, 'overdue', { overdue_days: 9, due_at: at(-9) }),
    inState(3, 'in_review', { occurred_at: at(-1) }),
    inState(4, 'in_review', { occurred_at: at(-2) }),
    inState(5, 'in_review', { occurred_at: at(-3) }),
    inState(6, 'received', { occurred_at: at(-4) }),
    inState(7, 'received', { occurred_at: at(-5) }),
    inState(8, 'received', { occurred_at: at(-6) }),
    inState(9, 'action_assigned', { due_at: at(3) }),
    inState(10, 'action_assigned', { due_at: at(20) }),
    inState(11, 'closed'),
  ];
}

/** Manejadores propios de la prueba: se registran después de los del fixture, que no los tapan. */
const pending: HttpHandler[] = [];
function later(...handlers: HttpHandler[]): void {
  pending.push(...handlers);
}

async function open(
  options: Parameters<typeof fixture>[0] = {},
  items: Finding[] = inbox(),
): Promise<Awaited<ReturnType<typeof fixture>> & { container: HTMLElement }> {
  const env = await fixture({ permissions: COORDINATOR, ...options });
  server.use(
    http.get('http://localhost/loop/findings', () => HttpResponse.json(page(items))),
    ...pending.splice(0),
  );
  const { container } = render(<Home />, { wrapper: env.Wrapper });
  await screen.findByRole('region', { name: text('scr08.home.coverage') }, WAIT);
  // Las lecturas van en cadena (jerarquía → compuertas y cobertura): se espera a que ninguna
  // sección siga cargando, no solo a un instante sin peticiones.
  await waitFor(() => {
    expect(env.cache.client.isFetching()).toBe(0);
    expect(container.querySelector('[aria-busy="true"]')).toBeNull();
    expect(container.querySelector('[data-part="coverage_loading"]')).toBeNull();
  }, WAIT);
  return { ...env, container };
}

function count(card: string): string | null {
  return (
    document
      .querySelector(`[data-card="${card}"] [data-part="count"]`)
      ?.getAttribute('data-value') ?? null
  );
}

describe('SCR-08 inicio del coordinador (VIG-365)', { timeout: 40_000 }, () => {
  it('lo urgente va antes que la cobertura, y los hallazgos nuevos al final', async () => {
    const { container } = await open();
    const order = [...container.querySelectorAll('main > section, main > div > section')].map(
      (section) => section.getAttribute('aria-label'),
    );
    expect(order).toEqual([
      text('scr08.home.urgent'),
      text('scr08.home.coverage'),
      text('scr08.home.new_table'),
    ]);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('scr08.home.title'));
  });

  it('tres tarjetas con los recuentos del estado del servidor', async () => {
    await open();
    expect([count('overdue'), count('review'), count('due_week')]).toEqual(['2', '3', '1']);
    expect(document.querySelector('[data-card="overdue"]')?.getAttribute('data-alert')).toBe(
      'true',
    );
  });

  it('la tabla de vencidas lleva «Abrir lazo» por fila, la más vencida primero', async () => {
    await open();
    const table = screen.getByRole('region', { name: text('scr08.home.overdue_table') });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-finding'))).toEqual([
      finding(2).finding_state_id,
      finding(1).finding_state_id,
    ]);
    expect(
      rows.map((row) => row.querySelector('a[data-access="loop"]')?.getAttribute('href')),
    ).toEqual([
      `/findings/${finding(2).finding_state_id}/loop`,
      `/findings/${finding(1).finding_state_id}/loop`,
    ]);
    // La columna del siguiente paso va fija en la caja con desplazamiento.
    expect(rows[0]?.lastElementChild?.className).toMatch(/sticky/u);
  });

  it('sin vencidas, la tabla sigue visible y lo dice', async () => {
    await open({}, [inState(1, 'received')]);
    expect(document.querySelector('[data-part="overdue_empty"]')?.textContent).toBe(
      text('scr08.home.overdue_empty'),
    );
  });

  it('los 5 hallazgos nuevos más recientes, cada uno con su siguiente paso, y la bandeja (N)', async () => {
    await open();
    const table = screen.getByRole('region', { name: text('scr08.home.new_table') });
    const rows = within(table).getAllByRole('row').slice(1);
    expect(rows.map((row) => row.getAttribute('data-finding'))).toEqual(
      [3, 4, 5, 6, 7].map((index) => finding(index).finding_state_id),
    );
    expect(
      rows.map((row) => row.querySelector('[data-access]')?.getAttribute('data-access')),
    ).toEqual(['review_queue', 'review_queue', 'review_queue', 'classify', 'classify']);
    expect(document.querySelector('[data-part="open_inbox"]')?.textContent).toBe(
      `${text('scr08.home.open_inbox_count')} (11)`,
    );
  });

  it('la cobertura llega cargada al abrir, sin «Desconocido», y una sola actualización', async () => {
    let calls = 0;
    later(
      http.get('http://localhost/zones/:zone_id/coverage/at', ({ params }) => {
        calls++;
        return HttpResponse.json({
          zone_id: params.zone_id,
          instant: INSTANT,
          state: 'not_observable',
          causes: ['no_communication'],
          layer: 'platform',
          clock_basis: 'platform',
          source_record_ids: [],
        });
      }),
    );
    await open();
    expect(calls).toBe(1);
    expect(screen.queryByRole('button', { name: text('scr08.home.load_coverage') })).toBeNull();
    const strip = screen.getByRole('region', { name: text('scr08.home.coverage') });
    expect(
      within(strip)
        .getAllByRole('group')
        .map((group) => group.getAttribute('aria-label')),
    ).toEqual([
      `${text('components_status.observability.group_label')}: ${text('components_status.observability.not_observable')}`,
    ]);
    // «No observable» no lleva la frase de «Observable» (S3-07).
    expect(strip.querySelector('[data-part="legend"]')).toBeNull();
    expect(
      screen.getAllByRole('button', { name: text('components_status.refresh.button') }),
    ).toHaveLength(1);
  });

  it('con zonas observables, la leyenda va una sola vez en la franja', async () => {
    await open({ plants: [plant([zone(1), zone(2), zone(3)])] });
    const strip = screen.getByRole('region', { name: text('scr08.home.coverage') });
    expect(
      [...strip.querySelectorAll('[data-part="legend"]')].map((legend) => legend.textContent),
    ).toEqual([text('components_status.observability.legend')]);
  });

  it.each([0, 50, 51])(
    'borde de %i zonas: hasta 50 tarjetas y 50 lecturas de cobertura',
    async (n) => {
      const requests: string[] = [];
      later(
        http.get('http://localhost/zones/:zone_id/coverage/at', ({ params, request }) => {
          requests.push(String(params.zone_id));
          return HttpResponse.json({
            zone_id: params.zone_id,
            instant: new URL(request.url).searchParams.get('instant'),
            state: 'observable',
            causes: [],
            layer: 'platform',
            clock_basis: 'platform',
            source_record_ids: [],
          });
        }),
      );
      const { container, clock, cache } = await open(
        { plants: [plant(Array.from({ length: n }, (_, i) => zone(i + 1)))] },
        [],
      );
      expect(screen.queryAllByRole('article')).toHaveLength(Math.min(n, 50));
      expect(container.querySelectorAll('[data-part="grouped-zones"] li')).toHaveLength(
        Math.max(n - 50, 0),
      );
      expect(new Set(requests).size).toBe(Math.min(n, 50));
      expect(requests).toHaveLength(Math.min(n, 50));
      // Auditada: no se repite sola con el refresco periódico de la página.
      await act(async () => {
        clock.advance(60_000);
        await settle();
      });
      await waitFor(() => {
        expect(cache.client.isFetching()).toBe(0);
      }, WAIT);
      expect(requests).toHaveLength(Math.min(n, 50));
    },
  );

  it('P9: un hallazgo de zona no productiva no se muestra ni se cuenta', async () => {
    const productive = zone(1);
    const blocked = zone(2);
    later(
      http.get('http://localhost/zones/:zone_id/gates', ({ params }) => {
        const value = gate(String(params.zone_id));
        if (params.zone_id === blocked.zone_id) {
          value.resulting_mode = 'no_capture';
          value.mounting.status = 'pending';
        }
        return HttpResponse.json(value);
      }),
    );
    await open({ plants: [plant([productive, blocked])] }, [
      inState(1, 'overdue', { overdue_days: 3, zone_id: blocked.zone_id }),
      inState(2, 'overdue', { overdue_days: 1, zone_id: productive.zone_id }),
      inState(3, 'received', { zone_id: blocked.zone_id }),
    ]);
    expect([count('overdue'), count('review'), count('due_week')]).toEqual(['1', '0', '0']);
    const overdue = screen.getByRole('region', { name: text('scr08.home.overdue_table') });
    expect(
      within(overdue)
        .getAllByRole('row')
        .slice(1)
        .map((row) => row.getAttribute('data-finding')),
    ).toEqual([finding(2).finding_state_id]);
    expect(document.querySelector('[data-part="new_empty"]')).not.toBeNull();
  });

  it('sin catalog.read no muestra ningún hallazgo: sin compuerta, nada (P9)', async () => {
    let gates = 0;
    later(
      http.get('http://localhost/zones/:zone_id/gates', ({ params }) => {
        gates++;
        return HttpResponse.json(gate(String(params.zone_id)));
      }),
    );
    await open({ permissions: COORDINATOR.filter((key) => key !== 'catalog.read') });
    expect(gates).toBe(0);
    expect(document.querySelector('[data-card]')).toBeNull();
    expect(screen.queryByRole('region', { name: text('scr08.home.new_table') })).toBeNull();
  });

  it('sin coverage.read no pide cobertura', async () => {
    let calls = 0;
    later(
      http.get('http://localhost/zones/:zone_id/coverage/at', () => {
        calls++;
        return HttpResponse.error();
      }),
    );
    await open({ permissions: COORDINATOR.filter((key) => key !== 'coverage.read') });
    expect(calls).toBe(0);
    expect(document.querySelector('[data-part="coverage_loading"]')).toBeNull();
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

  it('«vence esta semana» es una acción asignada con plazo de 7 días o menos', () => {
    const now = Date.parse(INSTANT);
    const gates = [{ zone_id: zone().zone_id, regression: 'current', gate: gate() }];
    const summary = homeSummary(
      page([
        inState(1, 'action_assigned', { due_at: at(7) }),
        inState(2, 'action_assigned', { due_at: at(7.5) }),
        inState(3, 'action_assigned', { due_at: at(-1) }),
        inState(4, 'overdue', { due_at: at(1) }),
      ]),
      gates,
      now,
    );
    expect(summary.dueThisWeek.map((item) => item.finding_state_id)).toEqual([
      finding(1).finding_state_id,
      finding(3).finding_state_id,
    ]);
  });

  it('recorrido real de inicio a bandeja y axe del panel sin violaciones', async () => {
    const env = await fixture({ permissions: COORDINATOR });
    server.use(http.get('http://localhost/loop/findings', () => HttpResponse.json(page(inbox()))));
    const { container } = render(
      <Routes>
        <Route path="/home" element={<Home />} />
        <Route path="/findings" element={<Inbox />} />
      </Routes>,
      { wrapper: env.Wrapper },
    );
    await screen.findByRole('region', { name: text('scr08.home.new_table') }, WAIT);
    await waitFor(() => {
      expect(env.cache.client.isFetching()).toBe(0);
    }, WAIT);
    expect(await axe(container)).toHaveNoViolations();
    const link = document.querySelector<HTMLAnchorElement>('[data-part="open_inbox"]');
    if (link === null) throw new Error('Falta «Abrir la bandeja completa»');
    fireEvent.click(link);
    await screen.findByRole('heading', { name: text('scr08.inbox.title'), level: 1 }, WAIT);
    expect(screen.getByRole('form', { name: text('scr08.inbox.filters') })).toBeDefined();
  });
});
