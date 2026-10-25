/** Proveedores reales, enrutador de datos y datos exclusivamente sintéticos para SCR-05. */
import { cleanup, configure, fireEvent, screen } from '@testing-library/react';
import { HttpResponse, http } from 'msw';
import { useState, type ReactNode } from 'react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach } from 'vitest';
import { createTransport, createPortConnection, installTransport } from '@/ports/base';
import type {
  Agreement,
  GateState,
  Plant,
  PlantPolicy,
  SignatoryPolicy,
  Transparency,
} from '@/ports/scr05Gates';
import { createScreenQueryCache, ScreenQueryProvider } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';
import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';
import { server } from '../../msw/server';

// Los límites de espera no son objeto de estas pruebas; toleran el runner compartido (retro 29).
configure({ asyncUtilTimeout: 10_000 });

export const PLANT = '0190a1b2-0505-7000-8000-000000000001';
export const ZONE = '0190a1b2-0505-7000-8000-000000000011';
export const OTHER_ZONE = '0190a1b2-0505-7000-8000-000000000012';
export const CAMERA = '0190a1b2-0505-7000-8000-0000000000c1';
export const AGREEMENT = '0190a1b2-0505-7000-8000-0000000000a1';
export const PENDING = '0190a1b2-0505-7000-8000-0000000000a2';
export const SELF = '0190a1b2-0505-7000-8000-0000000000e1';
export const OTHER_USER = '0190a1b2-0505-7000-8000-0000000000e2';
export const THIRD_USER = '0190a1b2-0505-7000-8000-0000000000e3';
const DOCUMENT = '0190a1b2-0505-7000-8000-0000000000d1';
export const INSTANT = '2026-01-01T12:00:00.000Z';
export const WAIT = { timeout: 10_000 };
export const ORIGIN = 'http://localhost';

/** Permisos del instalador que firma (carga, consulta y su propia firma). */
export const INSTALLER = [
  'catalog.read',
  'commissioning.run',
  'agreements.sign',
  'transparency.read',
] as const;

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
        { zone_id: ZONE, plant_id: PLANT, code: 'Z-05', name: 'Zona de prensas', node_id: null },
        {
          zone_id: OTHER_ZONE,
          plant_id: PLANT,
          code: 'Z-06',
          name: 'Zona de empaque',
          node_id: null,
        },
      ],
    },
  ];
}

export type Mode = 'pending' | 'commissioning' | 'productive' | 'usage_revoked';

/** Estado de compuertas sintético de la zona en cada fase. */
export function gateState(mode: Mode, zoneId = ZONE): GateState {
  const pending = { status: 'pending' as const, decided_at: null, decided_by: null };
  const approved = { status: 'approved' as const, decided_at: INSTANT, decided_by: SELF };
  return {
    zone_id: zoneId,
    plant_id: PLANT,
    mounting:
      mode === 'pending' ? { ...pending, record_id: null } : { ...approved, record_id: DOCUMENT },
    usage:
      mode === 'productive'
        ? { ...approved, agreement_id: AGREEMENT }
        : mode === 'usage_revoked'
          ? { status: 'revoked', decided_at: INSTANT, decided_by: SELF, agreement_id: AGREEMENT }
          : { ...pending, agreement_id: null },
    resulting_mode:
      mode === 'pending' ? 'no_capture' : mode === 'productive' ? 'productive' : 'commissioning',
    issued_at: mode === 'pending' ? null : INSTANT,
  };
}

export function plantPolicy(loaded: boolean): PlantPolicy {
  return loaded
    ? {
        loaded: true,
        policy_id: '0190a1b2-0505-7000-8000-0000000000b1',
        version: 2,
        signed_at: INSTANT,
        signed_by_display_name: 'Gerencia sintética',
        legal_opinion_reference: 'CONCEPTO-SINT-002',
        document_sha256: 'a'.repeat(64),
        criteria_summary_es: 'Criterios sintéticos de incerrables',
      }
    : { loaded: false };
}

export function signatoryPolicy(configured = true): SignatoryPolicy {
  return configured
    ? {
        plant_id: PLANT,
        configured: true,
        required_roles: ['plant_manager', 'coordinator_sst', 'copasst'],
        minimum: 3,
        workers_role: 'copasst',
        updated_by: SELF,
        updated_at: INSTANT,
      }
    : { plant_id: PLANT, configured: false, workers_role: 'copasst' };
}

export function agreement(overrides: Partial<Agreement> = {}): Agreement {
  return {
    agreement_id: PENDING,
    zone_id: ZONE,
    status: 'pending_signatures',
    signatories: [
      {
        role: 'plant_manager',
        user_id: SELF,
        display_name: 'Gerencia sintética',
        confirmed_at: null,
      },
      { role: 'coordinator_sst', user_id: OTHER_USER, display_name: null, confirmed_at: null },
      { role: 'copasst', user_id: THIRD_USER, display_name: null, confirmed_at: null },
    ],
    document_sha256: null,
    replaces_agreement_id: null,
    created_by: SELF,
    created_at: INSTANT,
    approved_at: null,
    approved_by: null,
    ...overrides,
  };
}

export function transparency(pending: string | null, mode: Mode = 'commissioning'): Transparency {
  const gates = gateState(mode);
  return {
    zone_id: ZONE,
    declared_scope: {
      scope_text_es: 'Alcance sintético de la zona de prensas',
      cameras: [],
      minimum_coverage: null,
    },
    standards: [
      {
        standard_id: '0190a1b2-0505-7000-8000-0000000000f1',
        version: 1,
        title_es: 'Estándar sintético de resguardo',
        declared_text: 'Texto declarado sintético',
      },
    ],
    current_agreement: null,
    gates: { mounting: gates.mounting, usage: gates.usage, resulting_mode: gates.resulting_mode },
    pending_confirmation_for_me: pending,
  };
}

function catalogVersion(cameras: readonly { camera_id: string; code: string }[]) {
  return {
    zone_id: ZONE,
    catalog_version: 1,
    issued_at: INSTANT,
    issued_by: SELF,
    role_in_use: 'administrator',
    reason_es: 'Motivo sintético de la versión',
    changed_fields: ['cameras'],
    superseded_at: null,
    single_occupancy: false,
    aggregation_window_minutes: null,
    catalog: { cameras: cameras.map((camera) => ({ ...camera, role_in_zone: 'primary' })) },
  };
}

export interface Scenario {
  gates?: (zoneId: string) => GateState;
  policy?: PlantPolicy;
  signatories?: SignatoryPolicy;
  transparency?: () => Transparency;
  cameras?: readonly { camera_id: string; code: string }[];
}

/** Lecturas de la pantalla con datos sintéticos; cada prueba las sustituye con `server.use`. */
export function serveReads(scenario: Scenario = {}) {
  const reads: string[] = [];
  server.use(
    http.get(`${ORIGIN}/zones/:zone_id/gates`, ({ params }) => {
      const zoneId = String(params.zone_id);
      reads.push(`gates:${zoneId}`);
      return HttpResponse.json((scenario.gates ?? (() => gateState('commissioning')))(zoneId));
    }),
    http.get(`${ORIGIN}/plants/:plant_id/policy`, () =>
      HttpResponse.json(scenario.policy ?? plantPolicy(true)),
    ),
    http.get(`${ORIGIN}/plants/:plant_id/signatory-policy`, () =>
      HttpResponse.json(scenario.signatories ?? signatoryPolicy()),
    ),
    http.get(`${ORIGIN}/zones/:zone_id/transparency`, () =>
      HttpResponse.json((scenario.transparency ?? (() => transparency(null)))()),
    ),
    http.get(`${ORIGIN}/zones/:zone_id/catalog`, () =>
      HttpResponse.json(
        catalogVersion(scenario.cameras ?? [{ camera_id: CAMERA, code: 'CAM-01' }]),
      ),
    ),
  );
  return reads;
}

/** SHA-256 de `abc` (hexadecimal y base64): el archivo sintético de las subidas. */
const ABC_SHA256 = 'ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad';
const ABC_BASE64 = 'ungWv48Bz+pBQUDeXa4iI7ADYaOWF3qctBD/YfIAFa0=';

export function syntheticFile(name: string, type: string): File {
  return new File(['abc'], name, { type });
}

/** Concesión de `POST /documents` y depósito sintético; devuelve los cuerpos de concesión. */
export function serveDocuments() {
  const grants: Record<string, unknown>[] = [];
  server.use(
    http.post(`${ORIGIN}/documents`, async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      grants.push(body);
      const documentId = `${DOCUMENT.slice(0, -1)}${String(grants.length)}`;
      const contentType = String(body.content_type);
      return HttpResponse.json(
        {
          document_id: documentId,
          upload: {
            method: 'PUT',
            url: `${ORIGIN}/synthetic-store`,
            required_headers: { 'content-type': contentType, 'x-amz-checksum-sha256': ABC_BASE64 },
            expires_at: '2026-01-01T12:15:00.000Z',
          },
          document_ref: {
            document_id: documentId,
            storage_key: `org/sintetica/${documentId}`,
            sha256: ABC_SHA256,
            content_type: contentType,
            size_bytes: 3,
          },
        },
        { status: 201 },
      );
    }),
    http.options(
      `${ORIGIN}/synthetic-store`,
      () =>
        new HttpResponse(null, {
          status: 200,
          headers: {
            'access-control-allow-origin': '*',
            'access-control-allow-methods': 'PUT',
            'access-control-allow-headers': 'content-type, x-amz-checksum-sha256',
          },
        }),
    ),
    http.put(
      `${ORIGIN}/synthetic-store`,
      () =>
        new HttpResponse(null, { status: 200, headers: { 'access-control-allow-origin': '*' } }),
    ),
  );
  return grants;
}

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
});

const ROUTES = ['/plants/:plant_id/gates', '/zones/:zone_id/gates', '/plants/:plant_id/policy'];

export async function gatesFixture(path: string, permissions: readonly string[] = INSTALLER) {
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
    userId: SELF,
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
  installTransport(createTransport({ clock, connection, origin: ORIGIN }));
  server.use(
    http.get(`${ORIGIN}/hierarchy`, () =>
      HttpResponse.json({ organization: me.organization, plants: plants(), nodes: [] }),
    ),
  );
  disposers.push(() => {
    session.dispose();
    cache.dispose();
  });
  function Wrapper({ children }: { readonly children: ReactNode }) {
    const [router] = useState(() =>
      createMemoryRouter(
        ROUTES.map((route) => ({
          path: route,
          element: (
            <SessionProvider store={session}>
              <ScreenQueryProvider cache={cache}>{children}</ScreenQueryProvider>
            </SessionProvider>
          ),
        })),
        { initialEntries: [path] },
      ),
    );
    return <RouterProvider router={router} />;
  }
  return { clock, cache, Wrapper };
}

/** Chip de compuertas (UI-17) dentro de `root`. */
export function chips(root: ParentNode = document): string[] {
  return [
    ...root.querySelectorAll(
      `[role="group"][aria-label="${text('components_status.gate.group_label')}"]`,
    ),
  ].map((chip) => chip.getAttribute('data-state') ?? '');
}

/** La parte `[data-part=name]` de `root`; falla la prueba si no existe. */
export function part(root: ParentNode, name: string): HTMLElement {
  const node = root.querySelector<HTMLElement>(`[data-part="${name}"]`);
  if (node === null) throw new Error(`Falta [data-part="${name}"].`);
  return node;
}

/** El elemento padre de `node`; falla la prueba si no lo tiene. */
export function parentOf(node: HTMLElement): HTMLElement {
  if (node.parentElement === null) throw new Error('El elemento no tiene padre.');
  return node.parentElement;
}

export function typeInto(labelText: string, value: string) {
  fireEvent.change(screen.getByLabelText(labelText), { target: { value } });
}
