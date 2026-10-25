/** Datos exclusivamente sintéticos y proveedores reales para el recorrido de SCR-07. */
import { cleanup } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { type ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { FleetNode } from '@/ports/scr07Fleet';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

export const PLANT = '0190a1b2-0000-7000-8000-000000000001';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export const plants = [
  {
    plant_id: PLANT,
    code: 'PLANTA-SINT',
    name: 'Planta sintética',
    timezone: 'America/Bogota',
    country: 'CO',
  },
];
export function node(index = 1): FleetNode {
  return {
    node_id: `0190a1b2-0000-7000-8000-${String(index).padStart(12, '0')}`,
    code: `NODO-SINT-${String(index)}`,
    plant_id: PLANT,
    zones: [],
    status: 'enrolled',
    decommissioned_at: null,
    replaces_node_id: null,
    software_version: '1.1.0',
    contract_version: '1.1.0',
    model_version: '1.0.0',
    contract_notice: null,
    last_heartbeat_at: INSTANT,
    since: INSTANT,
    communication_state: 'reachable',
    heartbeat_notice: null,
    local_queue: { pending: 4, oldest_pending_at: INSTANT, dead_letter: [] },
    clock: { synchronized: true, offset_ms: 12 },
    signal_reader: null,
    cameras: [],
    zone_states: [],
    warnings: ['clock_drift'],
    target_version: '1.2.0',
    last_update_result: 'failed',
    live_view_local_url: 'https://nodo.local:8443/',
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export const ZONE_A = '0190a1b2-0000-7000-8000-00000000a001';
export const ZONE_B = '0190a1b2-0000-7000-8000-00000000a002';
export const CAMERA = '0190a1b2-0000-7000-8000-00000000c001';

/** Jerarquía sintética completa (`HierarchyView`) con dos zonas y los nodos dados. */
export function hierarchyPlants() {
  return plants.map((plant) => ({
    ...plant,
    data_region: 'us-east-1',
    status: 'active',
    zones: [
      {
        zone_id: ZONE_A,
        plant_id: PLANT,
        code: 'ZONA-SINT-A',
        name: 'Zona sintética A',
        node_id: null,
      },
      {
        zone_id: ZONE_B,
        plant_id: PLANT,
        code: 'ZONA-SINT-B',
        name: 'Zona sintética B',
        node_id: null,
      },
    ],
  }));
}
export function hierarchyNode(index: number, status = 'enrolled') {
  const value = node(index);
  return {
    node_id: value.node_id,
    plant_id: PLANT,
    code: value.code,
    status,
    live_view_local_url: null,
  };
}

export async function fleetFixture(
  path = '/fleet',
  hierarchy: { readonly plants: unknown[]; readonly nodes: unknown[] } = { plants, nodes: [] },
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  let active = true;
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => document.visibilityState === 'visible',
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => active,
    },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    permissions: ['fleet.read', 'fleet.manage', 'commissioning.run'],
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
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({ organization: me.organization, ...hierarchy }),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <MemoryRouter initialEntries={[path]}>{children}</MemoryRouter>
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  return {
    clock,
    cache,
    Wrapper,
    pause: () => {
      active = false;
    },
  };
}
