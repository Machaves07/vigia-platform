/** Proveedores reales y datos exclusivamente sintéticos para el recorrido de SCR-16. */
import { cleanup } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { Plant, Transparency } from '@/ports/scr16Transparency';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

const PLANT = '0190a1b2-0000-7000-8000-000000000016';
export const ZONE = '0190a1b2-1616-7000-8000-000000000001';
const CAMERA = '0190a1b2-1616-7000-8000-0000000000c1';
export const AGREEMENT = '0190a1b2-1616-7000-8000-0000000000a1';
export const PENDING = '0190a1b2-1616-7000-8000-0000000000a2';
export const USER = '0190a1b2-1616-7000-8000-0000000000e1';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };

function plants(): Plant[] {
  return [
    {
      plant_id: PLANT,
      code: 'PLANTA-SINT',
      name: 'Planta sintética',
      timezone: 'America/Bogota',
      country: 'CO',
      status: 'active',
      data_region: 'co',
      zones: [
        { zone_id: ZONE, plant_id: PLANT, code: 'Z-16', name: 'Zona sintética', node_id: null },
      ],
    },
  ];
}

export function transparency(overrides: Partial<Transparency> = {}): Transparency {
  return {
    zone_id: ZONE,
    declared_scope: {
      scope_text_es: 'Alcance sintético de la zona de prensas',
      cameras: [
        {
          camera_id: CAMERA,
          framing_description_es: 'Encuadre sintético del acceso norte',
          reference_marker: true,
        },
      ],
      minimum_coverage: { required_count: 1, required_camera_ids: [CAMERA] },
    },
    standards: [
      {
        standard_id: '0190a1b2-1616-7000-8000-0000000000d1',
        version: 3,
        title_es: 'Estándar sintético de resguardo',
        declared_text: 'Texto declarado sintético del estándar',
      },
    ],
    current_agreement: {
      agreement_id: AGREEMENT,
      zone_id: ZONE,
      status: 'approved',
      signatories: [
        {
          role: 'copasst',
          user_id: USER,
          display_name: 'Firmante sintético',
          confirmed_at: INSTANT,
        },
        { role: 'plant_manager', user_id: PLANT, display_name: null, confirmed_at: null },
      ],
      document_sha256: null,
      replaces_agreement_id: null,
      created_by: USER,
      created_at: INSTANT,
      approved_at: INSTANT,
      approved_by: USER,
    },
    gates: {
      mounting: { status: 'approved', decided_at: INSTANT, record_id: null, decided_by: null },
      usage: { status: 'approved', decided_at: INSTANT, agreement_id: AGREEMENT, decided_by: null },
      resulting_mode: 'productive',
    },
    pending_confirmation_for_me: null,
    ...overrides,
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function transparencyFixture(
  path = `/transparency/${ZONE}`,
  role?: Parameters<typeof meResponse>[0]['role'],
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: {
      isVisible: () => document.visibilityState === 'visible',
      canReadPlatform: () => connection.canRead('platform'),
      isActive: () => true,
    },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    permissions: ['transparency.read', 'live_view.open'],
    ...(role === undefined ? {} : { role }),
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
      HttpResponse.json({ organization: me.organization, plants: plants(), nodes: [] }),
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
          <MemoryRouter initialEntries={[path]}>
            <Routes>
              <Route path="/transparency/:zone_id" element={children} />
              <Route path="/transparency" element={children} />
            </Routes>
          </MemoryRouter>
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  return { clock, cache, Wrapper };
}
