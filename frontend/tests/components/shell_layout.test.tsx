/**
 * Sistema visual v1 (VIG-364): menú agrupado por tarea, barra inferior de la tableta y cabecera.
 *
 * - El grupo es solo presentación: para cada rol, las entradas agrupadas más las de la cabecera son
 *   exactamente las de `navItems` (BR-APP-01), sin repetir ni perder ninguna.
 * - En tableta no hay menú lateral: la barra inferior lleva al inicio y a los primeros destinos, y
 *   «Menú» despliega el menú completo, que se pliega al navegar o con Escape.
 *
 * Las aserciones comparan claves del catálogo, rutas y atributos exactos; nunca subcadenas con
 * datos (VIG-194). Datos sintéticos.
 */
import { cleanup, screen, waitFor, within } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import type { ReactNode } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { Role } from '@/app/descriptor';
import { buildNavigationMap } from '@/app/navigation';
import { menuSections, navItems, tabItems, MENU_PROFILES, itemLabel } from '@/app/NavMenu';
import { SCREENS } from '@/app/screens';
import { TABLET_QUERY } from '@/app/useTabletLayout';
import { text } from '@/texts';
import { label } from '@/texts/labels';

import { effectivePermissions } from '../generators';
import { mountShell, serveSession, shellMe, syntheticScreen, WAIT } from '../support/appShell';

const ROLES: readonly Role[] = [
  'coordinator_sst',
  'administrator',
  'provider_installer',
  'plant_manager',
  'line_manager',
  'copasst',
];

afterEach(() => {
  cleanup();
  vi.unstubAllGlobals();
});

function navigationOf(role: Role) {
  const context = role === 'provider_installer' ? 'provider_organization' : 'customer_organization';
  const principal = {
    effective_permissions: effectivePermissions(context, [role]),
    assignments: [{ role }],
  };
  return buildNavigationMap(principal, SCREENS);
}

const slot = (item: { screen_id: string; group: string }) => `${item.screen_id}:${item.group}`;

describe('menú agrupado por tarea (sistema visual v1, «Estructura»)', () => {
  it.each(ROLES)('%s: agrupar no añade, no repite ni pierde entradas', (role) => {
    const navigation = navigationOf(role);
    const items = navItems(navigation, SCREENS);
    const grouped = menuSections(items, navigation.home).flatMap((section) => section.items);
    expect(new Set(grouped.map(slot)).size).toBe(grouped.length);
    const inHeader = items.filter((item) => !grouped.includes(item)).map(slot);
    expect(inHeader.every((entry) => ['SCR-01:account', 'SCR-15:list'].includes(entry))).toBe(true);
    expect(grouped.every((item) => items.includes(item))).toBe(true);
  });

  it('el coordinador: Mi trabajo, Planta y Expediente, con «Inicio» primero; el resto en «Más»', () => {
    const navigation = navigationOf('coordinator_sst');
    const sections = menuSections(navItems(navigation, SCREENS), navigation.home);
    expect(
      sections.map((section) => [
        section.title_text,
        section.items.map((item) => [itemLabel(item, navigation.home), item.path]),
      ]),
    ).toEqual([
      [
        'navigation.menu.group.work',
        [
          ['navigation.menu.home', '/home'],
          ['navigation.menu.findings', '/findings'],
          ['navigation.menu.review_queue', '/review-queue'],
        ],
      ],
      [
        'navigation.menu.group.plant',
        [
          ['navigation.menu.coverage', '/zones/:zone_id/coverage'],
          ['navigation.menu.monthly', '/zones/monthly'],
          ['navigation.menu.metrics', '/metrics'],
          ['navigation.menu.weekly', '/metrics/weekly'],
        ],
      ],
      [
        'navigation.menu.group.record',
        [
          ['navigation.menu.integrity', '/integrity'],
          ['navigation.menu.exports', '/exports'],
          ['navigation.menu.transparency', '/transparency'],
        ],
      ],
      [
        'navigation.menu.group.more',
        [
          ['navigation.menu.hierarchy', '/admin/hierarchy'],
          ['navigation.menu.catalog', '/zones/:zone_id/catalog'],
          ['navigation.menu.admissions', '/plants/:plant_id/admissions'],
          ['navigation.menu.gates', '/zones/:zone_id/gates'],
          ['navigation.menu.policy', '/plants/:plant_id/policy'],
        ],
      ],
    ]);
  });

  it('el gerente empieza por su resumen semanal, llamado «Inicio»', () => {
    const navigation = navigationOf('plant_manager');
    const [first] = menuSections(navItems(navigation, SCREENS), navigation.home);
    expect(first?.title_text).toBe('navigation.menu.group.summary');
    expect(first?.items.map((item) => [itemLabel(item, navigation.home), item.path])).toEqual([
      ['navigation.menu.home', '/metrics/weekly'],
      ['navigation.menu.metrics', '/metrics'],
      ['navigation.menu.monthly', '/zones/monthly'],
    ]);
  });

  it('el administrador: personas y estructura, equipos, reglas y expediente', () => {
    const navigation = navigationOf('administrator');
    const sections = menuSections(navItems(navigation, SCREENS), navigation.home);
    const titles = sections.map((section) => section.title_text);
    expect(titles.slice(0, 4)).toEqual([
      'navigation.menu.group.people',
      'navigation.menu.group.equipment',
      'navigation.menu.group.rules',
      'navigation.menu.group.record',
    ]);
    // El inicio del administrador (la flota) encabeza el primer grupo.
    expect(sections[0]?.items[0]?.path).toBe('/fleet');
    expect(
      sections.find((section) => section.id === 'rules')?.items.map((item) => item.title_text),
    ).toEqual(['navigation.menu.catalog', 'navigation.menu.vocabulary', 'navigation.menu.policy']);
  });

  it.each(['line_manager', 'copasst', 'provider_installer'] as const)(
    '%s: menú corto, sin encabezados de grupo',
    (role) => {
      const navigation = navigationOf(role);
      const sections = menuSections(navItems(navigation, SCREENS), navigation.home);
      expect(MENU_PROFILES[role]).toBeUndefined();
      expect(sections.map((section) => section.title_text)).toEqual([null]);
      expect(sections[0]?.items[0]?.path).toBe(navigation.home?.path);
    },
  );
});

describe('barra inferior de la tableta', () => {
  it.each(ROLES)('%s: inicio primero, cuatro pestañas como máximo y sin selectores', (role) => {
    const navigation = navigationOf(role);
    const items = navItems(navigation, SCREENS);
    const { tabs, more } = tabItems(items, navigation.home);
    expect(tabs.length).toBeGreaterThan(0);
    expect(tabs.length).toBeLessThanOrEqual(4);
    expect(tabs[0]?.path).toBe(navigation.home?.path);
    expect(tabs.every((item) => item.select === undefined)).toBe(true);
    const grouped = menuSections(items, navigation.home).flatMap((section) => section.items);
    expect(more).toBe(grouped.some((item) => !tabs.includes(item)));
  });

  it('el COPASST lleva sus notificaciones en la barra', () => {
    const navigation = navigationOf('copasst');
    const { tabs } = tabItems(navItems(navigation, SCREENS), navigation.home);
    expect(tabs.map(slot)).toContain('SCR-15:list');
  });
});

function Page({ title }: { readonly title: string }): ReactNode {
  return (
    <main>
      <h1>{title}</h1>
    </main>
  );
}

const TABLET_SCREENS = [
  syntheticScreen({
    id: 'SCR-08',
    title: 'navigation.scr08',
    path: '/findings',
    permission: 'findings.read',
    page: () => <Page title="Bandeja sintética" />,
    home: true,
  }),
  syntheticScreen({
    id: 'SCR-10',
    title: 'navigation.scr10',
    path: '/review-queue',
    permission: 'review_queue.resolve',
    page: () => <Page title="Cola sintética" />,
  }),
  syntheticScreen({
    id: 'SCR-12',
    title: 'navigation.scr12',
    path: '/zones/monthly',
    permission: 'monthly_package.read',
    page: () => <Page title="Paquete sintético" />,
  }),
  syntheticScreen({
    id: 'SCR-13',
    title: 'navigation.scr13',
    path: '/metrics',
    permission: 'metrics.read',
    page: () => <Page title="Métricas sintéticas" />,
  }),
  syntheticScreen({
    id: 'SCR-14',
    title: 'navigation.scr14',
    path: '/exports',
    permission: 'export.create',
    page: () => <Page title="Exportación sintética" />,
  }),
];

const TABLET_PERMISSIONS = [
  'findings.read',
  'review_queue.resolve',
  'monthly_package.read',
  'metrics.read',
  'export.create',
];

/** `matchMedia` de una ventana de 768 px: solo la consulta de la tableta coincide. */
function stubTablet(matches: boolean): void {
  vi.stubGlobal('matchMedia', (query: string) => ({
    matches: matches && query === TABLET_QUERY,
    media: query,
    onchange: null,
    addEventListener: () => undefined,
    removeEventListener: () => undefined,
    addListener: () => undefined,
    removeListener: () => undefined,
    dispatchEvent: () => false,
  }));
}

describe('armazón en tableta (768 px)', { timeout: 30_000 }, () => {
  it('sin menú lateral: barra inferior y «Menú» que se pliega al navegar y con Escape', async () => {
    stubTablet(true);
    serveSession(shellMe(TABLET_PERMISSIONS));
    const fixture = mountShell('/findings', TABLET_SCREENS);
    await screen.findByRole('heading', { name: 'Bandeja sintética' }, WAIT);
    const tabs = screen.getByRole('navigation', { name: text('navigation.menu.tabs') });
    expect(screen.queryByRole('navigation', { name: text('app_shell.nav.label') })).toBeNull();
    expect(
      within(tabs)
        .getAllByRole('link')
        .map((link) => [link.textContent, link.getAttribute('href')]),
    ).toEqual([
      // El orden es el del menú agrupado del coordinador: Mi trabajo, Planta, Expediente y Más.
      [text('navigation.menu.home'), '/findings'],
      [text('navigation.scr12'), '/zones/monthly'],
      [text('navigation.scr14'), '/exports'],
      [text('navigation.scr10'), '/review-queue'],
    ]);
    const user = userEvent.setup();
    const toggle = within(tabs).getByRole('button', { name: text('navigation.menu.open') });
    expect(toggle.getAttribute('aria-expanded')).toBe('false');
    await user.click(toggle);
    const menu = screen.getByRole('navigation', { name: text('app_shell.nav.label') });
    expect(toggle.getAttribute('aria-expanded')).toBe('true');
    expect(toggle.getAttribute('aria-controls')).toBe(menu.id);
    expect(toggle.textContent).toBe(text('navigation.menu.close'));
    await user.keyboard('{Escape}');
    expect(screen.queryByRole('navigation', { name: text('app_shell.nav.label') })).toBeNull();
    await user.click(toggle);
    await user.click(
      within(screen.getByRole('navigation', { name: text('app_shell.nav.label') })).getByRole(
        'link',
        { name: text('navigation.scr13') },
      ),
    );
    await screen.findByRole('heading', { name: 'Métricas sintéticas' }, WAIT);
    expect(fixture.route()).toBe('/metrics');
    await waitFor(() => {
      expect(screen.queryByRole('navigation', { name: text('app_shell.nav.label') })).toBeNull();
    }, WAIT);
  });

  it('en escritorio: menú lateral y ninguna barra inferior', async () => {
    stubTablet(false);
    serveSession(shellMe(TABLET_PERMISSIONS));
    mountShell('/findings', TABLET_SCREENS);
    await screen.findByRole('heading', { name: 'Bandeja sintética' }, WAIT);
    expect(screen.getByRole('navigation', { name: text('app_shell.nav.label') })).toBeDefined();
    expect(screen.queryByRole('navigation', { name: text('navigation.menu.tabs') })).toBeNull();
  });
});

describe('cabecera', { timeout: 30_000 }, () => {
  it('muestra el rol con el que se trabaja junto a «Cuenta», nunca el nombre', async () => {
    serveSession(shellMe(TABLET_PERMISSIONS, { role: 'coordinator_sst' }));
    const fixture = mountShell('/findings', TABLET_SCREENS);
    await screen.findByRole('heading', { name: 'Bandeja sintética' }, WAIT);
    const banner = screen.getByRole('banner');
    expect(banner.querySelector('[data-part="role"]')?.textContent).toBe(
      label('role', 'coordinator_sst'),
    );
    expect(fixture.container.querySelector('[data-part="role"]')?.textContent).not.toBe(
      text('common.unknown'),
    );
  });
});
