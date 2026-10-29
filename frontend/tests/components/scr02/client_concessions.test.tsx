/**
 * `/concessions` (SCR-02, cliente; RV-04; IF-23; BR-APP-54; A-48; H-56):
 *
 * - Lista con alcance, motivo, concedida, vence, estado y quién revocó, y «actualizado hace X».
 * - Las consultas del proveedor (`GET /concessions/{id}/queries`, lectura auditada) se piden solo
 *   al abrir el detalle o pulsar «Actualizar»: cero peticiones en 5 min simulados.
 * - Cada consulta se etiqueta «acceso o intento», también la que el servidor rechazó: el
 *   `provider_query` se escribe antes de la ruta y `ProviderQueryItem` no trae campo de resultado.
 * - Revocar exige confirmación explícita; un `conflict` se ancla a la confirmación.
 * - `axe` sin violaciones serias.
 */
import { screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { describe, expect, it } from 'vitest';

import { text } from '@/texts';
import { label } from '@/texts/labels';

import { server } from '../../msw/server';
import {
  advance,
  apiError,
  clientConcession,
  CONCESSION,
  expectNoSerious,
  mountAt,
  nth,
  ORGANIZATION,
  ORIGIN,
  OTHER_CONCESSION,
  PLANT,
  providerQuery,
  seriousViolations,
  WAIT,
} from './support';

type User = ReturnType<typeof userEvent.setup>;

const FIVE_MINUTES = 5 * 60_000;
const PLANT_SCOPE = `${label('scope_level', 'plant')}: Planta sintética`;
const ORGANIZATION_SCOPE = `${label('scope_level', 'organization')}: Organización sintética`;

/** Cuenta las peticiones a las consultas de cada concesión y responde con `queries`. */
function countQueries(queries: unknown[] = [providerQuery()]) {
  const seen: string[] = [];
  server.use(
    http.get(`${ORIGIN}/concessions/:concessionId/queries`, ({ params }) => {
      seen.push(String(params.concessionId));
      return HttpResponse.json({ queries, next_after: null });
    }),
  );
  return seen;
}

async function listTable() {
  const region = await screen.findByRole('region', { name: text('scr02.client.list') }, WAIT);
  await waitFor(() => {
    expect(within(region).getAllByRole('row').length).toBeGreaterThan(1);
  }, WAIT);
  return region;
}

async function openDetail(user: User, scope: string) {
  await user.click(
    await screen.findByRole('button', { name: `${text('scr02.client.open')}: ${scope}` }, WAIT),
  );
  return screen.findByRole('region', { name: text('scr02.detail.title') }, WAIT);
}

describe('/concessions', { timeout: 60_000 }, () => {
  it('lista alcance, motivo, concedida, vence, estado y quién revocó', async () => {
    countQueries();
    await mountAt('/concessions', {
      concessions: [
        clientConcession(),
        clientConcession({
          concession_id: OTHER_CONCESSION,
          scope_level: 'organization',
          scope_id: ORGANIZATION,
          reason: null,
          status: 'revoked',
          revoked_at: '2026-01-01T10:00:00.000Z',
          revoked_by_side: 'client',
        }),
      ],
    });
    const region = await listTable();
    expect(within(region).getByText(text('components_status.refresh.on_demand'))).toBeDefined();
    const rows = within(region).getAllByRole('row');
    expect(rows).toHaveLength(3);
    const headers = within(nth(rows, 0))
      .getAllByRole('columnheader')
      .map((cell) => cell.textContent);
    expect(headers).toEqual([
      text('scr02.col.scope'),
      text('scr02.col.reason'),
      text('scr02.col.granted_at'),
      text('scr02.col.expires_at'),
      text('scr02.col.status'),
      text('scr02.col.revoked_by'),
      text('scr02.col.actions'),
    ]);
    await waitFor(() => {
      expect(within(nth(rows, 1)).getByText(PLANT_SCOPE)).toBeDefined();
    }, WAIT);
    const active = within(region).getAllByRole('row')[1];
    const revoked = within(region).getAllByRole('row')[2];
    if (active === undefined || revoked === undefined) throw new Error('faltan filas');
    expect(within(active).getByText('Soporte del montaje de la planta sintética')).toBeDefined();
    expect(within(active).getByText(label('concession_status', 'active'))).toBeDefined();
    expect(within(active).getByText(text('scr02.revoked_by.none'))).toBeDefined();
    expect(within(revoked).getByText(ORGANIZATION_SCOPE)).toBeDefined();
    expect(within(revoked).getByText(text('scr02.reason.none'))).toBeDefined();
    expect(within(revoked).getByText(label('concession_status', 'revoked'))).toBeDefined();
    expect(within(revoked).getByText(text('scr02.revoked_by.client'))).toBeDefined();
  });

  it('VIG-366: el gerente con alcance de planta pide por su planta y ve la lista, no un error', async () => {
    const { requests } = await mountAt('/concessions', { plantScoped: true });
    await listTable();
    expect(screen.queryByText(text('components_structure.keyset.error'))).toBeNull();
    expect(requests.concessionPlants).toEqual([PLANT]);
  });

  it('con alcance de organización pide una sola vez, sin plant_id', async () => {
    const { requests } = await mountAt('/concessions');
    await listTable();
    expect(requests.concessionPlants).toEqual([null]);
  });

  it('sin concesiones muestra el texto propio', async () => {
    await mountAt('/concessions', { concessions: [] });
    expect(await screen.findByText(text('scr02.client.empty'), {}, WAIT)).toBeDefined();
  });

  it('las consultas se piden solo al abrir el detalle o al pulsar actualizar (cero en 5 min)', async () => {
    const seen = countQueries();
    const user = userEvent.setup();
    const fixture = await mountAt('/concessions');
    await listTable();
    advance(fixture.clock, FIVE_MINUTES);
    advance(fixture.clock, FIVE_MINUTES);
    expect(seen).toEqual([]);

    const detail = await openDetail(user, PLANT_SCOPE);
    const queries = await within(detail).findByRole(
      'region',
      { name: text('scr02.queries.list') },
      WAIT,
    );
    await waitFor(() => {
      expect(within(queries).getAllByRole('row')).toHaveLength(2);
    }, WAIT);
    expect(seen).toEqual([CONCESSION]);

    // 5 min simulados con el detalle abierto: ninguna petición periódica.
    for (let minute = 0; minute < 5; minute += 1) advance(fixture.clock, 60_000);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(seen).toEqual([CONCESSION]);

    await user.click(
      within(queries).getByRole('button', { name: text('components_status.refresh.button') }),
    );
    await waitFor(() => {
      expect(seen).toEqual([CONCESSION, CONCESSION]);
    }, WAIT);
    for (let minute = 0; minute < 5; minute += 1) advance(fixture.clock, 60_000);
    await new Promise((resolve) => setTimeout(resolve, 0));
    expect(seen).toEqual([CONCESSION, CONCESSION]);
  });

  it('con más de una página, la primera se pinta y «Cargar más» pide la siguiente con su cursor', async () => {
    // VIG-365: con `after` como clave del cursor, KeysetTable rechazaba la primera página con
    // continuación y la lista entera quedaba en «No se pudo cargar».
    const second = providerQuery({
      record_id: '0190a1b2-0000-7000-8000-0000000000e3',
      occurred_at: '2026-01-01T09:10:00.000Z',
    });
    const afters: (string | null)[] = [];
    server.use(
      http.get(`${ORIGIN}/concessions/:concessionId/queries`, ({ request }) => {
        const after = new URL(request.url).searchParams.get('after');
        afters.push(after);
        return after === null
          ? HttpResponse.json({ queries: [providerQuery()], next_after: 'c2lndWllbnRl' })
          : HttpResponse.json({ queries: [second], next_after: null });
      }),
    );
    const user = userEvent.setup();
    await mountAt('/concessions');
    await listTable();
    const detail = await openDetail(user, PLANT_SCOPE);
    const queries = await within(detail).findByRole(
      'region',
      { name: text('scr02.queries.list') },
      WAIT,
    );
    await waitFor(() => {
      expect(within(queries).getAllByRole('row')).toHaveLength(2);
    }, WAIT);
    expect(within(queries).queryByRole('alert')).toBeNull();
    await user.click(
      within(queries).getByRole('button', { name: text('components_structure.keyset.load_more') }),
    );
    await waitFor(() => {
      expect(within(queries).getAllByRole('row')).toHaveLength(3);
    }, WAIT);
    expect(afters).toEqual([null, 'c2lndWllbnRl']);
  });

  it('una consulta rechazada se etiqueta «acceso o intento», nunca como datos entregados', async () => {
    // El servidor escribe el provider_query antes de la ruta (A-48): la consulta rechazada llega
    // con los mismos campos que la atendida. Estos son exactamente los que devuelve.
    const rejected = providerQuery({
      record_id: '0190a1b2-0000-7000-8000-0000000000e2',
      operation: 'write',
      method: 'POST',
      resource: '/zones/{zone_id}/standards',
      occurred_at: '2026-01-01T09:05:00.000Z',
    });
    expect(Object.keys(rejected).sort()).toEqual([
      'method',
      'occurred_at',
      'operation',
      'plant_id',
      'provider_user_id',
      'reason',
      'received_at',
      'record_id',
      'resource',
    ]);
    countQueries([providerQuery(), rejected]);
    const user = userEvent.setup();
    await mountAt('/concessions');
    await listTable();
    const detail = await openDetail(user, PLANT_SCOPE);
    expect(within(detail).getByText(text('scr02.queries.notice'))).toBeDefined();
    const queries = await within(detail).findByRole(
      'region',
      { name: text('scr02.queries.list') },
      WAIT,
    );
    await waitFor(() => {
      expect(within(queries).getAllByRole('row')).toHaveLength(3);
    }, WAIT);
    const rows = within(queries).getAllByRole('row').slice(1);
    const kinds = rows.map(
      (row) =>
        within(row).getByText(text('scr02.queries.kind_attempt')).getAttribute('data-text-key') ??
        '',
    );
    expect(kinds).toEqual(['scr02.queries.kind_attempt', 'scr02.queries.kind_attempt']);
    const second = nth(rows, 1);
    expect(within(second).getByText(text('scr02.queries.operation.write'))).toBeDefined();
    expect(within(second).getByText('/zones/{zone_id}/standards')).toBeDefined();
    // Ningún texto del panel presenta una consulta como entrega de datos.
    const keys = [...detail.querySelectorAll('[data-text-key]')].map((node) =>
      node.getAttribute('data-text-key'),
    );
    expect(keys.filter((key) => key !== null && /deliver|entreg/.test(key))).toEqual([]);
  });

  it('revocar exige confirmación explícita y vuelve a pedir la lista', async () => {
    countQueries();
    const revokes: string[] = [];
    server.use(
      http.post(`${ORIGIN}/concessions/:concessionId/revoke`, ({ params }) => {
        revokes.push(String(params.concessionId));
        return HttpResponse.json(
          clientConcession({
            status: 'revoked',
            revoked_at: '2026-01-01T12:00:00.000Z',
            revoked_by_side: 'client',
          }),
        );
      }),
    );
    const user = userEvent.setup();
    const fixture = await mountAt('/concessions');
    await listTable();
    const detail = await openDetail(user, PLANT_SCOPE);

    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.open') }));
    expect(revokes).toEqual([]);
    expect(within(detail).getByText(text('scr02.revoke.question'))).toBeDefined();
    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.cancel') }));
    expect(revokes).toEqual([]);

    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.open') }));
    const listed = fixture.requests.concessions;
    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.confirm') }));
    expect(await within(detail).findByText(text('scr02.revoke.done'), {}, WAIT)).toBeDefined();
    expect(revokes).toEqual([CONCESSION]);
    expect(within(detail).getByText(label('concession_status', 'revoked'))).toBeDefined();
    await waitFor(() => {
      expect(fixture.requests.concessions).toBe(listed + 1);
    }, WAIT);
  });

  it('un conflict al revocar se ancla a la confirmación con el mensaje del servidor', async () => {
    countQueries();
    server.use(
      http.post(`${ORIGIN}/concessions/:concessionId/revoke`, () =>
        apiError('conflict', 409, 'La concesión ya está cerrada.'),
      ),
    );
    const user = userEvent.setup();
    await mountAt('/concessions', { concessions: [clientConcession({ status: 'expired' })] });
    await listTable();
    const detail = await openDetail(user, PLANT_SCOPE);
    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.open') }));
    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.confirm') }));
    const group = within(detail).getByRole('group', { name: text('scr02.revoke.open') });
    const alert = await within(group).findByRole('alert', {}, WAIT);
    expect(alert.getAttribute('data-code')).toBe('conflict');
    expect(group.getAttribute('aria-describedby')?.split(' ')).toContain(alert.id);
  });

  it('axe sin violaciones serias en la lista y en el detalle con la confirmación', async () => {
    countQueries([providerQuery()]);
    const user = userEvent.setup();
    const fixture = await mountAt('/concessions');
    await listTable();
    expectNoSerious(await seriousViolations(fixture.container));
    const detail = await openDetail(user, PLANT_SCOPE);
    const queries = await within(detail).findByRole(
      'region',
      { name: text('scr02.queries.list') },
      WAIT,
    );
    await waitFor(() => {
      expect(within(queries).getAllByRole('row')).toHaveLength(2);
    }, WAIT);
    await user.click(within(detail).getByRole('button', { name: text('scr02.revoke.open') }));
    expectNoSerious(await seriousViolations(fixture.container));
  });
});
