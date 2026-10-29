/**
 * Recorrido de las pantallas registradas (VIG-336, LC-APP-28): cada grupo de rutas de cada
 * `ScreenDescriptor` de `SCREENS` se monta en su ruta de entrada sobre el armazón real
 * (`mountShell`: `App`, servicios, transporte y caché reales) con los manejadores de MSW
 * generados desde la OpenAPI. Lo usan la prueba de `axe` de todas las pantallas, la de datos
 * personales en el cliente y PR-APP-28.
 *
 * La sesión tiene todas las claves de la matriz salvo las de la plataforma, para que ninguna
 * sección quede oculta; los grupos públicos se montan sin sesión (`GET /me` en 401).
 */
import { readFileSync } from 'node:fs';
import { dirname, resolve } from 'node:path';
import { fileURLToPath } from 'node:url';

import { act, fireEvent, waitFor } from '@testing-library/react';
import { fromZonedTime } from 'date-fns-tz';
import { getResponse, http, HttpResponse, type HttpHandler } from 'msw';

import type { RouteGroup, ScreenDescriptor } from '@/app/descriptor';
import { SCREENS } from '@/app/screens';
import type { HierarchyView } from '@/ports/shared';
import { text } from '@/texts';

import { apiError, handlers } from '../msw/handlers';
import { server } from '../msw/server';

import {
  mountShell,
  ORIGIN,
  serveSession,
  shellHierarchy,
  shellMe,
  type ShellFixture,
} from './appShell';
import { esDate } from './dateFields';
import { settle } from './queryCache';

const WAIT = { timeout: 10_000 };

/** Valor sintético de cada parámetro de ruta declarado. */
const PARAMS: Readonly<Record<string, string>> = {
  zone_id: '0190a1b2-1111-7000-8000-000000000001',
  plant_id: '0190a1b2-0000-7000-8000-0000000000a1',
  finding_state_id: '0190a1b2-2222-7000-8000-000000000001',
  node_id: '0190a1b2-6666-7000-8000-000000000001',
  record_id: '0190a1b2-7777-7000-8000-000000000001',
  period: '2026-09',
  // Token de invitación sintético con la forma base64url del enlace, sin forma de secreto real.
  token: 'Sint_'.repeat(9),
};

/** Todas las claves de la matriz exportada salvo las de la plataforma (sin pantalla). */
const FIXTURES = resolve(
  dirname(fileURLToPath(import.meta.url)),
  '../../../backend/openapi/u05-fixtures.json',
);
const { permissions } = JSON.parse(readFileSync(FIXTURES, 'utf8')) as {
  readonly permissions: { readonly keys: readonly string[]; readonly platform_prefix: string };
};
const ALL_PERMISSIONS: readonly string[] = permissions.keys.filter(
  (key) => !key.startsWith(permissions.platform_prefix),
);

export interface ScreenEntry {
  readonly screen: ScreenDescriptor['screen_id'];
  readonly group: RouteGroup['id'];
  readonly access: RouteGroup['access'];
  /** Plantilla de la ruta de entrada del grupo (`/zones/:zone_id/coverage`). */
  readonly template: string;
  /** Ruta de entrada con los parámetros sintéticos. */
  readonly path: string;
}

/** `:param` sustituido por su valor sintético; un parámetro sin valor detiene la prueba. */
function fillRoute(template: string): string {
  return template.replace(/:([a-z_]+)/g, (_, name: string) => {
    const value = PARAMS[name];
    if (value === undefined) throw new Error(`Parámetro de ruta sin valor sintético: ${name}`);
    return value;
  });
}

/** Una entrada por grupo de rutas de cada pantalla registrada, en el orden de `SCREENS`. */
export const SCREEN_ENTRIES: readonly ScreenEntry[] = SCREENS.flatMap((descriptor) =>
  descriptor.route_groups.map((group) => ({
    screen: descriptor.screen_id,
    group: group.id,
    access: group.access,
    template: group.paths[0],
    path: fillRoute(group.paths[0]),
  })),
);

/** Organización de la sesión: el grupo del proveedor solo tiene sentido en una de proveedor. */
function organizationKind(entry: ScreenEntry): string {
  return entry.screen === 'SCR-02' && entry.group === 'provider' ? 'provider' : 'client';
}

export interface TourOptions {
  /** Manejadores (rutas relativas, como los generados) por delante de los generados. */
  readonly extra?: readonly HttpHandler[];
  /** Transforma el cuerpo JSON de cada respuesta antes de entregarlo a la aplicación. */
  readonly rewrite?: (body: unknown) => unknown;
}

/**
 * Relevo de MSW: el armazón de pruebas sirve la API en `ORIGIN` y los manejadores generados
 * responden en el origen de la página de jsdom; cada petición a `ORIGIN` se responde con el
 * manejador de su operación (`extra`, después el generado; `not_found` si no hay ninguno).
 */
function relay({ extra = [], rewrite }: TourOptions): HttpHandler {
  return http.all(`${ORIGIN}/*`, async ({ request }) => {
    const url = new URL(request.url);
    const body = ['GET', 'HEAD'].includes(request.method) ? null : await request.arrayBuffer();
    const forwarded = new Request(new URL(`${url.pathname}${url.search}`, location.origin), {
      method: request.method,
      headers: request.headers,
      body,
    });
    const response = await getResponse([...extra, ...TOUR_DATA, ...handlers], forwarded);
    if (response === undefined) return apiError('not_found', 404);
    if (!response.headers.get('content-type')?.includes('json')) return response;
    const transform = rewrite ?? ((value: unknown) => value);
    // El cuerpo cambia de longitud: la cabecera original ya no vale.
    const headers = new Headers(response.headers);
    headers.delete('content-length');
    return HttpResponse.json(transform(onePage(await response.json())) as never, {
      status: response.status,
      headers,
    });
  });
}

/**
 * Los ejemplos generados repiten el mismo cursor en cada página y los puertos rechazan un cursor
 * repetido («Cursor de revisión inválido o repetido.»): el recorrido sirve una sola página.
 */
function onePage(body: unknown): unknown {
  if (typeof body !== 'object' || body === null || Array.isArray(body)) return body;
  const page: Record<string, unknown> = { ...body };
  for (const field of ['next_cursor', 'next_after']) if (field in page) page[field] = null;
  return page;
}

const INSTANT = '2026-01-01T12:00:00.000Z';

/**
 * Catálogo de zona que cumple el esquema del contrato (el ejemplo genérico de la OpenAPI es un
 * diccionario libre y SCR-04 lo rechaza con «Catálogo fuera del contrato.»).
 */
function zoneCatalog(): Record<string, unknown> {
  const catalog = {
    zone_id: PARAMS.zone_id,
    zone_code: 'Z-SINT',
    plant_id: PARAMS.plant_id,
    organization_id: shellHierarchy().organization.organization_id,
    version: 1,
    issued_at: INSTANT,
    cameras: [
      {
        camera_id: PARAMS.node_id,
        code: 'CAM-SINT',
        role_in_zone: 'primary',
        declared_min_fps: 10,
      },
    ],
    minimum_coverage: { required_count: 1, required_camera_ids: [] },
    signals: [],
    standards: [
      {
        standard_id: PARAMS.record_id,
        version: 1,
        family: 'dwell',
        title_es: 'Estándar sintético',
        declared_text: 'Presencia en zona con energía afirmada.',
        declared_by: {
          user_id: PARAMS.plant_id,
          display_name: 'Responsable sintético',
          role: 'administrator',
        },
        effective_from: INSTANT,
        tier_policy: 'tier_1_when_signal_valid',
        predicate: {
          all_of: [{ presence: true }, { signal_role: 'energy', value: 'asserted' }],
          min_duration_ms: 1000,
        },
      },
    ],
    thresholds: { review: 0.4, publication: 0.8 },
    clip_window: { pre_seconds: 5, post_seconds: 5 },
    episode: { grouping_window_ms: 3000, max_segment_ms: 900000 },
    commissioning_watermark: true,
  };
  return {
    zone_id: PARAMS.zone_id,
    catalog_version: 1,
    issued_at: INSTANT,
    issued_by: PARAMS.plant_id,
    role_in_use: 'administrator',
    reason_es: 'Motivo sintético de publicación',
    changed_fields: ['standards'],
    superseded_at: null,
    single_occupancy: false,
    aggregation_window_minutes: 60,
    catalog,
  };
}

/**
 * El ejemplo generado de `GET /loop/findings/{id}` con la planta y la zona de la jerarquía: sin
 * ellas SCR-09 no conoce la zona horaria de la planta y deja el lazo en «Cargando…».
 */
const loopFinding = http.get('/loop/findings/:finding_state_id', async ({ request }) => {
  const response = await getResponse([...handlers], request);
  if (response === undefined) return apiError('not_found', 404);
  const body = (await response.json()) as Record<string, unknown>;
  const finding = { ...body, plant_id: PARAMS.plant_id, zone_id: PARAMS.zone_id };
  return HttpResponse.json(finding as never, { status: response.status });
});

/** Cambia, a cualquier profundidad, los campos de alcance y el periodo mensual de `value`. */
function inScope(value: unknown, period: string | null): unknown {
  if (Array.isArray(value)) return value.map((item) => inScope(item, period));
  if (typeof value !== 'object' || value === null) return value;
  const scope: Readonly<Record<string, string>> = {
    plant_id: PARAMS.plant_id ?? '',
    zone_id: PARAMS.zone_id ?? '',
    organization_id: shellHierarchy().organization.organization_id,
  };
  return Object.fromEntries(
    Object.entries(value).map(([key, item]) => {
      if (key in scope && typeof item === 'string') return [key, scope[key]];
      if (key === 'period' && typeof item === 'string' && period !== null) return [key, period];
      return [key, inScope(item, period)];
    }),
  );
}

/**
 * VIG-363: el resumen del gerente (SCR-13 `weekly`) se carga solo con la primera zona de la
 * jerarquía; los ejemplos generados traen otra planta, otra zona y otro mes, y SCR-13 los rechaza
 * por estar fuera de alcance. Se sirven con el alcance de la jerarquía y el mes pedido.
 */
const programInScope = [
  '/accreditation/metrics',
  '/accreditation/exposure',
  '/accreditation/adoption',
  '/loop/overdue',
].map((path) =>
  http.get(path, async ({ request }) => {
    const response = await getResponse([...handlers], request);
    if (response === undefined) return apiError('not_found', 404);
    const period = new URL(request.url).searchParams.get('period');
    const body = inScope(await response.json(), period) as Record<string, unknown>;
    if (period !== null && 'period_from' in body) {
      const timeZone = shellHierarchy().plants[0]?.timezone ?? 'UTC';
      const [year = 0, month = 0] = period.split('-').map(Number);
      const next =
        month === 12
          ? `${String(year + 1)}-01`
          : `${String(year)}-${String(month + 1).padStart(2, '0')}`;
      body.period_from = fromZonedTime(`${period}-01T00:00:00`, timeZone).toISOString();
      body.period_to = fromZonedTime(`${next}-01T00:00:00`, timeZone).toISOString();
    }
    return HttpResponse.json(body as never, { status: response.status });
  }),
);

/** Datos del recorrido por delante de los generados (rutas relativas, como ellos). */
const TOUR_DATA: readonly HttpHandler[] = [
  http.get('/zones/:zone_id/catalog', () => HttpResponse.json(zoneCatalog())),
  loopFinding,
  ...programInScope,
];

/** Jerarquía sintética con la planta y la zona de los parámetros de ruta. */
function tourHierarchy(): HierarchyView {
  const hierarchy = shellHierarchy();
  return {
    ...hierarchy,
    plants: hierarchy.plants.map((plant) => ({
      ...plant,
      zones: [
        {
          zone_id: PARAMS.zone_id,
          plant_id: plant.plant_id,
          code: 'Z-SINT',
          name: 'Zona sintética',
          node_id: null,
        },
      ],
    })),
  } as HierarchyView;
}

/**
 * Responde la API de `entry`: la sesión (o 401 si el grupo es público) y el resto por MSW, con
 * las opciones del recorrido (`TourOptions`).
 */
function serveEntry(entry: ScreenEntry, options: TourOptions = {}): void {
  server.use(relay(options));
  if (entry.access === 'public') {
    server.use(http.get(`${ORIGIN}/me`, () => apiError('unauthenticated', 401)));
    return;
  }
  serveSession(shellMe(ALL_PERMISSIONS, { kind: organizationKind(entry) }));
  server.use(http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(tourHierarchy())));
}

/** Textos de carga: con uno visible la pantalla aún no muestra su contenido. */
const LOADING_TEXTS: readonly string[] = [
  'common.loading',
  'common.loading_data',
  'components_status.loading.label',
  'screen_state.loading',
  'scr06.matrix.clips.loading',
  'scr08.detail.contract_loading',
].map((key) => text(key));

/**
 * Consultas auditadas (BR-APP-…: no se piden solas) que el recorrido pulsa para auditar el
 * contenido de la pantalla y no solo su botón de consulta.
 */
const REVEAL: Readonly<Record<string, RevealStep>> = {
  'SCR-06 record': { button: 'scr06.record.request' },
  'SCR-08 labels': {
    fill: { from: '2025-12-01', to: '2026-01-01' },
    button: 'scr08.labels.submit',
  },
  // Clasificar y el lazo abren cargados (VIG-365): sin consulta que pulsar.
  'SCR-09 loop/closure': { button: 'scr09.closure.load' },
  'SCR-09 loop/unclosable': { button: 'scr09.closure.load' },
  'SCR-09 loop/reopening': { button: 'scr09.closure.load' },
  // VIG-363: con un análisis en curso la pantalla ofrece «Continuar» en vez de «Iniciar».
  'SCR-11 root_cause': { button: 'scr11.start', orButton: 'scr11.continue' },
};

interface RevealStep {
  /** Campos (`name` → valor) que la consulta exige antes de pulsarla. */
  readonly fill?: Readonly<Record<string, string>>;
  /** Clave del texto del botón de la consulta. */
  readonly button: string;
  /** Otro botón que abre lo mismo según lo que diga el servidor; se pulsa el que aparezca. */
  readonly orButton?: string;
}

/** Texto visible del botón de consulta auditada que el recorrido pulsa en `entry`, si lo hay. */
export function revealButton(entry: ScreenEntry): string | null {
  const step = REVEAL[`${entry.screen} ${entry.group}`];
  return step === undefined ? null : text(step.button);
}

/**
 * Lo que impide auditar la pantalla montada: el estado de error de sección o un texto de carga;
 * `null` si muestra su contenido. Las consultas auditadas las pulsa y comprueba `mountEntry`.
 */
export function unrevealed(): string | null {
  const main = document.querySelector('main');
  if (main === null) return 'sin región principal';
  if (main.textContent.includes(text('screen_state.error'))) return 'estado de error de sección';
  const loading = LOADING_TEXTS.find((value) => main.textContent.includes(value));
  return loading === undefined ? null : `texto de carga visible: «${loading}»`;
}

function findButton(label: string): HTMLButtonElement | undefined {
  return [...document.querySelectorAll<HTMLButtonElement>('main button')].find(
    (button) => button.textContent.trim() === label,
  );
}

/** Las lecturas de la caché que siguen en vuelo (las que esperan un reintento en el reloj simulado). */
function inFlight(fixture: ShellFixture): string[] {
  return fixture.services.cache.client
    .getQueryCache()
    .getAll()
    .filter((query) => query.state.fetchStatus === 'fetching')
    .map((query) => String(query.queryKey[1]));
}

/** Lo que impide dar la pantalla por pintada, o `null` si ya lo está. */
function pending(fixture: ShellFixture): string | null {
  const main = document.querySelector('main');
  if (main === null) return 'sin región principal';
  if (main.querySelector('h1') === null) return 'sin título de pantalla';
  const loading = LOADING_TEXTS.find((value) => main.textContent.includes(value));
  if (loading !== undefined) return `texto de carga visible: «${loading}»`;
  const fetching = inFlight(fixture);
  if (fetching.length > 0) return `lecturas en vuelo: ${fetching.join(', ')}`;
  if (document.querySelector('[aria-busy="true"]') !== null) return 'indicador de carga visible';
  return null;
}

/**
 * Plazo real para lo que corre en tiempo real (las porciones de ruta se importan de verdad):
 * cada vuelta espera al menos `REAL_STEP_MS` reales, así que las vueltas acotan el plazo por
 * abajo sin leer la hora (PAT-APP-RES-06).
 */
const REAL_BUDGET_MS = 20_000;
const REAL_STEP_MS = 20;
/** Plazo simulado: los reintentos de `ports.base`; lejos del vencimiento de la sesión. */
const SIMULATED_BUDGET_MS = 30_000;
const SIMULATED_STEP_MS = 500;

/**
 * Espera a que la pantalla esté pintada: su título, ninguna porción de ruta ni lectura pendiente
 * y ningún indicador de carga. Los dos plazos van por separado: el reloj simulado solo avanza
 * (500 ms por vuelta, hasta 30 s) mientras hay lecturas en vuelo, que son las que esperan en él;
 * una porción de ruta que aún se importa espera en tiempo real, hasta 20 s, sin tocar ese reloj.
 */
async function quiet(fixture: ShellFixture): Promise<void> {
  let simulated = 0;
  let reason = 'sin comprobar';
  for (
    let round = 0;
    round < REAL_BUDGET_MS / REAL_STEP_MS && simulated <= SIMULATED_BUDGET_MS;
    round += 1
  ) {
    await act(async () => {
      await settle(5);
      await new Promise((resolve) => setTimeout(resolve, REAL_STEP_MS));
    });
    const problem = pending(fixture);
    if (problem === null) return;
    reason = problem;
    if (inFlight(fixture).length > 0) {
      simulated += SIMULATED_STEP_MS;
      act(() => {
        fixture.clock.advance(SIMULATED_STEP_MS);
      });
    }
  }
  throw new Error(`La pantalla no terminó de pintarse: ${reason}`);
}

/**
 * Monta `entry` sin `StrictMode` (como la construcción de producción), espera la calma y pulsa
 * sus consultas auditadas (`revealButtons`), esperando la calma tras cada una.
 */
export async function mountEntry(
  entry: ScreenEntry,
  options: TourOptions = {},
): Promise<ShellFixture> {
  serveEntry(entry, options);
  const fixture = mountShell(entry.path, SCREENS, { strict: false });
  await waitFor(() => {
    if (document.querySelector('main') === null) throw new Error('sin región principal');
  }, WAIT);
  await quiet(fixture);
  const step = REVEAL[`${entry.screen} ${entry.group}`];
  if (step !== undefined) {
    const label = text(step.button);
    for (const [name, value] of Object.entries(step.fill ?? {})) {
      // Los campos de fecha propios (VIG-366) se escriben `dd/mm/aaaa` en su campo visible; el
      // nombre lo lleva un campo oculto.
      const typed = document.querySelector<HTMLInputElement>(`main [data-field-name="${name}"]`);
      const field = typed ?? document.querySelector<HTMLInputElement>(`main [name="${name}"]`);
      if (field === null) throw new Error(`Sin el campo «${name}» de la consulta «${label}»`);
      act(() => {
        fireEvent.change(field, { target: { value: typed === null ? value : esDate(value) } });
      });
    }
    const other = step.orButton === undefined ? null : text(step.orButton);
    // La pantalla puede decidir su botón con una lectura propia (SCR-11): se espera a que aparezca.
    const button = await waitFor(() => {
      const found = findButton(label) ?? (other === null ? undefined : findButton(other));
      if (found === undefined) throw new Error(`Sin el botón de consulta «${label}»`);
      return found;
    }, WAIT);
    const before = document.querySelector('main')?.textContent ?? '';
    act(() => {
      fireEvent.click(button);
    });
    await quiet(fixture);
    // La consulta tiene que haber pintado algo, y no un aviso de validación: si no, axe
    // auditaría solo el formulario de consulta.
    if ((document.querySelector('main')?.textContent ?? '') === before)
      throw new Error(`La consulta «${label}» no pintó contenido`);
    if (document.querySelector('main [aria-invalid="true"]') !== null)
      throw new Error(`La consulta «${label}» quedó con un campo inválido`);
  }
  return fixture;
}
