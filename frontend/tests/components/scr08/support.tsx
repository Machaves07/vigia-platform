/** Proveedores reales y datos exclusivamente sintéticos para SCR-08. */
import { cleanup } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { Finding, Gate, InboxPage, Plant, Zone } from '@/ports/scr08Findings';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

export const PLANT = '0190a1b2-0000-7000-8000-000000000001';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export function zone(index = 1): Zone {
  return {
    zone_id: `0190a1b2-1111-7000-8000-${String(index).padStart(12, '0')}`,
    plant_id: PLANT,
    code: `Z-${String(index)}`,
    name: `Zona sintética ${String(index)}`,
    node_id: null,
  };
}
export function plant(zones: Zone[] = [zone()]): Plant {
  return {
    plant_id: PLANT,
    code: 'PLANTA-SINT',
    name: 'Planta sintética',
    timezone: 'America/Bogota',
    country: 'CO',
    status: 'active',
    data_region: 'co',
    zones,
  };
}
export function gate(id = zone().zone_id): Gate {
  return {
    zone_id: id,
    plant_id: PLANT,
    resulting_mode: 'productive',
    issued_at: INSTANT,
    mounting: { status: 'approved', decided_at: INSTANT, decided_by: null, record_id: 'acta' },
    usage: { status: 'approved', decided_at: INSTANT, decided_by: null, agreement_id: 'acuerdo' },
  };
}
export function finding(index = 1): Finding {
  return {
    finding_state_id: `0190a1b2-2222-7000-8000-${String(index).padStart(12, '0')}`,
    subject_kind: 'finding',
    plant_id: PLANT,
    zone_id: zone().zone_id,
    family: 'coexistence',
    tier: 'tier_1',
    standard: { standard_id: '0190a1b2-3333-7000-8000-000000000001', version: 3 },
    lifecycle_state: 'open',
    classification_outcome: null,
    classification_count: 0,
    age_days: 4,
    observability_state_at_event: 'degraded',
    occurred_at: INSTANT,
    due_at: null,
    overdue_days: 0,
    time_aggregated: false,
    aggregation_window_minutes: null,
    evidence_flagged: false,
    notices: [],
  };
}
export function page(items: Finding[] = []): InboxPage {
  return { items, zones: [], partitions: [], next_cursor: null };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function fixture({
  path = '/home',
  plants = [plant()],
  permissions = ['findings.read', 'coverage.read', 'catalog.read', 'executive_summary.read'],
}: { path?: string; plants?: Plant[]; permissions?: string[] } = {}) {
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
    permissions,
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
      HttpResponse.json({ organization: me.organization, plants, nodes: [] }),
    ),
    http.get('http://localhost/zones/:zone_id/gates', ({ params }) =>
      HttpResponse.json(gate(String(params.zone_id))),
    ),
    http.get('http://localhost/zones/:zone_id/regression', ({ params }) =>
      HttpResponse.json({
        zone_id: params.zone_id,
        state: 'current',
        marked_at: null,
        cause: null,
        catalog_version: null,
        model_version: null,
        affected_row_ids: null,
      }),
    ),
    http.get('http://localhost/zones/:zone_id/coverage/at', ({ params, request }) =>
      HttpResponse.json({
        zone_id: params.zone_id,
        instant: new URL(request.url).searchParams.get('instant'),
        state: 'observable',
        causes: [],
        layer: 'platform',
        clock_basis: 'platform',
        source_record_ids: [],
      }),
    ),
    http.get('http://localhost/loop/findings', () => HttpResponse.json(page())),
    http.get('http://localhost/loop/review-queue', () =>
      HttpResponse.json({ items: [], next_cursor: null }),
    ),
    http.get('http://localhost/loop/overdue', () =>
      HttpResponse.json({
        plant_id: PLANT,
        highlight_threshold_days: 3,
        items: [],
        zones: [{ zone_id: zone().zone_id, overdue_count: 7, highlighted_count: 2 }],
        partition_notice: null,
        next_cursor: null,
      }),
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
    Wrapper,
    clock,
    cache,
    pause: () => {
      active = false;
    },
  };
}
