/** Sesión, transporte y servidor sintéticos de la revisión mensual. */
import { cleanup, render } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, vi } from 'vitest';
import Page from '@/pages/scr12/package';
import Entry from '@/pages/scr12/entry';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import type { Commitment, MonthlyPackage, SampleItem } from '@/ports/scr12Monthly';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { server } from '../../msw/server';
import { FakeClock } from '../../support/fakeClock';
import { meResponse, USER_A } from '../../support/session';

export const WAIT = { timeout: 10_000 };
export const ZONE = '0190a1b2-1111-7000-8000-000000000101';
const OTHER_ZONE = '0190a1b2-1111-7000-8000-000000000102';
export const PLANT = '0190a1b2-0000-7000-8000-000000000101';
export const PACKAGE = '0190a1b2-2222-7000-8000-000000000101';
export const ITEM = '0190a1b2-3333-7000-8000-000000000101';
const RECORD = '0190a1b2-4444-7000-8000-000000000101';
export const PERIOD = '2026-02';
const INSTANT = '2026-03-02T12:00:00.000Z';
export const URL_ZONE = 'https://objetos.example.test/paquete-zona.zip?firma=sintetica';
const ROUTE = `/zones/${ZONE}/monthly/${PERIOD}`;

function packageData(): MonthlyPackage {
  return {
    package_id: PACKAGE,
    zone_id: ZONE,
    period: PERIOD,
    state: 'sealed',
    generated_at: INSTANT,
    findings_summary: {
      received: 8,
      classified: 7,
      confirmed: 5,
      discarded: 2,
      closed: 3,
      overdue: 1,
      unclosable: 1,
      reopened: 0,
    },
    recurrence_summary: [
      { family: 'coexistence', episodes_after_closure: 1, reference_closure_ids: [RECORD] },
    ],
    coverage_summary: [],
    commitments_from_previous_period: [],
    audit_sample_id: ITEM,
    record_id: RECORD,
  };
}
function commitmentData(): Commitment {
  return {
    commitment_id: ITEM,
    package_id: PACKAGE,
    zone_id: ZONE,
    period: PERIOD,
    text: 'Instalar un resguardo sintético sobre la zona.',
    responsible_user_id: USER_A,
    due_date: '2026-03-20',
    state: 'open',
    recorded_at: INSTANT,
    record_id: RECORD,
    closed_note: null,
    closed_at: null,
  };
}
function sampleData(): SampleItem {
  return {
    sample_item_id: ITEM,
    classification_id: RECORD,
    subject_id: ITEM,
    classification_outcome: 'confirmed',
    reason_category: 'real_coexistence',
    classified_by_role: 'coordinator_sst',
    classified_at: INSTANT,
    outcome: null,
    comment: null,
    reviewed_by: null,
    reviewed_at: null,
  };
}
export function apiError(detail_code: string | null, status = 409, code = 'conflict') {
  return HttpResponse.json(
    {
      code,
      detail_code,
      message_es: 'Error sintético',
      correlation_id: RECORD,
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

export async function setup(
  options: {
    permissions?: string[];
    entry?: boolean;
    packageResponse?: () => Response;
    createResponse?: () => Response;
    resultResponse?: () => Response;
    downloads?: { zone_id: string; url: string; expires_at: string }[];
  } = {},
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 28_800_000,
    permissions: options.permissions ?? [
      'review_package.read',
      'commitments.write',
      'findings.classify',
    ],
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => ROUTE,
    navigate: () => undefined,
    storages: [],
  });
  await session.load();
  installTransport(createTransport({ clock, connection, origin: 'http://localhost' }));
  let commitments: Commitment[] = [];
  let sample = sampleData();
  const calls = {
    reads: [] as string[],
    writes: [] as { method: string; path: string; body: unknown }[],
    download: 0,
    downloadPaths: [] as string[],
  };
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({
        organization: me.organization,
        plants: [
          {
            plant_id: ITEM,
            code: 'P-OTRA',
            name: 'Otra planta sintética',
            timezone: 'UTC',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [],
          },
          {
            plant_id: PLANT,
            code: 'P-SINT',
            name: 'Planta sintética',
            timezone: 'America/Bogota',
            country: 'CO',
            status: 'active',
            data_region: 'co',
            zones: [
              {
                zone_id: ZONE,
                plant_id: PLANT,
                code: 'Z-SINT',
                name: 'Zona sintética',
                node_id: null,
              },
            ],
          },
        ],
        nodes: [],
      }),
    ),
    http.get('http://localhost/accreditation/monthly-packages', ({ request }) => {
      calls.reads.push(new URL(request.url).search);
      return options.packageResponse?.() ?? HttpResponse.json(packageData());
    }),
    http.get('http://localhost/accreditation/commitments', () => HttpResponse.json(commitments)),
    http.get('http://localhost/accreditation/monthly-packages/:id/sample', () => {
      calls.reads.push('sample');
      return HttpResponse.json([sample]);
    }),
    http.get(
      'http://localhost/accreditation/monthly-packages/:plant/:period/download',
      ({ request }) => {
        calls.download++;
        calls.downloadPaths.push(new URL(request.url).pathname);
        return HttpResponse.json({
          plant_id: PLANT,
          period: PERIOD,
          zones: options.downloads ?? [
            {
              zone_id: OTHER_ZONE,
              url: 'https://objetos.example.test/otra.zip',
              expires_at: INSTANT,
            },
            { zone_id: ZONE, url: URL_ZONE, expires_at: '2026-03-02T12:15:00.000Z' },
          ],
        });
      },
    ),
    http.post(
      'http://localhost/accreditation/monthly-packages/:id/commitments',
      async ({ request }) => {
        const body: unknown = await request.json();
        calls.writes.push({ method: 'POST', path: new URL(request.url).pathname, body });
        if (options.createResponse) return options.createResponse();
        commitments = [commitmentData()];
        return HttpResponse.json(
          {
            commitment_batch_id: ITEM,
            record_id: RECORD,
            package_id: PACKAGE,
            zone_id: ZONE,
            commitments,
          },
          { status: 201 },
        );
      },
    ),
    http.patch('http://localhost/accreditation/commitments/:id', async ({ request }) => {
      const body = (await request.json()) as { state: string; note: string | null };
      calls.writes.push({ method: 'PATCH', path: new URL(request.url).pathname, body });
      const commitment = { ...commitmentData(), state: body.state, closed_note: body.note };
      commitments = [commitment];
      return HttpResponse.json({ commitment, record_id: RECORD });
    }),
    http.post('http://localhost/accreditation/sample-items/:id/result', async ({ request }) => {
      const body = (await request.json()) as { outcome: string; comment: string | null };
      calls.writes.push({ method: 'POST', path: new URL(request.url).pathname, body });
      if (options.resultResponse) return options.resultResponse();
      sample = { ...sample, ...body, reviewed_at: INSTANT };
      return HttpResponse.json(
        {
          sample_item_id: ITEM,
          sample_id: ITEM,
          package_id: PACKAGE,
          classification_id: RECORD,
          ...body,
          record_id: RECORD,
          reviewed_at: INSTANT,
          review_completed: true,
        },
        { status: 201 },
      );
    }),
    // Captura también una regresión hacia cualquier ruta de escritura del lazo.
    http.post('http://localhost/loop/*', async ({ request }) => {
      calls.writes.push({
        method: 'POST',
        path: new URL(request.url).pathname,
        body: await request.json(),
      });
      return HttpResponse.json({});
    }),
  );
  const navigate = vi.fn<(url: string) => void>();
  function Providers({ children }: { readonly children: ReactNode }) {
    return (
      <SessionProvider store={session}>
        <ScreenQueryProvider cache={cache}>{children}</ScreenQueryProvider>
      </SessionProvider>
    );
  }
  const router = createMemoryRouter(
    [
      { path: '/zones/monthly', element: <Entry /> },
      { path: '/zones/:zone_id/monthly/:period', element: <Page navigate={navigate} /> },
    ],
    { initialEntries: [options.entry ? '/zones/monthly' : ROUTE] },
  );
  const view = render(
    <Providers>
      <RouterProvider router={router} />
    </Providers>,
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  return { clock, cache, calls, navigate, router, view };
}
