/**
 * Arnés de las pruebas del armazón (VIG-320): los servicios reales de `createAppServices` con el
 * reloj simulado, el transporte real contra MSW, `App` con un enrutador en memoria y pantallas
 * sintéticas. Las respuestas de `GET /me` y `GET /hierarchy` son sintéticas.
 */
import { act, cleanup, render } from '@testing-library/react';
import { http, HttpResponse } from 'msw';
import { StrictMode, type ComponentType } from 'react';
import { createMemoryRouter, type RouteObject } from 'react-router';
import { afterEach } from 'vitest';

import { App } from '@/app/AppShell';
import { createAppServices, type AppServices } from '@/app/bootstrap';
import type { ScreenDescriptor } from '@/app/descriptor';
import { installGlobalErrorHandler, rootErrorOptions } from '@/app/GlobalErrorHandler';
import { createAppVersion } from '@/app/version';
import { installTransport } from '@/ports/base';
import type { HierarchyView, MeResponse } from '@/ports/shared';

import { server } from '../setup/msw';

import { FakeClock } from './fakeClock';
import { meResponse } from './session';

export const ORIGIN = 'http://localhost';
/** Tope holgado de las esperas: no es lo que se prueba (lecciones, VIG-300). */
export const WAIT = { timeout: 10_000 };
export const START = Date.parse('2026-01-01T12:00:00.000Z');

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
  localStorage.clear();
  sessionStorage.clear();
});

type Role = MeResponse['assignments'][number]['role'];

/** `GET /me` sintético: 30 min de inactividad y 12 h absolutas desde `START`. */
export function shellMe(
  permissions: readonly string[],
  options: { readonly kind?: string; readonly role?: Role } = {},
): MeResponse {
  const me = meResponse({
    idleExpiresAt: START + 1_800_000,
    absoluteExpiresAt: START + 43_200_000,
    permissions,
  });
  return {
    ...me,
    organization: { ...me.organization, kind: options.kind ?? 'client' },
    assignments: me.assignments.map((assignment) => ({
      ...assignment,
      role: options.role ?? 'coordinator_sst',
    })),
    api_version: import.meta.env.VITE_APP_VERSION,
  };
}

export const PLANT_ID = '0190a1b2-0000-7000-8000-0000000000a1';

/** `GET /hierarchy` sintético con una planta. */
export function shellHierarchy(): HierarchyView {
  return {
    organization: {
      organization_id: '0190a1b2-0000-7000-8000-0000000000f1',
      code: 'ORG-SINT',
      name: 'Organización sintética',
      kind: 'client',
    },
    plants: [
      {
        plant_id: PLANT_ID,
        code: 'PL-SINT',
        name: 'Planta sintética',
        timezone: 'America/Bogota',
        country: 'CO',
        zones: [],
      },
    ],
  } as unknown as HierarchyView;
}

/** `GET /me` y `GET /hierarchy` respondiendo con `me` (y la jerarquía sintética). */
export function serveSession(me: MeResponse): void {
  server.use(
    http.get(`${ORIGIN}/me`, () =>
      HttpResponse.json(me, {
        headers: { 'X-Vigia-Api-Version': import.meta.env.VITE_APP_VERSION },
      }),
    ),
    http.get(`${ORIGIN}/hierarchy`, () => HttpResponse.json(shellHierarchy())),
  );
}

/** Pantalla sintética de una sola ruta, visible con `permission`. */
export function syntheticScreen(options: {
  readonly id: ScreenDescriptor['screen_id'];
  readonly title: string;
  readonly path: string;
  readonly permission: string;
  readonly page: ComponentType;
  readonly home?: boolean;
}): ScreenDescriptor {
  return {
    screen_id: options.id,
    title_text: options.title,
    route: options.path,
    required_permissions: [options.permission],
    route_groups: [
      {
        id: 'main',
        paths: [options.path],
        access: 'permission',
        required_permissions: [options.permission],
        load: () => Promise.resolve({ default: options.page }),
      },
    ],
    sections: [],
    expected_roles: ['coordinator_sst'],
    backend_unit: 'U-04',
    home_for_roles: options.home === true ? ['coordinator_sst'] : [],
    home_group: options.home === true ? 'main' : null,
    device_classes: ['desktop'],
  };
}

export interface ShellFixture {
  readonly clock: FakeClock;
  readonly services: AppServices;
  readonly container: HTMLElement;
  /** Ruta actual del enrutador en memoria. */
  readonly route: () => string;
}

/** Monta `App` en `path` con servicios nuevos (sesión `anonymous` hasta que responda `GET /me`). */
export function mountShell(
  path: string,
  screens: readonly ScreenDescriptor[],
  options: { readonly strict?: boolean } = {},
): ShellFixture {
  const clock = new FakeClock(START);
  const services = createAppServices({
    clock,
    storage: localStorage,
    origin: ORIGIN,
    // Controlador de versión propio: una discrepancia de otra prueba no bloquea las escrituras.
    version: createAppVersion({ clock }),
    activityTarget: null,
  });
  installTransport(services.transport);
  disposers.push(installGlobalErrorHandler(window, services.errors, services.currentRoute));
  disposers.push(() => {
    services.dispose();
  });
  let first = true;
  // Un enrutador nuevo (otros permisos) arranca donde estaba el anterior.
  const createRouter = (routes: RouteObject[]) => {
    const entry = first ? path : services.currentRoute();
    first = false;
    return createMemoryRouter(routes, { initialEntries: [entry] });
  };
  const app = <App services={services} screens={screens} createRouter={createRouter} />;
  const view = render(options.strict === false ? app : <StrictMode>{app}</StrictMode>, {
    onCaughtError: rootErrorOptions.onCaughtError,
    onRecoverableError: rootErrorOptions.onRecoverableError,
  });
  return { clock, services, container: view.container, route: () => services.currentRoute() };
}

/** Avanza el reloj simulado dentro de `act`. */
export function advance(clock: FakeClock, ms: number): void {
  act(() => {
    clock.advance(ms);
  });
}
