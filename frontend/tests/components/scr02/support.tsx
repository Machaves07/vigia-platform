/**
 * Arnés de las pruebas de SCR-02 (VIG-322): sesión real cargada con un `GET /me` sintético,
 * transporte real con el reloj simulado y la cabecera de concesión conectada como la conecta el
 * arranque (`transportOptions`), caché de consultas real, `ActiveConcession` real, enrutador en
 * memoria con los dos paneles y MSW. Datos exclusivamente sintéticos.
 */
import { act, cleanup, render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, expect } from 'vitest';
import { axe } from 'vitest-axe';

import ClientPage from '@/pages/scr02/client';
import ProviderPage from '@/pages/scr02/provider';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { ClientConcession, ProviderConcession, ProviderQuery } from '@/ports/scr02Concessions';
import type { MeResponse } from '@/ports/shared';
import { ConcessionProvider, createConcessionStore, transportOptions } from '@/state/concession';
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
const PROVIDER_ORGANIZATION = '0190a1b2-0000-7000-8000-0000000000f9';
export const PLANT = '0190a1b2-0000-7000-8000-0000000000a1';
export const CONCESSION = '0190a1b2-0000-7000-8000-0000000000c1';
export const OTHER_CONCESSION = '0190a1b2-0000-7000-8000-0000000000c2';
const PROVIDER_USER = '0190a1b2-0000-7000-8000-0000000000b9';

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
        zones: [],
      },
    ],
    nodes: [],
  };
}

export function clientConcession(overrides: Partial<ClientConcession> = {}): ClientConcession {
  return {
    concession_id: CONCESSION,
    provider_organization_id: PROVIDER_ORGANIZATION,
    provider_user_id: PROVIDER_USER,
    scope_level: 'plant',
    scope_id: PLANT,
    reason: 'Soporte del montaje de la planta sintética',
    granted_at: '2026-01-01T08:00:00.000Z',
    expires_at: '2026-01-02T08:00:00.000Z',
    status: 'active',
    revoked_at: null,
    revoked_by_side: null,
    ...overrides,
  };
}

export function providerConcession(
  overrides: Partial<ProviderConcession> = {},
): ProviderConcession {
  return {
    concession_id: CONCESSION,
    client_organization_id: ORGANIZATION,
    scope_level: 'plant',
    scope_id: PLANT,
    granted_at: '2026-01-01T08:00:00.000Z',
    expires_at: '2026-01-02T08:00:00.000Z',
    status: 'active',
    revoked_at: null,
    revoked_by_side: null,
    ...overrides,
  };
}

export function providerQuery(overrides: Partial<ProviderQuery> = {}): ProviderQuery {
  return {
    record_id: '0190a1b2-0000-7000-8000-0000000000e1',
    received_at: '2026-01-01T09:00:00.000Z',
    plant_id: PLANT,
    provider_user_id: PROVIDER_USER,
    operation: 'read',
    method: 'GET',
    resource: '/zones/{zone_id}/catalog',
    occurred_at: '2026-01-01T09:00:00.000Z',
    reason: 'Soporte del montaje de la planta sintética',
    ...overrides,
  };
}

/** Cuerpo de error cerrado de la plataforma (`ApiErrorBody`). */
export function apiError(code: string, status: number, messageEs: string) {
  return HttpResponse.json(
    {
      code,
      detail_code: null,
      message_es: messageEs,
      correlation_id: '0190a1b2-0000-7000-8000-0000000000e9',
      retry_after_seconds: null,
    },
    { status },
  );
}

/** `GET /me` del gerente del cliente (`concessions.read`, `concessions.revoke`). */
function clientMe(clock: FakeClock): MeResponse {
  return meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    userId: USER_A,
    permissions: ['concessions.read', 'concessions.revoke'],
  });
}

/** `GET /me` del instalador del proveedor sin concesión (`concessions.grant`). */
export function providerMe(clock: FakeClock): MeResponse {
  const base = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    userId: USER_A,
    permissions: ['concessions.grant'],
  });
  return {
    ...base,
    organization: {
      organization_id: PROVIDER_ORGANIZATION,
      code: 'PROV-SINT',
      name: 'Proveedora sintética',
      kind: 'provider',
    },
    assignments: [
      { role: 'provider_installer', scope_level: 'organization', scope_id: PROVIDER_ORGANIZATION },
    ],
  };
}

export async function mountAt(
  path: '/concessions' | '/provider/concessions',
  options: {
    readonly concessions?: readonly ClientConcession[];
    readonly providerConcessions?: readonly ProviderConcession[];
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
  const me = path === '/concessions' ? clientMe(clock) : providerMe(clock);
  const meHeaders: (string | null)[] = [];
  const session = createSessionStore({
    clock,
    port: {
      me: {
        read: () => {
          meHeaders.push(concession.header());
          return Promise.resolve(me);
        },
      },
      logout: { write: () => Promise.resolve() },
    },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => path,
    navigate: () => undefined,
    storages: [],
  });
  const concession = createConcessionStore({
    clock,
    organizationKind: () => session.getSnapshot().principal?.organization.kind ?? null,
    cache,
  });
  await session.load();
  installTransport(
    createTransport({ clock, connection, origin: ORIGIN, ...transportOptions(concession) }),
  );
  const requests = { concessions: 0, providerConcessions: 0 };
  /** `X-Vigia-Concession` de cada `GET /hierarchy` (`null` sin cabecera). */
  const hierarchyHeaders: (string | null)[] = [];
  server.use(
    http.get(`${ORIGIN}/hierarchy`, ({ request }) => {
      hierarchyHeaders.push(request.headers.get('X-Vigia-Concession'));
      return HttpResponse.json(hierarchy());
    }),
    http.get(`${ORIGIN}/concessions`, () => {
      requests.concessions += 1;
      return HttpResponse.json({ concessions: options.concessions ?? [clientConcession()] });
    }),
    http.get(`${ORIGIN}/provider/concessions`, () => {
      requests.providerConcessions += 1;
      return HttpResponse.json({
        concessions: options.providerConcessions ?? [providerConcession()],
      });
    }),
  );
  const router = createMemoryRouter(
    [
      { path: '/concessions', Component: ClientPage },
      { path: '/provider/concessions', Component: ProviderPage },
    ],
    { initialEntries: [path] },
  );
  const view = render(
    <StrictMode>
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <ConcessionProvider store={concession}>
            <RouterProvider router={router} />
          </ConcessionProvider>
        </ScreenQueryProvider>
      </SessionProvider>
    </StrictMode>,
  );
  disposers.push(() => {
    session.dispose();
    concession.dispose();
    cache.dispose();
  });
  return {
    clock,
    cache,
    concession,
    requests,
    meHeaders,
    hierarchyHeaders,
    router,
    container: view.container,
  };
}

/** Avanza el reloj simulado dentro de `act`. */
export function advance(clock: FakeClock, ms: number): void {
  act(() => {
    clock.advance(ms);
  });
}

type Violations = Awaited<ReturnType<typeof axe>>['violations'];

/** Violaciones de `axe` serias o críticas (NFR-APP-50). */
export async function seriousViolations(container: HTMLElement): Promise<Violations> {
  const results = await axe(container);
  return results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
}

export function expectNoSerious(violations: Violations): void {
  expect(violations.map((violation) => violation.id)).toEqual([]);
}

/** El elemento `index` de una lista; si falta, la prueba falla nombrándolo. */
export function nth<T>(items: readonly T[], index: number): T {
  const item = items[index];
  if (item === undefined) throw new Error(`falta el elemento ${String(index)}`);
  return item;
}
