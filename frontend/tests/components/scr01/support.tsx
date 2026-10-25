/**
 * Arnés de las pruebas de SCR-01 (sesión, VIG-321): transporte real con el reloj simulado y los
 * ganchos de sesión conectados como los conecta el armazón, almacén de sesión real, enrutador en
 * memoria en modo de datos con `SessionGate` como ruta de diseño y MSW. Datos sintéticos.
 */
import { act, cleanup, render } from '@testing-library/react';
import { StrictMode, type ReactNode } from 'react';
import { createMemoryRouter, Navigate, RouterProvider, type RouteObject } from 'react-router';
import { afterEach, expect } from 'vitest';
import { axe } from 'vitest-axe';

import { createAppRoutes } from '@/app/router';
import { createAppVersion } from '@/app/version';
import LoginPage from '@/pages/scr01/login';
import InvitationPage from '@/pages/scr01/invitation';
import { SessionGate } from '@/pages/scr01/SessionGate';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { sessionPort, type MeResponse } from '@/ports/shared';
import { createSessionStore, SessionProvider } from '@/state/session';
import type { SessionStore } from '@/state/session/store';

import { FakeClock } from '../../support/fakeClock';
import { meResponse } from '../../support/session';

const INSTANT = '2026-01-01T12:00:00.000Z';
export const ORIGIN = 'http://localhost';
/** Tope holgado de las esperas: no es lo que se prueba (lecciones, VIG-300). */
export const WAIT = { timeout: 10_000 };

const disposers: (() => void)[] = [];
afterEach(() => {
  cleanup();
  for (const dispose of disposers.splice(0)) dispose();
  installTransport(null);
  localStorage.clear();
  sessionStorage.clear();
});

/**
 * `GET /me` sintético con 30 min de inactividad y 12 h absolutas desde `INSTANT`, y la versión
 * de la API igual a la de esta construcción (si no, `app.version` bloquearía las escrituras).
 */
export function currentMe(): MeResponse {
  const now = Date.parse(INSTANT);
  return {
    ...meResponse({ idleExpiresAt: now + 1_800_000, absoluteExpiresAt: now + 43_200_000 }),
    api_version: import.meta.env.VITE_APP_VERSION,
    privacy_notice_version: 'v0-pendiente',
  };
}

/** Ruta de prueba con sesión: pide `GET /me` al pulsar, como haría cualquier pantalla. */
function Probe({ session }: { readonly session: SessionStore }): ReactNode {
  return (
    <main>
      <h1>Pantalla sintética</h1>
      <button
        type="button"
        onClick={() => {
          void session.load().catch(() => undefined);
        }}
      >
        Consultar
      </button>
    </main>
  );
}

/**
 * - `harness`: las rutas de sesión reales más `/home` y `/zones/:zone_id` sintéticas;
 * - `app`: el árbol real de `createAppRoutes(null)` (sin `Principal`), como al entrar.
 */
type RouteSet = 'harness' | 'app';

function routesFor(set: RouteSet, session: SessionStore): RouteObject[] {
  const children: RouteObject[] =
    set === 'app'
      ? createAppRoutes(null)
      : [
          { path: '/', element: <Navigate to="/home" replace /> },
          { path: '/login', Component: LoginPage },
          { path: '/login/second-factor', Component: LoginPage },
          { path: '/login/enroll', Component: LoginPage },
          { path: '/invitations/:token', Component: InvitationPage },
          {
            path: '/home',
            element: (
              <main>
                <h1>Inicio sintético</h1>
              </main>
            ),
          },
          { path: '/zones/:zone_id', element: <Probe session={session} /> },
        ];
  return [{ Component: SessionGate, children }];
}

export interface Fixture {
  readonly clock: FakeClock;
  readonly session: SessionStore;
  readonly router: ReturnType<typeof createMemoryRouter>;
  readonly container: HTMLElement;
  readonly unmount: () => void;
}

/** Monta la aplicación en `path` con un almacén de sesión nuevo (estado `anonymous`). */
export function mount(
  path: string,
  options: { readonly routes?: RouteSet; readonly before?: (session: SessionStore) => void } = {},
): Fixture {
  const clock = new FakeClock(Date.parse(INSTANT));
  const connection = createPortConnection(clock);
  let router: ReturnType<typeof createMemoryRouter> | null = null;
  const session = createSessionStore({
    clock,
    port: sessionPort,
    cache: { clearAll: () => undefined },
    clearPreferences: () => undefined,
    currentRoute: () =>
      router === null ? path : `${router.state.location.pathname}${router.state.location.search}`,
    navigate: (route) => {
      void router?.navigate(route);
    },
  });
  installTransport(
    createTransport({
      clock,
      connection,
      origin: ORIGIN,
      // Controlador de versión propio: una discrepancia de otra prueba no bloquea las escrituras.
      version: createAppVersion({ clock }),
      onSessionEvent: session.transportHooks.onSessionEvent,
      onAuthenticatedResponse: session.transportHooks.onAuthenticatedResponse,
    }),
  );
  options.before?.(session);
  router = createMemoryRouter(routesFor(options.routes ?? 'harness', session), {
    initialEntries: [path],
  });
  const view = render(
    <StrictMode>
      <SessionProvider store={session}>
        <RouterProvider router={router} />
      </SessionProvider>
    </StrictMode>,
  );
  disposers.push(() => {
    session.dispose();
  });
  return { clock, session, router, container: view.container, unmount: view.unmount };
}

/** Avanza el reloj simulado dentro de `act`. */
export function advance(clock: FakeClock, ms: number): void {
  act(() => {
    clock.advance(ms);
  });
}

/** Violaciones de `axe` serias o críticas (NFR-APP-50). */
type Violations = Awaited<ReturnType<typeof axe>>['violations'];

export async function seriousViolations(container: HTMLElement): Promise<Violations> {
  const results = await axe(container);
  return results.violations.filter(
    (violation) => violation.impact === 'serious' || violation.impact === 'critical',
  );
}

/** Todo lo que guardan `localStorage` y `sessionStorage`: claves y valores. */
export function storedText(): string {
  const parts: string[] = [];
  for (const storage of [localStorage, sessionStorage]) {
    for (let index = 0; index < storage.length; index += 1) {
      const key = storage.key(index) ?? '';
      parts.push(key, storage.getItem(key) ?? '');
    }
  }
  return parts.join('\n');
}

/**
 * El árbol serializado con los identificadores de `useId` renombrados por orden de aparición:
 * React los numera con un contador global, así que dos montajes iguales difieren solo en ellos.
 */
export function normalizedTree(element: Element): string {
  const seen = new Map<string, string>();
  return element.outerHTML.replace(/«r[0-9a-z]+»|:r[0-9a-z]+:|_r_[0-9a-z]+_/g, (id) => {
    const known = seen.get(id) ?? `id${String(seen.size)}`;
    seen.set(id, known);
    return known;
  });
}

/** Ruta actual del enrutador (con la consulta). */
export function location(fixture: Fixture): string {
  const { pathname, search } = fixture.router.state.location;
  return `${pathname}${search}`;
}

export function expectNoSerious(violations: Violations): void {
  expect(violations.map((violation) => violation.id)).toEqual([]);
}
