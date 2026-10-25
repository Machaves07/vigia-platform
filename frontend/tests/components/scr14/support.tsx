/** Proveedores reales y datos sintéticos, sin hora del sistema. */
import { cleanup, fireEvent, render, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter, createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';
import Page from '@/pages/scr14/exports';
import { descriptor } from '@/pages/scr14/descriptor';
import { createAppRoutes } from '@/app/router';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { ExportJob, LedgerPage, Plant } from '@/ports/scr14Exports';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

export const API = 'http://localhost';
export const PLANT = '0190a1b2-0000-7000-8000-000000000014';
export const EXPORT = '0190a1b2-1414-7000-8000-000000000001';
export const ORG = '0190a1b2-0000-7000-8000-0000000000f1';
const INSTANT = '2026-01-01T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };
export const HASH = 'a'.repeat(64);
export const DOCUMENT_HASH = 'b'.repeat(64);
export const MANIFEST_HASH = 'c'.repeat(64);
const zones = Array.from({ length: 21 }, (_, index) => ({
  zone_id: `0190a1b2-1414-7000-8000-${String(index + 1).padStart(12, '0')}`,
  plant_id: PLANT,
  code: `Z-${String(index + 1)}`,
  name: `Zona sintética ${String(index + 1)}`,
  node_id: null,
}));
const plant: Plant = {
  plant_id: PLANT,
  code: 'P-SINT',
  name: 'Planta sintética',
  timezone: 'America/Bogota',
  country: 'CO',
  status: 'active',
  data_region: 'co',
  zones,
};
export function job(export_state: ExportJob['export_state'] = 'ready'): ExportJob {
  return {
    export_id: EXPORT,
    plant_id: PLANT,
    zone_ids: zones.slice(0, 1).map((zone) => zone.zone_id),
    period: { from: '2026-01-01T05:00:00.000Z', to: '2026-01-02T05:00:00.000Z' },
    package_form: 'without_clips',
    export_state,
    progress_percent: 100,
    current_artifact: null,
    attempt_count: 1,
    failure_code: null,
    requested_at: INSTANT,
    started_at: INSTANT,
    completed_at: INSTANT,
    expires_at: '2026-02-01T12:00:00.000Z',
    correlation_id: EXPORT,
    manifest_summary: {
      document_sha256: DOCUMENT_HASH,
      archive_sha256: HASH,
      size_bytes: 100,
      artifact_count: 2,
      checkpoint_count: 1,
      verifier_version: 'v1',
    },
  };
}

export function ledgerPage(): LedgerPage {
  return {
    items: [
      {
        record_id: EXPORT,
        organization_id: ORG,
        plant_id: PLANT,
        record_type: 'export_produced',
        content: { export_id: EXPORT, manifest_sha256: MANIFEST_HASH },
      } as LedgerPage['items'][number],
    ],
    next_cursor: null,
  };
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function fixture(realRouter = false) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1_800_000,
    absoluteExpiresAt: clock.now() + 8 * 3_600_000,
    permissions: ['export.create', 'catalog.read', 'integrity.verify', 'findings.read'],
  });
  const session = createSessionStore({
    clock,
    port: { me: { read: () => Promise.resolve(me) }, logout: { write: () => Promise.resolve() } },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => '/exports',
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
        { initialEntries: ['/exports'] },
      )
    : null;
  installTransport(createTransport({ clock, connection, origin: API }));
  server.use(
    http.get(`${API}/hierarchy`, () =>
      HttpResponse.json({ organization: me.organization, plants: [plant], nodes: [] }),
    ),
    http.get(`${API}/zones/:zone_id/gates`, () =>
      HttpResponse.json({
        mounting: { status: 'approved' },
        usage: { status: 'revoked' },
        resulting_mode: 'commissioning',
      }),
    ),
    http.get(`${API}/accreditation/exports/:export_id`, () => HttpResponse.json(job())),
    http.post(`${API}/accreditation/exports`, () =>
      HttpResponse.json(job('requested'), { status: 202 }),
    ),
    http.post(`${API}/accreditation/exports/:export_id/download-url`, () =>
      HttpResponse.json({
        export_id: EXPORT,
        document_sha256: DOCUMENT_HASH,
        size_bytes: 100,
        url: 'https://downloads.example.test/document.pdf',
        expires_at: '2026-01-01T12:15:00.000Z',
      }),
    ),
    http.get(`${API}/accreditation/exports/:export_id/download`, () =>
      HttpResponse.json({
        export_id: EXPORT,
        archive_sha256: HASH,
        volumes: [1, 2].map((volume) => ({
          volume,
          name: `vol-0${String(volume)}.zip`,
          size_bytes: 50,
          sha256: HASH,
          url: `https://downloads.example.test/vol-0${String(volume)}.zip`,
          expires_at: '2026-01-01T12:15:00.000Z',
        })),
      }),
    ),
    http.get(`${API}/ledger/records`, () => HttpResponse.json(ledgerPage())),
    http.get(`${API}/.well-known/vigia-verifier`, () =>
      HttpResponse.json({
        name: 'vigia_verify.py',
        sha256: 'd'.repeat(64),
        size_bytes: 100,
        format_version: 1,
      }),
    ),
    http.get(`${API}/integrity/checkpoints`, () =>
      HttpResponse.json({
        organization_id: ORG,
        checkpoints: [
          { chain: { kind: 'ledger', plant_id: PLANT }, sequence: 7, entry_hash: HASH },
        ],
      }),
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
          {router === null ? (
            <MemoryRouter initialEntries={['/exports']}>{children}</MemoryRouter>
          ) : (
            <RouterProvider router={router} />
          )}
        </ScreenQueryProvider>
      </SessionProvider>
    );
  }
  const view = render(<Page />, { wrapper: Wrapper });
  await screen.findByRole('option', { name: plant.name }, WAIT);
  return { clock, cache, view };
}

export function fillRequest(count = 1, to = '2026-01-02T00:00') {
  fireEvent.change(screen.getByLabelText(text('scr14.plant')), { target: { value: PLANT } });
  for (const zone of zones.slice(0, count))
    fireEvent.click(screen.getByRole('checkbox', { name: zone.name }));
  fireEvent.change(screen.getByLabelText(text('scr14.from')), {
    target: { value: '2026-01-01T00:00' },
  });
  fireEvent.change(screen.getByLabelText(text('scr14.to')), { target: { value: to } });
}

export async function openJob() {
  fireEvent.change(screen.getByLabelText(text('scr14.export_id')), { target: { value: EXPORT } });
  fireEvent.click(screen.getByRole('button', { name: text('scr14.open') }));
  await screen.findByRole('region', { name: text('scr14.manifest') }, WAIT);
}
