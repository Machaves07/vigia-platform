/** Sesión, caché y transporte reales sobre MSW; todos los datos son sintéticos. */
import { cleanup, render } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach } from 'vitest';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import {
  LOOP_FIELDS,
  type Hierarchy,
  type LoopConfiguration,
  type OrganizationSettings,
} from '@/ports/scr01Hierarchy';
import { ScreenQueryProvider, createScreenQueryCache } from '@/state/query';
import { SessionProvider, createSessionStore } from '@/state/session';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';

export const PLANT = '0190a1b2-0000-7000-8000-000000000101';
export const OTHER_PLANT = '0190a1b2-0000-7000-8000-000000000102';
export const ZONE = '0190a1b2-1111-7000-8000-000000000101';
const NODE = '0190a1b2-2222-7000-8000-000000000101';
const INSTANT = '2026-03-02T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };

export function hierarchy(): Hierarchy {
  return {
    organization: {
      organization_id: '0190a1b2-0000-7000-8000-0000000000f1',
      code: 'ORG-SINT',
      name: 'Organización sintética',
      kind: 'client',
      status: 'active',
    },
    plants: [
      {
        plant_id: PLANT,
        code: 'PLANTA-SINT',
        name: 'Planta sintética',
        country: 'CO',
        data_region: 'co',
        timezone: 'America/Bogota',
        status: 'active',
        zones: [
          { zone_id: ZONE, plant_id: PLANT, code: 'Z-SINT', name: 'Zona sintética', node_id: NODE },
        ],
      },
    ],
    nodes: [
      {
        node_id: NODE,
        plant_id: PLANT,
        code: 'NODO-SINT',
        status: 'active',
        live_view_local_url: null,
      },
    ],
  };
}

export function configuration(plantId = PLANT): LoopConfiguration {
  // Valores sintéticos de lectura: no constituyen los límites de validación de escritura.
  const integers = Object.fromEntries(LOOP_FIELDS.map((field) => [field, 3])) as Pick<
    LoopConfiguration,
    (typeof LOOP_FIELDS)[number]
  >;
  return {
    ...integers,
    plant_id: plantId,
    timezone: 'America/Bogota',
    business_calendar: { working_weekdays: [1, 2, 3, 4, 5], non_working_dates: ['2026-03-23'] },
    version: 1,
    changed_at: INSTANT,
    changed_by: {
      user_id: '0190a1b2-3333-7000-8000-000000000101',
      role_in_use: 'administrator',
      display_name: 'Firmante sintético',
    },
    change_reason: 'initial_defaults',
  };
}

export function settings(): OrganizationSettings {
  return { ...hierarchy().organization, concession_max_days: 30, concession_default_days: 7 };
}

export function apiError(code: string, status: number, detail_code: string | null = null) {
  return HttpResponse.json(
    {
      code,
      detail_code,
      message_es: 'Mensaje sintético del servidor.',
      correlation_id: '0190a1b2-7777-7000-8000-000000000101',
      retry_after_seconds: null,
    },
    { status },
  );
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
});

export async function renderAdmin(element: ReactNode) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => true,
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 28_800_000,
    permissions: ['hierarchy.read', 'hierarchy.manage', 'organization.settings'],
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => '/admin/hierarchy',
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  server.use(
    http.get('http://localhost/hierarchy', () => HttpResponse.json(hierarchy())),
    http.get('http://localhost/organization/settings', () => HttpResponse.json(settings())),
    http.get('http://localhost/loop/plants/:plantId/configuration', ({ params }) =>
      HttpResponse.json(configuration(String(params.plantId))),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  const view = render(
    <MemoryRouter>
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>{element}</ScreenQueryProvider>
      </SessionProvider>
    </MemoryRouter>,
  );
  return { ...view, clock, cache };
}
