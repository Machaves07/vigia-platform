/**
 * Acceso a los episodios de la sombra (TASK-574; A-112, QF10; BR-GOB-30; BR-APP-01, 03, 77):
 * solo `commissioning.run`, que en la matriz tiene únicamente la instaladora (en su organización
 * o bajo concesión). Sin la clave (coordinador, gerente, administrador, jefe de línea, COPASST),
 * la ruta no aparece en el menú y, entrando por URL, el listado y el detalle pintan «no
 * disponible en su alcance» **sin pedir nada a la API** y sin cargar la porción del grupo.
 */
import { render, screen } from '@testing-library/react';
import { createMemoryRouter, RouterProvider } from 'react-router';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { navItems } from '@/app/NavMenu';
import { buildNavigationMap, type NavigationPrincipal } from '@/app/navigation';
import { createAppRoutes } from '@/app/router';
import { SCREENS } from '@/app/screens';
import { createPortConnection, createTransport, installTransport } from '@/ports/base';
import { ScreenQueryProvider, type ScreenQueryCache } from '@/state/query';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';

import { effectivePermissions, type PermissionContext } from '../../generators';
import { server } from '../../setup/msw';
import { queryFixture } from '../../support/queryCache';

const ZONE = '0190a1b2-0000-7000-8000-000000000401';
const PHOTO = '0190a1b2-0000-7000-8000-000000000402';
const LIST = `/zones/${ZONE}/commissioning/episodes`;
const DETAIL = `${LIST}/${PHOTO}`;

const group = SCREENS.find((screen) => screen.screen_id === 'SCR-06')?.route_groups.find(
  (candidate) => candidate.id === 'episodes',
);

function principalOf(context: PermissionContext, role: string): NavigationPrincipal {
  return {
    effective_permissions: effectivePermissions(context, [role]),
    assignments: [{ role }],
  };
}

const WITHOUT_KEY = [
  'coordinator_sst',
  'plant_manager',
  'administrator',
  'line_manager',
  'copasst',
];

type SessionStore = ReturnType<typeof createSessionStore>;
const caches: ScreenQueryCache[] = [];
const stores: SessionStore[] = [];
let requests: string[];
const onRequest = ({ request }: { request: Request }) => {
  requests.push(`${request.method} ${new URL(request.url).pathname}`);
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
  vi.restoreAllMocks();
});

/** Enrutador real con la sesión sin cargar (`GET /me` no se pide: la prueba es de enrutado). */
async function renderAt(principal: NavigationPrincipal, path: string) {
  const { cache, clock } = queryFixture();
  installTransport(
    createTransport({ clock, connection: createPortConnection(clock), devLog: () => undefined }),
  );
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
  await screen.findByRole('main', {}, { timeout: 10_000 });
  return view;
}

describe('sin commissioning.run (QF10, BR-GOB-30)', () => {
  it('el grupo exige exactamente commissioning.run', () => {
    expect(group?.access).toBe('permission');
    expect(group?.required_permissions).toEqual(['commissioning.run']);
  });

  it.each(WITHOUT_KEY)('%s: la matriz no le da la clave', (role) => {
    expect(principalOf('customer_organization', role).effective_permissions).not.toContain(
      'commissioning.run',
    );
  });

  it.each(WITHOUT_KEY.flatMap((role) => [LIST, DETAIL].map((path) => [role, path])))(
    '%s en %s: «no disponible en su alcance», sin peticiones ni porción',
    async (role, path) => {
      if (group === undefined) throw new Error('Falta el grupo episodes de SCR-06');
      const loader = vi.spyOn(group, 'load');
      const view = await renderAt(principalOf('customer_organization', role), path);
      expect(
        screen.getByRole('heading', { level: 1, name: text('errors.not_available_in_scope') }),
      ).toBeDefined();
      expect(view.container.querySelector('canvas, img')).toBeNull();
      expect(loader).not.toHaveBeenCalled();
      expect(requests).toEqual([]);
    },
  );

  it.each(WITHOUT_KEY)('%s: el menú no ofrece los episodios', (role) => {
    const principal = principalOf('customer_organization', role);
    const items = navItems(buildNavigationMap(principal, SCREENS), SCREENS);
    expect(items.filter((item) => item.group === 'episodes')).toEqual([]);
  });
});

describe('la instaladora bajo concesión', () => {
  it('el menú ofrece «Episodios de la sombra» con el selector de zona', () => {
    const principal = principalOf('customer_under_concession', 'provider_installer');
    expect(principal.effective_permissions).toContain('commissioning.run');
    const items = navItems(buildNavigationMap(principal, SCREENS), SCREENS);
    expect(items.filter((item) => item.group === 'episodes')).toEqual([
      {
        screen_id: 'SCR-06',
        group: 'episodes',
        title_text: 'navigation.menu.episodes',
        select: 'zone_id',
        path: '/zones/:zone_id/commissioning/episodes',
      },
    ]);
  });

  it('entrar por URL carga la porción del grupo y no pinta «no disponible»', async () => {
    if (group === undefined) throw new Error('Falta el grupo episodes de SCR-06');
    const loader = vi.spyOn(group, 'load');
    await renderAt(principalOf('customer_under_concession', 'provider_installer'), LIST);
    expect(loader).toHaveBeenCalled();
    expect(
      screen.queryByRole('heading', { level: 1, name: text('errors.not_available_in_scope') }),
    ).toBeNull();
  });
});
