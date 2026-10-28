/** Proveedores reales y datos exclusivamente sintéticos para SCR-09 (clasificación y lazo). */
import { cleanup, render } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactElement } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach } from 'vitest';
import Classify from '@/pages/scr09/classify';
import Loop from '@/pages/scr09/loop';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type {
  ClassificationHistory,
  ClassificationItem,
  FindingDetail,
  LoopConfiguration,
} from '@/ports/scr09Classify';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { exampleFor } from '../../msw/handlers';
import { server } from '../../msw/server';

export const FINDING = '0190a1b2-2222-7000-8000-000000000009';
const PLANT = '0190a1b2-0000-7000-8000-000000000001';
const ZONE = '0190a1b2-1111-7000-8000-000000000001';
export const USER = '0190a1b2-4444-7000-8000-000000000001';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };

export function classification(sequence: number, outcome = 'confirmed'): ClassificationItem {
  return {
    classification_id: `0190a1b2-5555-7000-8000-${String(sequence).padStart(12, '0')}`,
    record_id: `0190a1b2-6666-7000-8000-${String(sequence).padStart(12, '0')}`,
    sequence_in_subject: sequence,
    outcome,
    reason_category:
      outcome === 'confirmed'
        ? 'real_coexistence'
        : outcome === 'false_positive'
          ? 'reflection_or_glare'
          : 'scheduled_maintenance',
    reason_text: `Motivo sintético número ${String(sequence)} con detalle suficiente`,
    is_reclassification: sequence > 1,
    reinforced_reason: sequence > 1,
    previous_outcome: sequence > 1 ? 'confirmed' : null,
    effect_on_loop: sequence > 1 ? 'loop_suspended' : 'loop_opened',
    classified_at: INSTANT,
    classified_by: {
      user_id: USER,
      display_name: 'Coordinación sintética',
      role_in_use: 'coordinator_sst',
    },
  };
}

export function history(items: ClassificationItem[]): ClassificationHistory {
  return {
    finding_state_id: FINDING,
    time_aggregated: false,
    aggregation_window_minutes: null,
    classifications: items,
    review_resolutions: [],
  };
}

/** `lifecycle_state` admite cualquier texto: la lectura tolerante recibe valores fuera de la lista. */
export function detail(
  overrides: Partial<Omit<FindingDetail, 'lifecycle_state'>> & {
    readonly lifecycle_state?: string;
  } = {},
): FindingDetail {
  const base = exampleFor('get_finding_loop_findings__finding_state_id__get') as FindingDetail;
  return {
    ...base,
    finding_state_id: FINDING,
    plant_id: PLANT,
    zone_id: ZONE,
    lifecycle_state: 'open',
    classification_outcome: 'confirmed',
    classification_count: 1,
    time_aggregated: false,
    classifications: [classification(1)],
    actions: [],
    ...overrides,
  } as FindingDetail;
}

function configuration(): LoopConfiguration {
  return {
    ...(exampleFor('configuration_loop_plants__plant_id__configuration_get') as LoopConfiguration),
    plant_id: PLANT,
    action_deadline_default_days: 15,
    action_deadline_min_days: 1,
    action_deadline_max_days: 90,
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function renderScreen(
  screen: 'classify' | 'loop',
  {
    permissions = ['findings.read', 'findings.classify', 'actions.manage', 'hierarchy.read'],
  }: { permissions?: string[] } = {},
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
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    userId: USER,
    permissions,
  });
  const path = `/findings/${FINDING}/${screen}`;
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
      HttpResponse.json({
        organization: me.organization,
        nodes: [],
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
              {
                zone_id: ZONE,
                plant_id: PLANT,
                code: 'Z-1',
                name: 'Zona sintética de prensas',
                node_id: null,
              },
            ],
          },
        ],
      }),
    ),
    http.get('http://localhost/loop/plants/:plant_id/configuration', () =>
      HttpResponse.json(configuration()),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  const element: ReactElement = screen === 'classify' ? <Classify /> : <Loop />;
  const view = render(
    <SessionProvider store={session}>
      <ScreenQueryProvider cache={cache}>
        <MemoryRouter initialEntries={[path]}>
          <Routes>
            <Route path={`/findings/:finding_state_id/${screen}`} element={element} />
          </Routes>
        </MemoryRouter>
      </ScreenQueryProvider>
    </SessionProvider>,
  );
  return { ...view, cache, clock, organizationId: me.organization.organization_id };
}
