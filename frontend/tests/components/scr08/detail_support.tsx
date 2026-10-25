/** Proveedores reales y datos exclusivamente sintéticos para el detalle y las etiquetas de SCR-08. */
import { cleanup, render } from '@testing-library/react';
import fc from 'fast-check';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes } from 'react-router';
import { afterEach } from 'vitest';
import type { Finding as ContractFinding } from '@/contracts';
import Detail from '@/pages/scr08/detail';
import Labels from '@/pages/scr08/labels';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type {
  ClassificationHistory,
  ClassificationItem,
  FindingDetail,
  Label,
  Plant,
} from '@/ports/scr08Detail';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { finding as contractFinding } from '../../generators';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';

export const WAIT = { timeout: 10_000 };
const PLANT = '0190a1b2-0000-7000-8000-000000000001';
export const ZONE = '0190a1b2-1111-7000-8000-000000000001';
const FINDING = '0190a1b2-2222-7000-8000-000000000001';
const RECORD = '0190a1b2-4444-7000-8000-000000000001';
export const EVIDENCE = '0190a1b2-5555-7000-8000-000000000001';
const INSTANT = '2026-01-01T12:00:00.000Z';
export const ZONE_NAME = 'Zona sintética de prensa';
export const STANDARD_TEXT = 'Texto sintético declarado del estándar de resguardo.';

function plant(): Plant {
  return {
    plant_id: PLANT,
    code: 'PLANTA-SINT',
    name: 'Planta sintética',
    timezone: 'America/Bogota',
    country: 'CO',
    status: 'active',
    data_region: 'co',
    zones: [{ zone_id: ZONE, plant_id: PLANT, code: 'Z-1', name: ZONE_NAME, node_id: null }],
  };
}

/** Un `Finding` del contrato válido y fijo (semilla constante), con una cámara originaria. */
export function validFinding(): ContractFinding {
  const [sample] = fc.sample(contractFinding(), { numRuns: 1, seed: 327 });
  if (sample === undefined) throw new Error('Sin Finding sintético.');
  const cameras = sample.cameras.map((camera, index) => ({ ...camera, originating: index === 0 }));
  return {
    ...sample,
    organization_id: '0190a1b2-9999-7000-8000-000000000001',
    plant_id: PLANT,
    zone_id: ZONE,
    node_time: { ...sample.node_time, started_at: '2026-01-01T11:59:58.500Z' },
    observability_state: 'degraded',
    condition_duration_ms: 4_321,
    cameras,
  };
}

export function classification(sequence: number, outcome = 'confirmed'): ClassificationItem {
  return {
    classification_id: `0190a1b2-6666-7000-8000-${String(sequence).padStart(12, '0')}`,
    record_id: `0190a1b2-7777-7000-8000-${String(sequence).padStart(12, '0')}`,
    sequence_in_subject: sequence,
    outcome,
    reason_category: outcome === 'confirmed' ? 'guard_bypassed' : 'other_confirmed',
    reason_text: `Motivo sintético número ${String(sequence)} de la clasificación de la zona.`,
    is_reclassification: sequence > 1,
    reinforced_reason: sequence > 1,
    previous_outcome: sequence > 1 ? 'confirmed' : null,
    effect_on_loop: 'none',
    classified_at: INSTANT,
    classified_by: {
      user_id: '0190a1b2-8888-7000-8000-000000000001',
      display_name: 'Coordinación sintética',
      role_in_use: 'coordinator_sst',
    },
  };
}

export function history(items = [classification(1), classification(2)]): ClassificationHistory {
  return {
    finding_state_id: FINDING,
    time_aggregated: false,
    aggregation_window_minutes: null,
    classifications: items,
  };
}

export function detail(): FindingDetail {
  return {
    finding_state_id: FINDING,
    subject_kind: 'finding',
    subject_id: RECORD,
    plant_id: PLANT,
    zone_id: ZONE,
    family: 'coexistence',
    tier: 'tier_1',
    lifecycle_state: 'open',
    classification_outcome: 'confirmed',
    classification_count: 2,
    age_days: 3,
    observability_state_at_event: 'degraded',
    occurred_at: INSTANT,
    due_at: null,
    overdue_days: 0,
    time_aggregated: false,
    aggregation_window_minutes: null,
    evidence_flagged: false,
    standard: {
      standard_id: '0190a1b2-3333-7000-8000-000000000001',
      version: 3,
      title_es: 'Estándar sintético de resguardo',
      declared_text: STANDARD_TEXT,
      declared_text_sha256: 'a'.repeat(64),
      effective_from: '2025-06-01T00:00:00.000Z',
      superseded_since: null,
      superseded_in_catalog_version: null,
    },
    gate_at_event: {
      status: 'approved',
      effective_from: '2025-06-01T00:00:00.000Z',
      effective_until: null,
    },
    coverage_at_event: null,
    nodes: null,
    episode: null,
    records: [
      {
        record_id: RECORD,
        record_type: 'finding',
        chain_sequence: 7,
        content_hash: 'b'.repeat(64),
        segment_index: 0,
        received_at: INSTANT,
        occurred_at: INSTANT,
        signals: [
          { signal_id: null, role: 'energy', value: 'asserted', valid: true, read_at: INSTANT },
        ],
        clips: [
          {
            evidence_id: EVIDENCE,
            clip_id: '0190a1b2-5555-7000-8000-000000000002',
            camera_id: validFinding().cameras[0]?.camera_id ?? ZONE,
            segment: 'full',
            media_kind: 'video',
            content_type: 'video/mp4',
            size_bytes: 1024,
            sha256: 'c'.repeat(64),
            duration_ms: 5000,
            starts_at: INSTANT,
            ends_at: INSTANT,
            read_url: null,
          },
        ],
      },
    ],
    classifications: [],
    actions: [],
    analyses: [],
    notices: [],
  };
}

export function labelRow(index: number): Label {
  return {
    label_id: `0190a1b2-aaaa-7000-8000-${String(index).padStart(12, '0')}`,
    plant_id: PLANT,
    zone_id: ZONE,
    source_record_id: RECORD,
    subject_record_id: RECORD,
    family: 'guard_bypass',
    outcome: 'false_positive',
    reason_category: 'reflection_or_glare',
    evidence_ids: [EVIDENCE],
    labeled_at: INSTANT,
    labeled_by_user_id: '0190a1b2-8888-7000-8000-000000000009',
    labeled_by_role: 'coordinator_sst',
  };
}

export function apiError(code: string, status: number, detail_code: string | null = null) {
  return HttpResponse.json(
    {
      code,
      detail_code,
      message_es: 'Mensaje sintético del servidor.',
      correlation_id: '0190a1b2-bbbb-7000-8000-000000000001',
      retry_after_seconds: null,
    },
    { status },
  );
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

interface Server {
  detail: () => Response;
  history: () => Response;
  record: () => Response;
  coverage: () => Response;
  labels: (url: URL) => Response;
}

/** Cuántas veces se pidió cada operación y con qué URL. */
interface Calls {
  detail: number;
  history: number;
  record: number;
  coverage: URL[];
  labels: URL[];
  readUrl: number;
}

export async function fixture({
  path = `/findings/${FINDING}`,
  permissions = ['findings.read', 'evidence.read', 'coverage.read', 'labels.read'],
}: { path?: string; permissions?: string[] } = {}) {
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
  const calls: Calls = { detail: 0, history: 0, record: 0, coverage: [], labels: [], readUrl: 0 };
  const responses: Server = {
    detail: () => HttpResponse.json(detail()),
    history: () => HttpResponse.json(history()),
    record: () => HttpResponse.json({ record_id: RECORD, content: validFinding() }),
    coverage: () =>
      HttpResponse.json({
        zone_id: ZONE,
        instant: INSTANT,
        state: 'observable',
        causes: [],
        layer: 'platform',
        clock_basis: 'platform',
        source_record_ids: [],
      }),
    labels: () => HttpResponse.json({ items: [labelRow(1)], next_cursor: null }),
  };
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({ organization: me.organization, plants: [plant()], nodes: [] }),
    ),
    http.get('http://localhost/loop/findings/:id', () => {
      calls.detail++;
      return responses.detail();
    }),
    http.get('http://localhost/loop/findings/:id/classifications', () => {
      calls.history++;
      return responses.history();
    }),
    http.get('http://localhost/ledger/records/:id', () => {
      calls.record++;
      return responses.record();
    }),
    http.get('http://localhost/zones/:zone_id/coverage/at', ({ request }) => {
      calls.coverage.push(new URL(request.url));
      return responses.coverage();
    }),
    http.get('http://localhost/labels', ({ request }) => {
      const url = new URL(request.url);
      calls.labels.push(url);
      return responses.labels(url);
    }),
    http.post('http://localhost/evidence/:id/read-url', () => {
      calls.readUrl++;
      return HttpResponse.json({
        evidence_id: EVIDENCE,
        url: 'https://evidencia.example/clip-sintetico.mp4',
        expires_at: '2026-01-01T12:05:00.000Z',
      });
    }),
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
  const view = () =>
    render(
      <Routes>
        <Route path="/findings/labels" element={<Labels />} />
        <Route path="/findings/:finding_state_id" element={<Detail />} />
        <Route path="/findings" element={<p>bandeja</p>} />
      </Routes>,
      { wrapper: Wrapper },
    );
  return { clock, cache, connection, calls, responses, view };
}
