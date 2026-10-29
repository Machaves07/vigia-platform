/**
 * VIG-363 (hallazgos 18, 19 y 22 de VIG-338): navegación por rol.
 *
 * - Cada rol tiene su inicio y nadie más que el coordinador recibe su panel ni la miga «Bandeja
 *   de hallazgos» sin poder abrir la bandeja.
 * - El menú del administrador tiene todas sus pantallas con el nombre de la página que abre; las
 *   que necesitan zona o planta abren un selector del alcance, sin consultas en la dirección.
 * - «No disponible en su alcance» explica y lleva al inicio del rol (el mando, a su paquete).
 *
 * Las aserciones comparan claves del catálogo, rutas y atributos exactos; nunca subcadenas con
 * datos (VIG-194). Datos sintéticos.
 */
import { act, cleanup, render, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { HttpResponse, http } from 'msw';
import type { ReactNode } from 'react';
import { MemoryRouter, Route, Routes, useParams } from 'react-router';
import { afterEach, describe, expect, it } from 'vitest';
import { axe } from 'vitest-axe';

import type { Role, ScreenDescriptor } from '@/app/descriptor';
import { buildNavigationMap } from '@/app/navigation';
import { navItems } from '@/app/NavMenu';
import { SCREENS } from '@/app/screens';
import { FindingsNav } from '@/pages/scr08/shared';
import { NotAvailableInScope } from '@/pages/shared/ScreenState';
import { createSessionStore, SessionProvider } from '@/state/session';
import { text } from '@/texts';

import { effectivePermissions } from '../generators';
import { server } from '../setup/msw';
import {
  mountShell,
  ORIGIN,
  PLANT_ID,
  serveSession,
  shellHierarchy,
  shellMe,
  WAIT,
} from '../support/appShell';
import { FakeClock } from '../support/fakeClock';
import { queryFixture } from '../support/queryCache';
import { meResponse } from '../support/session';

const ZONE_A = '0190a1b2-1111-7000-8000-000000000363';
const ZONE_B = '0190a1b2-1111-7000-8000-000000000364';

afterEach(() => {
  cleanup();
});

function principal(role: Role) {
  return {
    effective_permissions: effectivePermissions('customer_organization', [role]),
    assignments: [{ role }],
  };
}

function itemsOf(role: Role) {
  return navItems(buildNavigationMap(principal(role), SCREENS), SCREENS);
}

describe('menú por rol con la tabla real (BR-APP-01, 04)', () => {
  it('el administrador: usuarios, jerarquía, admisiones, compuertas y catálogo con su nombre', () => {
    expect(
      itemsOf('administrator').map((item) => [
        item.screen_id,
        item.title_text,
        item.path,
        item.select ?? null,
      ]),
    ).toEqual([
      ['SCR-01', 'navigation.menu.account', '/account', null],
      ['SCR-01', 'navigation.menu.users', '/admin/users', null],
      ['SCR-01', 'navigation.menu.hierarchy', '/admin/hierarchy', null],
      ['SCR-01', 'navigation.menu.organization', '/admin/organization', null],
      ['SCR-02', 'navigation.scr02', '/concessions', null],
      ['SCR-03', 'navigation.menu.integrity', '/integrity', null],
      ['SCR-03', 'navigation.menu.coverage', '/zones/:zone_id/coverage', 'zone_id'],
      ['SCR-04', 'navigation.menu.catalog', '/zones/:zone_id/catalog', 'zone_id'],
      ['SCR-04', 'navigation.menu.admissions', '/plants/:plant_id/admissions', 'plant_id'],
      ['SCR-04', 'navigation.menu.vocabulary', '/admin/vocabulary', null],
      ['SCR-05', 'navigation.menu.gates', '/zones/:zone_id/gates', 'zone_id'],
      ['SCR-05', 'navigation.menu.policy', '/plants/:plant_id/policy', 'plant_id'],
      ['SCR-07', 'navigation.menu.fleet', '/fleet', null],
      ['SCR-13', 'navigation.menu.metrics', '/metrics', null],
      ['SCR-15', 'navigation.menu.notifications', '/notifications', null],
      ['SCR-16', 'navigation.menu.transparency', '/transparency', null],
    ]);
  });

  it('«Catálogo de estándares» ya no abre el vocabulario: cada entrada tiene el nombre de su página', () => {
    const catalog = itemsOf('administrator').filter((item) => item.screen_id === 'SCR-04');
    expect(catalog.find((item) => item.title_text === 'navigation.scr04')).toBeUndefined();
    expect(catalog.find((item) => item.path === '/admin/vocabulary')?.title_text).toBe(
      'navigation.menu.vocabulary',
    );
  });

  it.each([
    [
      'coordinator_sst',
      [
        ['navigation.menu.coordinator_home', '/home'],
        ['navigation.menu.findings', '/findings'],
      ],
    ],
    ['plant_manager', [['navigation.menu.findings', '/findings']]],
    ['administrator', []],
    ['line_manager', []],
    ['copasst', []],
  ] as const)('las entradas de hallazgos de %s', (role, expected) => {
    const findings = itemsOf(role).filter((item) => item.screen_id === 'SCR-08');
    expect(findings.map((item) => [item.title_text, item.path])).toEqual(expected);
  });

  it('nadie salvo el coordinador tiene en el menú el panel del coordinador', () => {
    const roles: Role[] = [
      'administrator',
      'plant_manager',
      'line_manager',
      'copasst',
      'provider_installer',
    ];
    for (const role of roles) {
      expect(itemsOf(role).map((item) => item.path)).not.toContain('/home');
    }
  });

  it('el gerente tiene su resumen semanal en el menú, con el nombre de la página', () => {
    expect(
      itemsOf('plant_manager')
        .filter((item) => item.screen_id === 'SCR-13')
        .map((item) => [item.title_text, item.path]),
    ).toEqual([
      ['navigation.menu.metrics', '/metrics'],
      ['navigation.menu.weekly', '/metrics/weekly'],
    ]);
  });
});

/** Pantalla sintética con una entrada de menú que necesita zona (como el catálogo). */
function ZonePage(): ReactNode {
  const { zone_id = '' } = useParams();
  return (
    <main>
      <h1>Página sintética de la zona</h1>
      <p data-part="zone">{zone_id}</p>
    </main>
  );
}

const ZONE_SCREENS: readonly ScreenDescriptor[] = [
  {
    screen_id: 'SCR-04',
    title_text: 'navigation.scr04',
    route: '/zones/:zone_id/catalog',
    required_permissions: ['catalog.read'],
    route_groups: [
      {
        id: 'catalog',
        paths: ['/zones/:zone_id/catalog'],
        access: 'permission',
        required_permissions: ['catalog.read'],
        menu: { title_text: 'scr04.catalog.title', select: 'zone_id' },
        load: () => Promise.resolve({ default: ZonePage }),
      },
    ],
    sections: [],
    expected_roles: ['administrator'],
    backend_unit: 'U-03',
    home_for_roles: [],
    home_group: null,
    device_classes: ['desktop'],
  },
];

describe('entrada con selector de zona (VIG-363)', { timeout: 60_000 }, () => {
  it('abre el selector del alcance y «Abrir» lleva a la zona elegida, sin consulta', async () => {
    serveSession(shellMe(['catalog.read']));
    const hierarchy = shellHierarchy();
    server.use(
      http.get(`${ORIGIN}/hierarchy`, () =>
        HttpResponse.json({
          ...hierarchy,
          plants: [
            {
              ...hierarchy.plants[0],
              zones: [
                { zone_id: ZONE_A, plant_id: PLANT_ID, code: 'Z-A', name: 'Zona sintética A' },
                { zone_id: ZONE_B, plant_id: PLANT_ID, code: 'Z-B', name: 'Zona sintética B' },
              ],
            },
          ],
        }),
      ),
    );
    const shell = mountShell('/', ZONE_SCREENS);
    const nav = await screen.findByRole('navigation', { name: text('app_shell.nav.label') }, WAIT);
    const entry = within(nav).getByRole('button', { name: text('scr04.catalog.title') });
    expect(entry.getAttribute('aria-expanded')).toBe('false');
    const user = userEvent.setup();
    await user.click(entry);
    expect(entry.getAttribute('aria-expanded')).toBe('true');
    const form = within(nav).getByRole('form', { name: text('scr04.catalog.title') });
    const open = within(form).getByRole('button', { name: text('role_navigation.open') });
    expect(open).toHaveProperty('disabled', true);
    const select = within(form).getByRole('combobox', { name: text('role_navigation.zone') });
    await waitFor(() => {
      expect(
        [...select.querySelectorAll('option')].map((option) => option.getAttribute('value')),
      ).toEqual(['', ZONE_A, ZONE_B]);
    }, WAIT);
    await user.selectOptions(select, ZONE_B);
    await user.click(open);
    await screen.findByRole('heading', { name: 'Página sintética de la zona' }, WAIT);
    expect(shell.route()).toBe(`/zones/${ZONE_B}/catalog`);
    expect(document.querySelector('[data-part="zone"]')?.textContent).toBe(ZONE_B);
    expect(entry.getAttribute('aria-expanded')).toBe('false');
    expect(await axe(nav)).toHaveNoViolations();
  });
});

async function withSession(role: Role | null, element: ReactNode) {
  const { cache, clock } = queryFixture();
  const now = new FakeClock(clock.now());
  const me = (session: Role) => {
    const base = meResponse({
      idleExpiresAt: now.now() + 1_800_000,
      absoluteExpiresAt: now.now() + 8 * 3_600_000,
      permissions: effectivePermissions('customer_organization', [session]),
    });
    return {
      ...base,
      assignments: base.assignments.map((assignment) => ({ ...assignment, role: session })),
    };
  };
  const store = createSessionStore({
    clock,
    port: {
      me: {
        read: () =>
          role === null ? Promise.reject(new Error('sin sesión')) : Promise.resolve(me(role)),
      },
      logout: { write: () => Promise.resolve() },
    },
    cache,
    clearPreferences: () => undefined,
    currentRoute: () => '/',
    navigate: () => undefined,
    storages: [],
  });
  if (role !== null) {
    await act(async () => {
      await store.load();
    });
  }
  const view = render(
    <SessionProvider store={store}>
      <MemoryRouter>
        <Routes>
          <Route path="*" element={element} />
        </Routes>
      </MemoryRouter>
    </SessionProvider>,
  );
  return {
    ...view,
    dispose: () => {
      store.dispose();
    },
  };
}

describe('«No disponible en su alcance» orienta (BR-APP-03, 77)', () => {
  it('el mando: explicación y enlace a su paquete mensual', async () => {
    const { container, dispose } = await withSession('line_manager', <NotAvailableInScope />);
    expect(screen.getByRole('heading', { level: 1 }).textContent).toBe(
      text('errors.not_available_in_scope'),
    );
    expect(container.querySelector('[data-part="scope_explanation"]')?.textContent).toBe(
      text('role_navigation.scope_explanation'),
    );
    const link = screen.getByRole('link', {
      name: `${text('role_navigation.go_home')}: ${text('navigation.menu.monthly')}`,
    });
    expect(link.getAttribute('href')).toBe('/zones/monthly');
    expect(await axe(container)).toHaveNoViolations();
    dispose();
  });

  it.each([
    ['plant_manager', 'navigation.menu.weekly', '/metrics/weekly'],
    ['copasst', 'navigation.menu.transparency', '/transparency'],
    ['administrator', 'navigation.menu.fleet', '/fleet'],
    ['coordinator_sst', 'navigation.menu.coordinator_home', '/home'],
  ] as const)('%s: lleva a su inicio %s', async (role, title, path) => {
    const { container, dispose } = await withSession(role, <NotAvailableInScope />);
    const link = container.querySelector('a[data-part="own_home"]');
    expect(link?.getAttribute('href')).toBe(path);
    expect(link?.textContent).toBe(`${text('role_navigation.go_home')}: ${text(title)}`);
    dispose();
  });

  it('sin sesión: la misma explicación y «Volver al inicio»', async () => {
    const { container, dispose } = await withSession(null, <NotAvailableInScope />);
    expect(container.querySelector('a[data-part="own_home"]')).toBeNull();
    expect(
      screen.getByRole('link', { name: text('screen_state.back_home') }).getAttribute('href'),
    ).toBe('/');
    expect(container.querySelector('[data-part="scope_explanation"]')).not.toBeNull();
    dispose();
  });
});

describe('miga del panel y la bandeja (hallazgo 18)', () => {
  function crumbs(container: HTMLElement) {
    return [...container.querySelectorAll('nav a')].map((link) => [
      link.textContent,
      link.getAttribute('href'),
    ]);
  }

  it('el coordinador: su inicio y la bandeja', async () => {
    const { container, dispose } = await withSession('coordinator_sst', <FindingsNav />);
    expect(crumbs(container)).toEqual([
      [text('scr08.home.title'), '/home'],
      [text('scr08.inbox.title'), '/findings'],
    ]);
    dispose();
  });

  it('el gerente: solo la bandeja, nunca «Inicio del coordinador»', async () => {
    const { container, dispose } = await withSession('plant_manager', <FindingsNav />);
    expect(crumbs(container)).toEqual([[text('scr08.inbox.title'), '/findings']]);
    dispose();
  });

  it.each(['line_manager', 'copasst', 'administrator'] as const)(
    '%s: ninguna miga (no tiene bandeja)',
    async (role) => {
      const { container, dispose } = await withSession(role, <FindingsNav />);
      expect(container.querySelector('nav')).toBeNull();
      dispose();
    },
  );
});
