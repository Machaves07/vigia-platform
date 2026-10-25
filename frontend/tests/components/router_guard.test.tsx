/**
 * Guarda del enrutador (BR-APP-03, 77; NFR-APP-23) y precarga de código (PAT-APP-REN-02).
 *
 * - Toda ruta sin permiso alcanzada por URL pinta exactamente el mismo árbol que una ruta que no
 *   existe (se compara el DOM serializado, no los textos), y no carga la porción de la pantalla.
 * - Con permiso, la ruta pinta su propia página y **no** el árbol de «no disponible en su alcance».
 * - `/` lleva al panel de inicio del rol más operativo; sin sesión, al inicio de sesión.
 * - Precargar el inicio y sus enlazadas, y navegar por ellas, no dispara ninguna petición (MSW
 *   cuenta cero).
 */
import { render, screen } from '@testing-library/react';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { createMemoryRouter, RouterProvider } from 'react-router';

import type { NavigationPrincipal } from '@/app/navigation';
import { afterFirstPaint, createAppRoutes, preloadGroups, preloadHomeCode } from '@/app/router';
import { SCREENS } from '@/app/screens';
import { Bell } from '@/pages/scr15/Bell';
import { ScreenState } from '@/pages/shared/ScreenState';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { ScreenQueryProvider, type ScreenQueryCache } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';

import { effectivePermissions } from '../generators';
import { server } from '../setup/msw';
import { queryFixture } from '../support/queryCache';

const ID = '0192d2c4-7a10-7c3e-8a51-3b1f0e9d2a44';
const NOT_FOUND = '/esta-ruta-no-existe/ni-esta';

const coordinator: NavigationPrincipal = {
  effective_permissions: effectivePermissions('customer_organization', ['coordinator_sst']),
  assignments: [{ role: 'coordinator_sst' }],
};
const nobody: NavigationPrincipal = { effective_permissions: [], assignments: [] };

/**
 * Ruta concreta: cada parámetro `:x` por un identificador o un periodo.
 */
const concrete = (path: string): string =>
  path.replace(/:period\b/, '2026-09').replace(/:[a-z_]+/g, ID);

const caches: ScreenQueryCache[] = [];
/** `SessionStore` no se reexporta en el barril de `state.session`: se toma del constructor. */
type SessionStore = ReturnType<typeof createSessionStore>;
const stores: SessionStore[] = [];

/**
 * Lo que el armazón da a las pantallas ya implementadas y esta prueba necesita montar: el
 * transporte (SCR-07 lee su `Clock` del transporte, `ports.base`), la caché de consultas
 * (VIG-293, `useScreenQuery`) y la sesión de UI (VIG-320, `useUiSession`). El almacén se monta
 * **sin cargar** la sesión (`GET /me` no se pide: esta prueba es de enrutado) y MSW responde las
 * lecturas de las pantallas con sus manejadores generados; la prueba no afirma nada de esos datos.
 */
function shellProviders(path: string) {
  const { cache, clock } = queryFixture();
  const connection = createPortConnection(clock);
  installTransport(createTransport({ clock, connection, devLog: () => undefined }));
  const store = createSessionStore({
    clock,
    port: {
      me: { read: () => Promise.reject(new Error('la guarda no carga la sesión')) },
      logout: { write: () => Promise.resolve() },
    },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => path,
    navigate: () => undefined,
    storages: [],
  });
  return { cache, store };
}

async function renderAt(principal: NavigationPrincipal | null, path: string) {
  const { cache, store } = shellProviders(path);
  caches.push(cache);
  stores.push(store);
  const router = createMemoryRouter(createAppRoutes(principal), { initialEntries: [path] });
  const view = render(
    <SessionProvider store={store}>
      <ScreenQueryProvider cache={cache}>
        <RouterProvider router={router} />
      </ScreenQueryProvider>
    </SessionProvider>,
  );
  // Tope amplio: la porción `lazy` llega por importación dinámica y no es lo que se prueba.
  await screen.findByRole('main', {}, { timeout: 10_000 });
  return { html: view.container.innerHTML, router, unmount: view.unmount };
}

let requests: string[];
const onRequest = ({ request }: { request: Request }) => {
  requests.push(`${request.method} ${request.url}`);
};

beforeEach(() => {
  requests = [];
  server.events.on('request:start', onRequest);
});

afterEach(() => {
  server.events.removeListener('request:start', onRequest);
  for (const cache of caches.splice(0)) cache.dispose();
  for (const store of stores.splice(0)) store.dispose();
  installTransport(null);
});

describe('guarda: sin permiso, el mismo árbol que not_found', () => {
  const guarded = SCREENS.flatMap((descriptor) =>
    descriptor.route_groups
      .filter((group) => group.access === 'permission')
      .flatMap((group) =>
        group.paths.map((path) => [`${descriptor.screen_id} ${group.id} ${path}`, group, path]),
      ),
  ) as [string, (typeof SCREENS)[number]['route_groups'][number], string][];

  it.each(guarded)('%s', async (_name, group, path) => {
    const loader = vi.spyOn(group, 'load');
    const missing = await renderAt(nobody, NOT_FOUND);
    missing.unmount();
    const denied = await renderAt(nobody, concrete(path));
    expect(denied.html).toBe(missing.html);
    expect(denied.html).toContain(text('errors.not_available_in_scope'));
    expect(loader).not.toHaveBeenCalled();
    expect(requests).toEqual([]);
  });

  it('cuenta y administración sin sesión también son not_found; el inicio de sesión no', async () => {
    const missing = await renderAt(null, NOT_FOUND);
    missing.unmount();
    const account = await renderAt(null, '/account');
    expect(account.html).toBe(missing.html);
    account.unmount();
    const login = await renderAt(null, '/login');
    expect(login.html).not.toBe(missing.html);
    expect(
      await screen.findByRole(
        'heading',
        { level: 1, name: text('scr01.login.title') },
        { timeout: 10_000 },
      ),
    ).toBeDefined();
  });
});

describe('con permiso, cada grupo pinta su página', () => {
  // Las rutas públicas (inicio de sesión e invitación, VIG-321) ya no son relevos: piden al
  // servidor al montarse y las prueban `tests/components/scr01/`.
  const open = SCREENS.flatMap((descriptor) =>
    descriptor.route_groups
      .filter((group) => group.access !== 'public')
      .flatMap((group) =>
        group.paths.map((path) => [
          `${descriptor.screen_id} ${group.id} ${path}`,
          descriptor,
          path,
        ]),
      ),
  ) as [string, (typeof SCREENS)[number], string][];

  const everything: NavigationPrincipal = {
    effective_permissions: [...new Set(SCREENS.flatMap((s) => s.required_permissions))],
    assignments: [],
  };

  it.each(open)('%s', async (_name, _descriptor, path) => {
    const missing = await renderAt(everything, NOT_FOUND);
    missing.unmount();
    // Una ruta que no existe no pide nada al servidor.
    expect(requests).toEqual([]);
    const page = await renderAt(everything, concrete(path));
    expect(page.html).not.toBe(missing.html);
    // El grupo pinta **su** página y no el árbol de «no disponible en su alcance» (BR-APP-03, 77).
    // No se exige un encabezado concreto: cada pantalla titula la suya —unas con el nombre de
    // navegación del descriptor y otras con el suyo (SCR-07, SCR-08, SCR-09)— y una pantalla que
    // todavía no tiene sesión pinta su estado de carga, sin encabezado.
    expect(
      screen.queryByRole('heading', { level: 1, name: text('errors.not_available_in_scope') }),
    ).toBeNull();
  });
});

describe('inicio', () => {
  it('/ lleva al panel de inicio del coordinador', async () => {
    const { router } = await renderAt(coordinator, '/');
    expect(router.state.location.pathname).toBe('/home');
    // El panel de inicio pinta el título de su propia página, no el nombre de navegación.
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(text('scr08.home.title'));
  });

  it('/ sin sesión lleva al inicio de sesión', async () => {
    const { router } = await renderAt(null, '/');
    expect(router.state.location.pathname).toBe('/login');
  });

  it('/ sin panel de inicio es «no disponible en su alcance»', async () => {
    const missing = await renderAt(nobody, NOT_FOUND);
    missing.unmount();
    const root = await renderAt(nobody, '/');
    expect(root.html).toBe(missing.html);
  });
});

describe('precarga solo de código (PAT-APP-REN-02)', () => {
  it('el coordinador precarga su inicio y las pantallas enlazadas, sin ninguna petición', async () => {
    const groups = preloadGroups(coordinator);
    const ids = groups.map((group) => group.id);
    expect(ids[0]).toBe('home');
    expect(ids).toEqual(expect.arrayContaining(['classify', 'loop', 'queue', 'root_cause']));
    const loads = groups.map((group) => vi.spyOn(group, 'load'));
    await preloadHomeCode(coordinator);
    for (const load of loads) expect(load).toHaveBeenCalledTimes(1);
    // La precarga es solo de código: no pide ningún dato (PAT-APP-REN-02). Al abrir después cada
    // pantalla, la pantalla pide su propia lectura y la prueba de esa pantalla lo comprueba.
    expect(requests).toEqual([]);
    const { router } = await renderAt(coordinator, '/');
    await router.navigate(`/findings/${ID}/classify`);
    await router.navigate('/review-queue');
    await router.navigate(`/findings/${ID}/root-cause`);
  });

  it('no precarga grupos sin permiso de las pantallas enlazadas', () => {
    const lineManager: NavigationPrincipal = {
      effective_permissions: effectivePermissions('customer_organization', ['line_manager']),
      assignments: [{ role: 'line_manager' }],
    };
    expect(preloadGroups(lineManager).map((group) => group.id)).toEqual(['entry']);
    expect(preloadGroups(nobody)).toEqual([]);
  });

  it('se programa tras la primera pintura, no en el acto, y se puede cancelar', async () => {
    const task = vi.fn();
    afterFirstPaint(task);
    expect(task).not.toHaveBeenCalled();
    await vi.waitFor(
      () => {
        expect(task).toHaveBeenCalledTimes(1);
      },
      { timeout: 10_000 },
    );
    const cancelled = vi.fn();
    const cancel = afterFirstPaint(cancelled);
    cancel();
    const witness = vi.fn();
    afterFirstPaint(witness);
    await vi.waitFor(
      () => {
        expect(witness).toHaveBeenCalledTimes(1);
      },
      { timeout: 10_000 },
    );
    expect(cancelled).not.toHaveBeenCalled();
  });
});

describe('estados comunes y campana de relevo', () => {
  it('loading es un esqueleto sin números; empty y error con su texto', () => {
    const loading = render(<ScreenState state="loading" />);
    expect(loading.container.textContent).not.toMatch(/\d/);
    expect(screen.getByRole('status').getAttribute('aria-busy')).toBe('true');
    loading.unmount();
    render(<ScreenState state="empty" message="Aún no hay datos." />);
    expect(screen.getByText('Aún no hay datos.')).toBeDefined();
    const retry = vi.fn();
    render(<ScreenState state="error" correlationId="c-1" onRetry={retry} />);
    expect(screen.getByRole('alert').textContent).toContain('c-1');
    screen.getByRole('button', { name: text('screen_state.retry') }).click();
    expect(retry).toHaveBeenCalledTimes(1);
  });

  it('la campana de relevo enlaza las notificaciones sin pedir nada', async () => {
    const router = createMemoryRouter([{ path: '/', element: <Bell /> }]);
    render(<RouterProvider router={router} />);
    const link = await screen.findByRole(
      'link',
      { name: text('navigation.bell') },
      { timeout: 10_000 },
    );
    expect(link.getAttribute('href')).toBe('/notifications');
    expect(requests).toEqual([]);
  });
});
