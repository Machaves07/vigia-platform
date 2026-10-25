/** Datos exclusivamente sintéticos y proveedores reales del armazón. */
import { cleanup, fireEvent, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { MetricSnapshot, Exposure, Adoption, Overdue } from '@/ports/scr13Metrics';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { composeCoverage } from '@/state/coverage/compose';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';
import { text } from '@/texts';

export const PLANT = '0190a1b2-0000-7000-8000-000000000003';
export const ZONE = '0190a1b2-0000-7000-8000-000000000031';
export const OTHER = '0190a1b2-0000-7000-8000-000000000032';
export const API = 'http://localhost';
export const WAIT = { timeout: 10_000 };
export async function consult() {
  await screen.findByRole('option', { name: 'Planta sintética: Zona sintética 1' }, WAIT);
  fireEvent.change(screen.getByLabelText(text('scr13.zone')), { target: { value: ZONE } });
  fireEvent.click(screen.getByRole('button', { name: text('scr13.consult') }));
}
const FROM = '2026-01-01T00:00:00.000Z';
const TO = '2026-02-01T00:00:00.000Z';
const PERIOD = { fromMs: Date.parse(FROM), toMs: Date.parse(TO) };
const duration = PERIOD.toMs - PERIOD.fromMs;

export function coverageResponse(organizationId: string) {
  return {
    organization_id: organizationId,
    plant_id: PLANT,
    zone_id: ZONE,
    period: { from: FROM, to: TO },
    intervals: [
      {
        starts_at: FROM,
        ends_at: TO,
        duration_ms: duration,
        layer: 'node_report',
        state: 'observable',
        causes: [],
        clock_basis: 'node',
      },
    ],
    summary: {
      observable_ms: duration,
      degraded_ms: 0,
      not_observable_ms: 0,
      no_communication_ms: 0,
      never_reported_ms: 0,
      zone_not_active_ms: 0,
    },
  };
}
export function composite() {
  return composeCoverage(PERIOD, [{ period: PERIOD, response: coverageResponse('synthetic') }]);
}
export function snapshot(organizationId: string): MetricSnapshot {
  return {
    snapshot_id: OTHER,
    organization_id: organizationId,
    plant_id: PLANT,
    zone_id: ZONE,
    period: '2026-01',
    period_from: FROM,
    period_to: TO,
    granularity: 'month',
    values: [
      {
        key: 'activation',
        unit: 'ratio',
        status: 'not_computable',
        value: null,
        excluded_unclosable_count: 0,
        detail_code: 'accreditation_metric_not_computable',
      },
    ],
    materialized_at: FROM,
    recomputed_at: null,
    inputs_digest: '0'.repeat(64),
  };
}
export function exposureData(): Exposure {
  return {
    zone_id: ZONE,
    period: '2026-01',
    period_from: FROM,
    period_to: TO,
    status: 'computed',
    confirmed_minutes: 12,
    pending_classification_minutes: 7,
    observable_coverage_percent: 80,
    partial_reason_es: null,
    usage_notice_es: 'Tiempo de exposición, no cantidad de personas.',
    hourly_distribution: [{ start_minute_of_day: 0, width_minutes: 240, confirmed_minutes: 12 }],
    coverage_timeline: {
      period: { from: FROM, to: TO },
      intervals: [],
      summary: { ...coverageResponse('synthetic').summary, not_elapsed_ms: 0, total_ms: duration },
      annotations: [],
    },
    materialized_at: FROM,
    recomputed_at: null,
  };
}
export function adoptionData(): Adoption {
  return {
    plant_id: PLANT,
    period_from: '2026-01-01',
    period_to: '2026-01-31',
    evaluated_day: '2026-01-31',
    threshold_days: 7,
    usage_notice_es: 'Actividad del lazo por zona.',
    zones: [
      {
        zone_id: ZONE,
        zone_active: true,
        silent_days: 8,
        streak_start: null,
        last_activity_at: null,
        days: [{ day: '2026-01-01', activity_counts: {}, activity_count: 9 }],
      },
    ],
  };
}
export function overdueData(): Overdue {
  return {
    plant_id: PLANT,
    highlight_threshold_days: 30,
    zones: [{ zone_id: ZONE, overdue_count: 2, highlighted_count: 2 }],
    items: [30, 31].map((days, index) => ({
      finding_state_id: index === 0 ? ZONE : OTHER,
      plant_id: PLANT,
      zone_id: ZONE,
      subject_kind: 'finding',
      family: 'coexistence',
      tier: 'tier_1',
      lifecycle_state: 'overdue',
      declared: false,
      action_id: OTHER,
      action_type: 'engineering',
      assignee_user_id: OTHER,
      assignee_inactive: false,
      due_at: FROM,
      overdue_since: FROM,
      overdue_days: days,
      highlighted: true,
    })),
    next_cursor: null,
    partition_notice: null,
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});
export async function fixture(
  permissions = ['metrics.read', 'exposure.read', 'executive_summary.read', 'coverage.read'],
) {
  const clock = new FakeClock(Date.parse('2026-01-15T12:00:00.000Z'));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 28_800_000,
    permissions,
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => '/metrics',
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: API }));
  server.use(
    http.get(`${API}/hierarchy`, () =>
      HttpResponse.json({
        organization: me.organization,
        plants: [
          {
            plant_id: PLANT,
            code: 'P-SINT',
            name: 'Planta sintética',
            timezone: 'UTC',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [ZONE, OTHER].map((id, index) => ({
              zone_id: id,
              plant_id: PLANT,
              code: `Z-${String(index)}`,
              name: `Zona sintética ${String(index + 1)}`,
              node_id: null,
            })),
          },
        ],
        nodes: [],
      }),
    ),
    http.get(`${API}/accreditation/metrics`, () =>
      HttpResponse.json({
        plant_id: PLANT,
        period: '2026-01',
        zones: [snapshot(me.organization.organization_id)],
      }),
    ),
    http.get(`${API}/accreditation/exposure`, () => HttpResponse.json(exposureData())),
    http.get(`${API}/accreditation/adoption`, () => HttpResponse.json(adoptionData())),
    http.get(`${API}/zones/:zone_id/coverage`, () =>
      HttpResponse.json(coverageResponse(me.organization.organization_id)),
    ),
    http.get(`${API}/loop/overdue`, () => HttpResponse.json(overdueData())),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <MemoryRouter>{children}</MemoryRouter>
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  return { Wrapper, clock, cache, organizationId: me.organization.organization_id };
}
