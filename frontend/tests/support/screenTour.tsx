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

import { act, waitFor } from '@testing-library/react';
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
    const response = await getResponse([...extra, ...handlers], forwarded);
    if (response === undefined) return apiError('not_found', 404);
    if (rewrite === undefined || !response.headers.get('content-type')?.includes('json')) {
      return response;
    }
    // El cuerpo cambia de longitud: la cabecera original ya no vale.
    const headers = new Headers(response.headers);
    headers.delete('content-length');
    return HttpResponse.json(rewrite(await response.json()) as never, {
      status: response.status,
      headers,
    });
  });
}

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

/** Lo que impide dar la pantalla por pintada, o `null` si ya lo está. */
function pending(fixture: ShellFixture): string | null {
  const main = document.querySelector('main');
  if (main === null) return 'sin región principal';
  if (main.querySelector('h1') === null) return 'sin título de pantalla';
  if (main.textContent.includes(text('common.loading'))) return 'porción de ruta sin cargar';
  const fetching = fixture.services.cache.client
    .getQueryCache()
    .getAll()
    .filter((query) => query.state.fetchStatus === 'fetching')
    .map((query) => String(query.queryKey[1]));
  if (fetching.length > 0) return `lecturas en vuelo: ${fetching.join(', ')}`;
  if (document.querySelector('[aria-busy="true"]') !== null) return 'indicador de carga visible';
  return null;
}

/**
 * Espera a que la pantalla esté pintada: su título, ninguna porción de ruta ni lectura pendiente
 * y ningún indicador de carga. Entre comprobaciones avanza 500 ms el reloj simulado (los
 * reintentos de `ports.base` esperan en ese reloj) y deja correr el tiempo real (las porciones
 * de ruta se importan de verdad). Como mucho 30 s simulados: lejos del vencimiento de la sesión.
 */
async function quiet(fixture: ShellFixture): Promise<void> {
  let reason = 'sin comprobar';
  for (let round = 0; round < 60; round += 1) {
    await act(async () => {
      await settle(5);
      await new Promise((resolve) => setTimeout(resolve, 20));
    });
    const problem = pending(fixture);
    if (problem === null) return;
    reason = problem;
    act(() => {
      fixture.clock.advance(500);
    });
  }
  throw new Error(`La pantalla no terminó de pintarse: ${reason}`);
}

/** Monta `entry` sin `StrictMode` (como la construcción de producción) y espera la calma. */
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
  return fixture;
}
