/** Datos y sesión exclusivamente sintéticos para el recorrido real de SCR-04. */
import { cleanup, configure, fireEvent, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type { Catalog, CatalogDetail, CatalogVersion } from '@/ports/scr04Catalog';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

// Los límites de espera no son objeto de estas pruebas; toleran el runner compartido (retro 29).
configure({ asyncUtilTimeout: 10_000 });

export const ZONE = '0190a1b2-0000-7000-8000-000000000011';
export const PLANT = '0190a1b2-0000-7000-8000-000000000012';
export const ORG = '0190a1b2-0000-7000-8000-0000000000f1';
export const STANDARD = '0190a1b2-0000-7000-8000-000000000013';
export const CLIENT = '0190a1b2-0000-7000-8000-000000000014';
const INSTANT = '2026-01-01T12:00:00.000Z';
export const PREDICATE = {
  all_of: [{ presence: true }, { signal_role: 'energy', value: 'asserted' }],
  min_duration_ms: 1000,
};
export function payload(version = 1): Catalog {
  return {
    zone_id: ZONE,
    zone_code: 'ZONA-SINT',
    plant_id: PLANT,
    organization_id: ORG,
    version,
    issued_at: INSTANT,
    cameras: [{ camera_id: ZONE, code: 'CAM-SINT', role_in_zone: 'primary', declared_min_fps: 10 }],
    minimum_coverage: { required_count: 1, required_camera_ids: [] },
    signals: [],
    standards: [
      {
        standard_id: STANDARD,
        version,
        family: 'dwell',
        title_es: `Estándar sintético ${String(version)}`,
        declared_text: 'Presencia en zona con energía afirmada.',
        declared_by: {
          user_id: PLANT,
          display_name: 'Responsable sintético',
          role: 'administrator',
        },
        effective_from: INSTANT,
        tier_policy: 'tier_1_when_signal_valid',
        predicate: PREDICATE as Catalog['standards'][number]['predicate'],
      },
    ],
    thresholds: { review: 0.4, publication: 0.8 },
    clip_window: { pre_seconds: 5, post_seconds: 5 },
    episode: { grouping_window_ms: 3000, max_segment_ms: 900000 },
    commissioning_watermark: true,
  };
}
export function version(number = 1): CatalogVersion {
  return {
    zone_id: ZONE,
    catalog_version: number,
    issued_at: INSTANT,
    issued_by: PLANT,
    role_in_use: 'administrator',
    reason_es: 'Motivo sintético de publicación',
    changed_fields: ['standards'],
    superseded_at: null,
    single_occupancy: false,
    aggregation_window_minutes: 60,
    catalog: payload(number) as unknown as Record<string, unknown>,
  };
}
export function detail(number = 1): CatalogDetail {
  return {
    ...version(number),
    envelope: {
      key_id: 'catalog-sint',
      signature: `${'A'.repeat(86)}==`,
      signed_at: INSTANT,
      payload_canonical_sha256: 'a'.repeat(64),
      payload: payload(number),
    },
  };
}
const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

export async function fixture(
  path = `/zones/${ZONE}/catalog`,
  options: {
    readonly permissions?: readonly string[];
    readonly concessionOrganizationId?: string;
  } = {},
) {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  const cache = createScreenQueryCache({
    clock,
    gates: { isVisible: () => true, canReadPlatform: () => true, isActive: () => true },
  });
  const me = meResponse({
    idleExpiresAt: clock.now() + 1800000,
    absoluteExpiresAt: clock.now() + 28800000,
    permissions: options.permissions ?? ['catalog.read', 'catalog.manage'],
  });
  const concessionId = options.concessionOrganizationId === undefined ? null : STANDARD;
  if (concessionId !== null) {
    me.organization.kind = 'provider';
    me.concession_id = concessionId;
    me.assignments = [{ role: 'provider_installer', scope_level: 'organization', scope_id: ORG }];
  }
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
  installTransport(
    createTransport({
      clock,
      connection,
      origin: 'http://localhost',
      concession: () => concessionId,
    }),
  );
  server.use(
    http.get('http://localhost/hierarchy', () =>
      HttpResponse.json({
        organization: {
          ...me.organization,
          organization_id: options.concessionOrganizationId ?? ORG,
        },
        plants: [
          {
            plant_id: STANDARD,
            code: 'OTRA-PLANTA',
            name: 'Otra planta sintética',
            timezone: 'UTC',
            country: 'CO',
            data_region: 'us-east-1',
            status: 'active',
            zones: [],
          },
          {
            plant_id: PLANT,
            code: 'PL-SINT',
            name: 'Planta sintética',
            timezone: 'America/Bogota',
            country: 'CO',
            data_region: 'us-east-1',
            status: 'active',
            zones: [
              {
                zone_id: ZONE,
                plant_id: PLANT,
                code: 'ZONA-SINT',
                name: 'Zona sintética',
                node_id: null,
              },
            ],
          },
        ],
        nodes: [],
      }),
    ),
    http.get(`http://localhost/zones/${ZONE}/catalog`, () =>
      HttpResponse.json({
        ...version(),
        catalog: { ...payload(), organization_id: options.concessionOrganizationId ?? ORG },
      }),
    ),
    http.get(`http://localhost/zones/${ZONE}/catalog/versions`, () =>
      HttpResponse.json({ versions: [version(2), version(1)], next_after: null }),
    ),
    http.get(`http://localhost/zones/${ZONE}/regression`, () =>
      HttpResponse.json({
        zone_id: ZONE,
        state: 'current',
        marked_at: null,
        cause: null,
        catalog_version: 1,
        model_version: null,
        affected_row_ids: null,
      }),
    ),
    http.get(`http://localhost/plants/${PLANT}/admissions`, () =>
      HttpResponse.json({
        admissions: [{ family: 'dwell', result: 'admitted' }],
        next_after: null,
      }),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    const router = createMemoryRouter(
      [
        {
          path: path.startsWith('/plants/')
            ? '/plants/:plant_id/catalog'
            : path.endsWith('/standards')
              ? '/zones/:zone_id/standards'
              : '/zones/:zone_id/catalog',
          element: (
            <SessionProvider store={session}>
              <ScreenQueryProvider cache={cache}>{children}</ScreenQueryProvider>
            </SessionProvider>
          ),
        },
      ],
      { initialEntries: [path] },
    );
    disposers.push(() => {
      router.dispose();
    });
    return <RouterProvider router={router} />;
  }
  return { Wrapper, cache };
}

export function fill(label: string, value: string) {
  fireEvent.change(
    screen.getByLabelText(
      label === 'scr04.catalog.reason'
        ? `${text(label)} (${text('components_forms.reason.required')})`
        : text(label),
    ),
    { target: { value } },
  );
}
export async function next() {
  fireEvent.click(
    await screen.findByRole('button', { name: text('components_structure.wizard.next') }),
  );
}
