/**
 * UI-14 `KeysetTable` (BR-APP-25, 53, 57; PAT-APP-ESC-03): primera página de 50, «Cargar más»
 * con el cursor opaco de la unidad, sin «ir a la página N» ni total propio, filtros que no piden
 * nada hasta el botón o Intro (MSW cuenta las peticiones), conteo de vencidos en cabecera,
 * navegación por teclado entre filas, `axe` sin violaciones serias y marcas de rendimiento con
 * la hora del reloj de la prueba. Se comparan textos del catálogo y nombres accesibles exactos.
 */
import { render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import { server } from '../setup/msw';
import { FakeClock } from '../support/fakeClock';
import {
  KeysetTable,
  type KeysetColumn,
  type KeysetFilterField,
} from '@/components/KeysetTable/KeysetTable';
import type { KeysetPage, KeysetRequest } from '@/components/KeysetTable/keyset';
import catalog from '@/texts/components_structure.json';
import statusCatalog from '@/texts/components_status.json';

type CatalogKey = keyof typeof catalog;
const t = (key: CatalogKey): string => catalog[key];

/** Tope de espera holgado: no es lo que se prueba. */
const WAIT = { timeout: 10_000 };
const ORIGIN = 'http://localhost';

interface Finding {
  readonly finding_state_id: string;
  readonly zone: string;
}

const ALL: readonly Finding[] = Array.from({ length: 120 }, (_, index) => ({
  finding_state_id: `fs-${String(index).padStart(4, '0')}`,
  zone: index % 2 === 0 ? 'Línea 1' : 'Prensa 3',
}));

const columns: readonly KeysetColumn<Finding>[] = [
  { id: 'id', header: 'Hallazgo', cell: (row) => row.finding_state_id },
  { id: 'zone', header: 'Zona', cell: (row) => row.zone },
];

const filterFields: readonly KeysetFilterField[] = [
  { name: 'q', label: 'Buscar', kind: 'text' },
  {
    name: 'zone',
    label: 'Zona',
    kind: 'select',
    options: [
      { value: 'Línea 1', label: 'Línea 1' },
      { value: 'Prensa 3', label: 'Prensa 3' },
    ],
  },
];

/** Peticiones recibidas por el manejador de `GET /loop/findings`. */
let received: URL[] = [];

/** Bandeja simulada de U-04: `cursor` opaco (el último `finding_state_id`) y `limit` ≤ 200. */
function serveFindings(total?: number) {
  received = [];
  server.use(
    http.get(`${ORIGIN}/loop/findings`, ({ request }) => {
      const url = new URL(request.url);
      received.push(url);
      const limit = Number(url.searchParams.get('limit'));
      if (!Number.isInteger(limit) || limit < 1 || limit > 200) {
        return HttpResponse.json({ code: 'validation_failed' }, { status: 422 });
      }
      const zone = url.searchParams.get('zone');
      const after = url.searchParams.get('cursor');
      const rows = ALL.filter((row) => zone === null || row.zone === zone).filter(
        (row) => after === null || row.finding_state_id > after,
      );
      const items = rows.slice(0, limit);
      const last = items.at(-1);
      return HttpResponse.json({
        items,
        next_cursor: rows.length > limit && last !== undefined ? last.finding_state_id : null,
        ...(total === undefined ? {} : { total }),
      });
    }),
  );
}

/** Puerto de pantalla de prueba: traduce la petición de la tabla a la ruta de U-04. */
async function fetchFindings(
  request: KeysetRequest,
  signal: AbortSignal,
): Promise<KeysetPage<Finding>> {
  const url = new URL(`${ORIGIN}/loop/findings`);
  url.searchParams.set('limit', String(request.pageSize));
  if (request.cursor !== null) {
    for (const [key, value] of Object.entries(request.cursor)) url.searchParams.set(key, value);
  }
  for (const [key, value] of Object.entries(request.filters)) url.searchParams.set(key, value);
  const response = await fetch(url, { signal });
  if (!response.ok) throw new Error(String(response.status));
  const body = (await response.json()) as {
    items: Finding[];
    next_cursor: string | null;
    total?: number;
  };
  return {
    items: body.items,
    nextCursor: body.next_cursor === null ? null : { cursor: body.next_cursor },
    total: body.total ?? null,
  };
}

function renderTable(props: Partial<Parameters<typeof KeysetTable<Finding>>[0]> = {}) {
  const clock = new FakeClock(Date.UTC(2026, 9, 24, 12));
  const result = render(
    <main>
      <KeysetTable<Finding>
        caption="Bandeja"
        columns={columns}
        rowKey={(row) => row.finding_state_id}
        fetchPage={fetchFindings}
        clock={clock}
        filterFields={filterFields}
        {...props}
      />
    </main>,
  );
  return { ...result, clock };
}

async function rowsShown(count: number): Promise<HTMLElement[]> {
  await waitFor(() => {
    expect(screen.getAllByRole('row')).toHaveLength(count + 1);
  }, WAIT);
  return screen.getAllByRole('row').slice(1);
}

afterEach(() => {
  performance.clearMarks();
  performance.clearMeasures();
});

describe('UI-14 KeysetTable', { timeout: 30_000 }, () => {
  it('pide 50 por omisión, carga más con el cursor y termina sin número de página ni total', async () => {
    serveFindings();
    const user = userEvent.setup();
    renderTable();
    await rowsShown(50);
    expect(received).toHaveLength(1);
    expect(received[0]?.searchParams.get('limit')).toBe('50');
    expect(received[0]?.searchParams.has('cursor')).toBe(false);

    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    await rowsShown(100);
    expect(received[1]?.searchParams.get('cursor')).toBe('fs-0049');

    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    const rows = await rowsShown(120);
    expect(
      within(rows.at(-1) ?? document.body).getByRole('cell', { name: 'fs-0119' }),
    ).toBeTruthy();
    expect(
      screen.queryByRole('button', { name: t('components_structure.keyset.load_more') }),
    ).toBeNull();
    expect(screen.getByText(t('components_structure.keyset.end'))).toBeTruthy();
    for (const url of received) {
      expect([...url.searchParams.keys()].sort()).toEqual(
        url.searchParams.has('cursor') ? ['cursor', 'limit'] : ['limit'],
      );
    }
    expect(document.querySelector('[data-part="total"]')).toBeNull();
    expect(screen.queryByRole('link')).toBeNull();
  });

  it('la página pedida nunca pasa de 200', async () => {
    serveFindings();
    renderTable({ pageSize: 500 });
    await rowsShown(120);
    expect(received.map((url) => url.searchParams.get('limit'))).toEqual(['200']);
  });

  it('el total solo aparece si lo da el servidor', async () => {
    serveFindings(987);
    const { container } = renderTable();
    await rowsShown(50);
    expect(container.querySelector('[data-part="total"]')?.textContent).toBe(
      `${t('components_structure.keyset.total')}: 987`,
    );
  });

  it('escribir en un filtro no pide nada; el botón o Intro aplican todo el bloque', async () => {
    serveFindings();
    const user = userEvent.setup();
    renderTable();
    await rowsShown(50);
    expect(received).toHaveLength(1);

    await user.type(screen.getByRole('textbox', { name: 'Buscar' }), 'línea norte');
    await user.selectOptions(screen.getByRole('combobox', { name: 'Zona' }), 'Prensa 3');
    expect(received).toHaveLength(1);

    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.apply_filters') }),
    );
    await waitFor(() => {
      expect(received).toHaveLength(2);
    }, WAIT);
    expect(received[1]?.searchParams.get('q')).toBe('línea norte');
    expect(received[1]?.searchParams.get('zone')).toBe('Prensa 3');
    expect(received[1]?.searchParams.has('cursor')).toBe(false);

    await user.clear(screen.getByRole('textbox', { name: 'Buscar' }));
    await user.type(screen.getByRole('textbox', { name: 'Buscar' }), 'otra');
    expect(received).toHaveLength(2);
    await user.keyboard('{Enter}');
    await waitFor(() => {
      expect(received).toHaveLength(3);
    }, WAIT);
    expect(received[2]?.searchParams.get('q')).toBe('otra');
    expect(received[2]?.searchParams.get('zone')).toBe('Prensa 3');
  });

  it('el conteo de vencidos sigue en la cabecera con cualquier filtro', async () => {
    serveFindings();
    const user = userEvent.setup();
    const { container } = renderTable({ overdueCount: 7 });
    await rowsShown(50);
    const overdue = () => container.querySelector('header [data-part="overdue"]')?.textContent;
    expect(overdue()).toBe(`${t('components_structure.keyset.overdue')}: 7`);
    await user.selectOptions(screen.getByRole('combobox', { name: 'Zona' }), 'Línea 1');
    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.apply_filters') }),
    );
    await waitFor(() => {
      expect(received).toHaveLength(2);
    }, WAIT);
    expect(overdue()).toBe(`${t('components_structure.keyset.overdue')}: 7`);
  });

  it('actualizar vuelve a la primera página', async () => {
    serveFindings();
    const user = userEvent.setup();
    renderTable();
    await rowsShown(50);
    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    await rowsShown(100);
    await user.click(
      screen.getByRole('button', { name: statusCatalog['components_status.refresh.button'] }),
    );
    await rowsShown(50);
    expect(received.at(-1)?.searchParams.has('cursor')).toBe(false);
  });

  it('un fallo de la página siguiente conserva las filas y deja reintentar', async () => {
    serveFindings();
    const user = userEvent.setup();
    renderTable();
    await rowsShown(50);
    server.use(
      http.get(`${ORIGIN}/loop/findings`, () => HttpResponse.json({}, { status: 503 }), {
        once: true,
      }),
    );
    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    expect((await screen.findByRole('alert', {}, WAIT)).textContent).toBe(
      t('components_structure.keyset.error_more'),
    );
    expect(screen.getAllByRole('row')).toHaveLength(51);
    await user.click(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    await rowsShown(100);
    expect(screen.queryByRole('alert')).toBeNull();
  });

  it('un cursor que no es de la unidad hace fallar la página en lugar de seguir', async () => {
    renderTable({
      filterFields: [],
      fetchPage: () => Promise.resolve({ items: ALL.slice(0, 50), nextCursor: { offset: '50' } }),
    });
    expect((await screen.findByRole('alert', {}, WAIT)).textContent).toBe(
      t('components_structure.keyset.error'),
    );
    expect(
      screen.queryByRole('button', { name: t('components_structure.keyset.load_more') }),
    ).toBeNull();
  });

  it('las filas se recorren por teclado: un solo punto de tabulación, flechas, Inicio, Fin e Intro', async () => {
    serveFindings();
    const user = userEvent.setup();
    const activated: string[] = [];
    renderTable({
      filterFields: [],
      onRowActivate: (row) => {
        activated.push(row.finding_state_id);
      },
    });
    const rows = await rowsShown(50);
    expect(rows.filter((row) => row.tabIndex === 0)).toHaveLength(1);

    await user.tab(); // «Actualizar»
    await user.tab();
    expect(document.activeElement).toBe(rows[0]);
    await user.keyboard('{ArrowDown}{ArrowDown}');
    expect(document.activeElement).toBe(rows[2]);
    await user.keyboard('{ArrowUp}');
    expect(document.activeElement).toBe(rows[1]);
    await user.keyboard('{End}');
    expect(document.activeElement).toBe(rows[49]);
    await user.keyboard('{ArrowDown}');
    expect(document.activeElement).toBe(rows[49]);
    await user.keyboard('{Home}');
    expect(document.activeElement).toBe(rows[0]);
    await user.keyboard('{ArrowDown}{Enter}');
    expect(activated).toEqual(['fs-0001']);

    await user.tab();
    expect(document.activeElement).toBe(
      screen.getByRole('button', { name: t('components_structure.keyset.load_more') }),
    );
    await user.tab({ shift: true });
    expect(document.activeElement).toBe(rows[1]);
  });

  it('axe sin violaciones con filtros, filas y cabecera', async () => {
    serveFindings(120);
    const { container } = renderTable({ overdueCount: 3 });
    await rowsShown(50);
    const results = await axe(container);
    const serious = results.violations.filter(
      (violation) => violation.impact === 'serious' || violation.impact === 'critical',
    );
    expect(serious).toEqual([]);
    expect(results).toHaveNoViolations();
  });

  it('deja las marcas de rendimiento de 50 filas con la hora del reloj de la prueba', async () => {
    serveFindings();
    const { clock } = renderTable();
    await rowsShown(50);
    const [received_] = performance.getEntriesByName('keyset_table.page_received', 'mark');
    const [committed] = performance.getEntriesByName('keyset_table.rows_committed', 'mark');
    expect(received_?.startTime).toBe(clock.now());
    expect(committed?.startTime).toBe(clock.now());
    expect((committed as PerformanceMark | undefined)?.detail).toEqual({ rows: 50 });
    expect(performance.getEntriesByName('keyset_table.render', 'measure')).toHaveLength(1);
  });
});
