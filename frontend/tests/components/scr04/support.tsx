/** Transporte, sesión y caché reales; solo datos sintéticos y rutas de esta tarea. */
import { cleanup, render } from '@testing-library/react';
import { afterEach } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';
import type { ReactNode } from 'react';
import { http, HttpResponse } from 'msw';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import { ScreenQueryProvider, createScreenQueryCache } from '@/state/query';
import { SessionProvider, createSessionStore } from '@/state/session';
import type { Admission, Catalog, Vocabulary } from '@/ports/scr04Parameters';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

export const ZONE = '0190a1b2-1111-7000-8000-000000000101';
export const PLANT = '0190a1b2-0000-7000-8000-000000000101';
export const CAMERA = '0190a1b2-2222-7000-8000-000000000101';
export const RECORD = '0190a1b2-3333-7000-8000-000000000101';
export const INSTANT = '2026-03-02T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };
export const REASON = 'Ajuste de configuración sintética';

export function catalog(version = 1): Catalog {
  return {
    zone_id: ZONE,
    catalog_version: version,
    issued_at: INSTANT,
    issued_by: RECORD,
    role_in_use: 'administrator',
    reason_es: REASON,
    changed_fields: ['thresholds'],
    superseded_at: null,
    single_occupancy: false,
    aggregation_window_minutes: 60,
    catalog: {
      thresholds: { review: 0.4, publication: 0.8 },
      minimum_coverage: { required_count: 1, required_camera_ids: [] },
      clip_window: { pre_seconds: 10, post_seconds: 10 },
      episode: { grouping_window_ms: 3000, max_segment_ms: 900000 },
    },
  };
}
export function admission(): Admission {
  return {
    admission_id: RECORD,
    plant_id: PLANT,
    family: 'coexistence',
    answers: { standard: true, remedy: true, subject: true },
    justification_es: null,
    result: 'admitted',
    failed_criterion: null,
    evaluated_by: RECORD,
    role_in_use: 'administrator',
    evaluated_at: INSTANT,
    ledger_record_id: RECORD,
  };
}
function vocabulary(): Vocabulary {
  return {
    list_version: 1,
    lemmatizer_version: 'sintetico',
    effective_from: INSTANT,
    record_id: RECORD,
    declared_names: [],
    terms: [
      {
        term_id: RECORD,
        category: 'intent_or_blame_attribution',
        form: 'lemma',
        value: 'término sintético',
        lemma: null,
        suggestion_es: 'Descripción observable',
        origin: 'derived',
        added_in_version: 1,
      },
    ],
  };
}
export function errorResponse(detail: string, extra: Record<string, unknown> = {}, status = 400) {
  return HttpResponse.json(
    {
      code: 'invalid_request',
      detail_code: detail,
      message_es: 'Mensaje sintético del servidor.',
      correlation_id: RECORD,
      ...extra,
    },
    { status },
  );
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
});

export async function renderPage(element: ReactNode, path = `/zones/${ZONE}/catalog/parameters`) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 28_800_000,
    permissions: ['catalog.read', 'catalog.manage', 'vocabulary.manage'],
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
    http.get('http://localhost/zones/:zone/catalog', () => HttpResponse.json(catalog())),
    http.get('http://localhost/zones/:zone/regression', () =>
      HttpResponse.json({ zone_id: ZONE, state: 'pending' }),
    ),
    http.get('http://localhost/plants/:plant/admissions', () =>
      HttpResponse.json({ admissions: [], next_after: null }),
    ),
    http.get('http://localhost/loop/vocabulary', () => HttpResponse.json(vocabulary())),
  );
  const route =
    path === '/admin/vocabulary'
      ? path
      : path.endsWith('/admissions')
        ? '/plants/:plant_id/admissions'
        : '/zones/:zone_id/catalog/parameters';
  const router = createMemoryRouter([{ path: route, element }], { initialEntries: [path] });
  disposers.push(() => {
    router.dispose();
    session.dispose();
    cache.dispose();
  });
  return {
    ...render(
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>
          <RouterProvider router={router} />
        </ScreenQueryProvider>
      </SessionProvider>,
    ),
    cache,
    clock,
  };
}
