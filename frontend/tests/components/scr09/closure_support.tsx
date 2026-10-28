/** Proveedores reales, reloj inyectado y expediente exclusivamente sintético de VIG-328. */
import { cleanup, render, screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';
import Closure from '@/pages/scr09/loop/closure';
import Unclosable from '@/pages/scr09/loop/unclosable';
import Reopening from '@/pages/scr09/loop/reopening';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { Detail, LedgerRecord } from '@/ports/scr09Closure';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';
import { exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { detail as baseDetail, FINDING, USER, WAIT } from './classify_support';

export { FINDING, USER, WAIT };
export const START = Date.parse('2026-10-25T02:00:00.000Z'); // En Bogotá aún es 24 de octubre.
export const INSTANT = '2026-10-25T02:00:00.000Z';
export const ACTION = '0190a1b2-7777-7000-8000-000000000001';
export const ATTACHMENT = '0190a1b2-8888-7000-8000-000000000001';
export const DECLARATION = '0190a1b2-9999-7000-8000-000000000001';
export const DESCRIPTION = 'Instalar un resguardo fijo sobre el acceso lateral de la prensa';
export const LICENSE = 'LICENCIA-SINTETICA';
export const PERMISSIONS = [
  'findings.read',
  'actions.manage',
  'findings.close_and_sign',
  'closure_attachments.read',
  'hierarchy.read',
];
export const detail: Detail = baseDetail({
  subject_id: FINDING,
  actions: [
    {
      action_id: ACTION,
      record_id: ACTION,
      action_type: 'engineering',
      action_description: DESCRIPTION,
      expected_outcome: null,
      state: 'overdue',
      assigned_at: INSTANT,
      due_at: INSTANT,
      root_cause_analysis_id: null,
      counts_as_overdue: true,
    },
  ],
});
const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export function ledgerRecord(
  type = 'closure',
  content: Record<string, unknown> = {},
): LedgerRecord {
  const page = exampleFor('list_records_ledger_records_get') as { items: LedgerRecord[] };
  const base = page.items[0];
  if (base === undefined) throw new Error('Falta el ejemplo de expediente.');
  return {
    ...base,
    record_id: ACTION,
    record_type: type,
    received_at: INSTANT,
    plant_id: detail.plant_id,
    scope: { plant_id: detail.plant_id, zone_id: detail.zone_id, node_id: null },
    content: {
      subject_id: FINDING,
      closure_id: ACTION,
      closure_description: DESCRIPTION,
      signed_at: INSTANT,
      signer: { display_name: 'Firma sintética', professional_license: LICENSE },
      ...content,
    },
  };
}

export async function renderClosure(
  route: 'closure' | 'unclosable' | 'reopening' = 'closure',
  {
    permissions = PERMISSIONS,
    records = [ledgerRecord()],
    now = START,
    license = LICENSE,
    entryState = null,
  }: {
    permissions?: string[];
    records?: LedgerRecord[];
    now?: number;
    license?: string | null;
    /** Estado de la navegación con que se llega (la revisión destacada del lazo, VIG-363). */
    entryState?: unknown;
  } = {},
) {
  const clock = new FakeClock(now);
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => true,
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = {
    ...meResponse({
      idleExpiresAt: now + 1_800_000,
      absoluteExpiresAt: now + 8 * 3_600_000,
      userId: USER,
      permissions,
    }),
    professional_license: license,
  };
  const path = `/findings/${FINDING}/loop/${route}`;
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => path,
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(
    createTransport({ clock, connection, origin: 'http://localhost', devLog: () => undefined }),
  );
  const history = records.map((record) => ({
    ...record,
    organization_id: me.organization.organization_id,
  }));
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({
        organization: me.organization,
        nodes: [],
        plants: [
          {
            plant_id: detail.plant_id,
            code: 'PLANTA-SINT',
            name: 'Planta sintética',
            timezone: 'America/Bogota',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [
              {
                zone_id: detail.zone_id,
                plant_id: detail.plant_id,
                code: 'Z-1',
                name: 'Zona sintética',
                node_id: null,
              },
            ],
          },
        ],
      }),
    ),
    http.get(`http://localhost/loop/findings/${FINDING}`, () => HttpResponse.json(detail)),
    http.get('http://localhost/ledger/records', () =>
      HttpResponse.json({ items: history, next_cursor: null }),
    ),
    http.get('http://localhost/loop/plants/:plant_id/configuration', () =>
      HttpResponse.json({
        ...(exampleFor('configuration_loop_plants__plant_id__configuration_get') as object),
        action_deadline_default_days: 15,
        action_deadline_min_days: 1,
        action_deadline_max_days: 90,
      }),
    ),
    http.get('http://localhost/plants/:plant_id/policy', () =>
      HttpResponse.json({
        loaded: true,
        version: 1,
        criteria_summary_es: 'Política sintética de planta',
        signed_by_display_name: 'Firmante sintético',
      }),
    ),
  );
  const router = createMemoryRouter(
    [
      {
        path: '/findings/:finding_state_id/loop/:subroute',
        element:
          route === 'closure' ? (
            <Closure />
          ) : route === 'unclosable' ? (
            <Unclosable />
          ) : (
            <Reopening />
          ),
      },
    ],
    { initialEntries: [{ pathname: path, state: entryState }] },
  );
  disposers.push(() => {
    router.dispose();
    session.dispose();
    cache.dispose();
  });
  const view = render(
    <SessionProvider store={session}>
      <ScreenQueryProvider cache={cache}>
        <RouterProvider router={router} />
      </ScreenQueryProvider>
    </SessionProvider>,
  );
  return {
    ...view,
    clock,
    connection,
    cache,
    history,
    organizationId: me.organization.organization_id,
  };
}
export async function load() {
  const user = userEvent.setup();
  await user.click(await screen.findByRole('button', { name: text('scr09.closure.load') }, WAIT));
  await screen.findByRole('region', { name: text('scr09.closure.history') }, WAIT);
  await waitFor(() => {
    expectLoadReady();
  }, WAIT);
  return user;
}
function expectLoadReady() {
  if (screen.getByRole<HTMLButtonElement>('button', { name: text('scr09.closure.load') }).disabled)
    throw new Error('Consulta aún en curso.');
}
export const reasonName = (key: string) =>
  `${text(key)} (${text('components_forms.reason.required')})`;
