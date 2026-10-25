/**
 * Arnés de las pruebas de cuenta y usuarios de SCR-01 (VIG-305): sesión real cargada con un
 * `GET /me` sintético, transporte real con el reloj simulado, caché de consultas real, enrutador
 * en memoria con `/account`, `/admin/users` y una ruta ajena para «salir», y MSW. Datos
 * exclusivamente sintéticos.
 */
import { cleanup, render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';

import AccountPage from '@/pages/scr01/account';
import UsersPage from '@/pages/scr01/users';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { ManagedUser } from '@/ports/scr01Admin';
import type { MeResponse } from '@/ports/shared';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';

import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse, USER_A } from '../../support/session';

export const ORIGIN = 'http://localhost';
const INSTANT = '2026-01-01T12:00:00.000Z';
/** Tope holgado de las esperas: no es lo que se prueba. */
export const WAIT = { timeout: 10_000 };

export const ORGANIZATION = '0190a1b2-0000-7000-8000-0000000000f1';
export const PLANT = '0190a1b2-0000-7000-8000-0000000000a1';
const ZONE = '0190a1b2-0000-7000-8000-0000000000b1';
export const USER_B = '0190a1b2-0000-7000-8000-0000000000c2';
export const ADMIN_ASSIGNMENT = '0190a1b2-0000-7000-8000-0000000000d1';

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

function hierarchy() {
  return {
    organization: {
      organization_id: ORGANIZATION,
      code: 'ORG-SINT',
      name: 'Organización sintética',
    },
    plants: [
      {
        plant_id: PLANT,
        code: 'PLANTA-SINT',
        name: 'Planta sintética',
        timezone: 'America/Bogota',
        country: 'CO',
        status: 'active',
        data_region: 'co',
        zones: [
          { zone_id: ZONE, plant_id: PLANT, code: 'Z-1', name: 'Zona sintética', node_id: null },
        ],
      },
    ],
    nodes: [],
  };
}

export function managedUser(overrides: Partial<ManagedUser> = {}): ManagedUser {
  return {
    user_id: USER_B,
    email: 'cuenta.sintetica@ejemplo.test',
    display_name: 'Cuenta sintética B',
    professional_license: null,
    status: 'active',
    second_factor_required: true,
    second_factor_enrolled: true,
    assignments: [
      {
        assignment_id: ADMIN_ASSIGNMENT,
        role: 'administrator',
        scope_level: 'organization',
        scope_id: ORGANIZATION,
      },
    ],
    ...overrides,
  };
}

/** El elemento `index` de una lista; si falta, la prueba falla nombrándolo. */
export function nth<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`falta el elemento ${String(index)}`);
  return item;
}

/** Cuerpo de error cerrado de la plataforma (`ApiErrorBody`), con el mensaje genérico de U-02. */
export function apiError(code: string, status: number, messageEs: string) {
  return HttpResponse.json(
    {
      code,
      detail_code: null,
      message_es: messageEs,
      correlation_id: '0190a1b2-0000-7000-8000-0000000000e1',
      retry_after_seconds: null,
    },
    { status },
  );
}

export async function mountAt(
  path: string,
  options: {
    readonly me?: Partial<MeResponse>;
    readonly users?: readonly ManagedUser[];
  } = {},
) {
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
  const me: MeResponse = {
    ...meResponse({
      idleExpiresAt: clock.now() + 1_800_000,
      absoluteExpiresAt: clock.now() + 8 * 3_600_000,
      userId: USER_A,
      permissions: ['users.manage', 'roles.manage'],
    }),
    ...options.me,
  };
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
  installTransport(createTransport({ clock, connection, origin: ORIGIN }));
  server.use(
    http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(hierarchy())),
    http.get(`${ORIGIN}/users`, () =>
      HttpResponse.json({ users: options.users ?? [managedUser()], next_after: null }),
    ),
  );
  const router = createMemoryRouter(
    [
      { path: '/account', Component: AccountPage },
      { path: '/admin/users', Component: UsersPage },
      {
        path: '/elsewhere',
        element: (
          <main>
            <h1>Otra pantalla sintética</h1>
          </main>
        ),
      },
    ],
    { initialEntries: [path] },
  );
  const view = render(
    <StrictMode>
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <RouterProvider router={router} />
        </ScreenQueryProvider>
      </SessionProvider>
    </StrictMode>,
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  return { clock, cache, router, me, container: view.container };
}
