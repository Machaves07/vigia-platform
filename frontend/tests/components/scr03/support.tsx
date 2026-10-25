/** Proveedores reales; únicamente datos sintéticos y reloj inyectado. */
import { cleanup } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider, MemoryRouter, Route, Routes } from 'react-router';
import { createAppRoutes } from '@/app/router';
import { descriptor } from '@/pages/scr03/descriptor';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { CoverageResponse, Plant } from '@/ports/scr03Integrity';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

export const PLANT = '0190a1b2-0000-7000-8000-000000000003';
export const ZONE = '0190a1b2-0000-7000-8000-000000000031';
export const OTHER_ZONE = '0190a1b2-0000-7000-8000-000000000032';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };
export const API = 'http://localhost';
const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function integrityFixture(path = '/integrity', realRouter = false) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    permissions: ['integrity.verify', 'coverage.read'],
  });
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
  const router = realRouter
    ? createMemoryRouter(
        createAppRoutes(
          { effective_permissions: me.effective_permissions, assignments: me.assignments },
          { screens: [descriptor] },
        ),
        { initialEntries: [path] },
      )
    : null;
  installTransport(createTransport({ clock, connection, origin: API }));
  const plants: Plant[] = [
    {
      plant_id: PLANT,
      code: 'P-SINT',
      name: 'Planta sintética',
      timezone: 'America/Bogota',
      country: 'CO',
      status: 'active',
      data_region: 'co',
      zones: [ZONE, OTHER_ZONE].map((id, index) => ({
        zone_id: id,
        plant_id: PLANT,
        code: `Z-${String(index)}`,
        name: `Zona sintética ${String(index + 1)}`,
        node_id: null,
      })),
    },
  ];
  server.use(
    http.get(`${API}/hierarchy`, () =>
      HttpResponse.json({ organization: me.organization, plants, nodes: [] }),
    ),
  );
  disposers.push(() => {
    router?.dispose();
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          {router !== null ? (
            <RouterProvider router={router} />
          ) : (
            <MemoryRouter initialEntries={[path]}>
              <Routes>
                <Route path="/integrity" element={children} />
                <Route path="/zones/:zone_id/coverage" element={children} />
              </Routes>
            </MemoryRouter>
          )}
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  return { clock, cache, Wrapper, organizationId: me.organization.organization_id };
}

export function coverageResponse(
  request: Request,
  organizationId: string,
  patch: Partial<CoverageResponse> = {},
): CoverageResponse {
  const url = new URL(request.url);
  const from = url.searchParams.get('from') ?? INSTANT;
  const to = url.searchParams.get('to') ?? INSTANT;
  const duration = Date.parse(to) - Date.parse(from);
  return {
    organization_id: organizationId,
    plant_id: PLANT,
    zone_id: url.pathname.split('/')[2] ?? ZONE,
    period: { from, to },
    intervals: [
      {
        starts_at: from,
        ends_at: to,
        duration_ms: duration,
        layer: 'node_report',
        state: 'observable',
        causes: [],
        subject: null,
        source_record_ids: [],
        clock_basis: 'node',
      },
    ],
    node_intervals: [],
    summary: {
      observable_ms: duration,
      degraded_ms: 0,
      not_observable_ms: 0,
      no_communication_ms: 0,
      never_reported_ms: 0,
      zone_not_active_ms: 0,
    },
    ...patch,
  };
}
