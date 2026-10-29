/**
 * UI-07 `NavMenu` (BR-APP-01 y 03, IF-05): las entradas de las pantallas visibles de
 * `NavigationMap`, en el orden fijo de `SCREENS`. Una pantalla sin permiso no aparece; alcanzada
 * por URL, la guarda del enrutador pinta «no disponible en su alcance».
 *
 * - Una pantalla con grupos de entrada propia (`menu`, VIG-363) da una entrada por cada uno que la
 *   persona puede abrir, con el nombre de la página que abre. Si la ruta lleva zona o planta, la
 *   entrada abre un selector de las zonas o plantas del alcance (`GET /hierarchy`) y «Abrir» lleva
 *   a la de la elegida; la dirección nunca lleva consultas.
 * - Las demás pantallas dan una sola entrada, a su primera ruta que la persona puede abrir: con
 *   sesión (no pública), sin parámetros y con su grupo visible. Una pantalla visible sin esa ruta
 *   (solo con rutas de detalle) no tiene entrada: no hay adónde llevar.
 * - El panel de inicio de otro rol (`home_only`, VIG-363) no es una entrada: el enrutador lleva a la
 *   persona a su propio inicio.
 *
 * Presentación (sistema visual v1, VIG-364): las mismas entradas, agrupadas por tarea según el rol
 * del inicio (`MENU_PROFILES`: coordinación, gerencia y administración), con «Inicio» primero. El
 * grupo es solo presentación: qué entradas hay lo deciden los permisos, y una entrada visible que
 * no está en ningún grupo del perfil va en «Más». Los demás roles tienen un menú corto, sin grupos.
 * «Mi cuenta» y «Notificaciones» están en la cabecera y no se repiten en el menú agrupado. En
 * tableta (`useTabletLayout`) no hay menú lateral: una barra inferior lleva a «Inicio» y a los
 * primeros destinos del rol, y «Menú» despliega el menú completo.
 */
import { useEffect, useId, useState, type ReactNode } from 'react';
import { NavLink, useLocation, useNavigate } from 'react-router';

import type { GroupMenu, Role, RouteGroup, ScreenDescriptor } from '@/app/descriptor';
import type { HomeScreen, NavigationMap } from '@/app/navigation';
import { sessionPort } from '@/ports/shared';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

import styles from './AppShell.module.css';
import { useTabletLayout } from './useTabletLayout';

export interface NavItem {
  readonly screen_id: string;
  /** Grupo de rutas de la entrada; único dentro de la pantalla. */
  readonly group: string;
  readonly title_text: string;
  /** Dirección de la entrada; con `select`, la plantilla con su parámetro. */
  readonly path: string;
  readonly select?: GroupMenu['select'];
}

function ownEntry(screen: string, group: RouteGroup, menu: GroupMenu): NavItem | null {
  if (menu.select === undefined) {
    const path = group.paths.find((candidate) => !candidate.includes(':'));
    return path === undefined ? null : { screen_id: screen, group: group.id, ...menu, path };
  }
  const parameter = `:${menu.select}`;
  const path = group.paths.find(
    (candidate) => candidate.includes(parameter) && candidate.split(':').length === 2,
  );
  return path === undefined ? null : { screen_id: screen, group: group.id, ...menu, path };
}

/** Las entradas del menú: las de las pantallas visibles, en el orden del mapa. */
export function navItems(
  navigation: NavigationMap,
  screens: readonly ScreenDescriptor[],
): NavItem[] {
  return navigation.screens.flatMap((entry) => {
    if (!entry.visible) return [];
    const descriptor = screens.find((screen) => screen.screen_id === entry.screen_id);
    if (descriptor === undefined) return [];
    const isHome = (group: RouteGroup) =>
      navigation.home?.screen_id === entry.screen_id && navigation.home.group === group.id;
    const open = descriptor.route_groups.filter(
      (group) =>
        group.access !== 'public' &&
        entry.groups.some((candidate) => candidate.id === group.id && candidate.visible) &&
        (group.home_only !== true || isHome(group)),
    );
    if (descriptor.route_groups.some((group) => group.menu !== undefined)) {
      return open.flatMap((group) => {
        const item = group.menu === undefined ? null : ownEntry(entry.screen_id, group, group.menu);
        return item === null ? [] : [item];
      });
    }
    for (const group of open) {
      const path = group.paths.find((candidate) => !candidate.includes(':'));
      if (path !== undefined) {
        return [
          { screen_id: entry.screen_id, group: group.id, title_text: entry.title_text, path },
        ];
      }
    }
    return [];
  });
}

/** Grupo de un perfil: sus entradas como `pantalla:grupo` (`pantalla:*`, cualquier grupo). */
interface SectionSpec {
  readonly id: string;
  readonly title_text: string;
  readonly slots: readonly string[];
}

/**
 * Menú agrupado por tarea de los roles con muchas pantallas (sistema visual v1, «Estructura»). El
 * inicio del rol va primero aunque no esté aquí; lo que la sesión no puede abrir simplemente no
 * aparece. «Lazos de cierre» (VIG-365) es la bandeja con los estados de lazo vivo.
 */
export const MENU_PROFILES: Readonly<Partial<Record<Role, readonly SectionSpec[]>>> = {
  coordinator_sst: [
    {
      id: 'work',
      title_text: 'navigation.menu.group.work',
      slots: ['SCR-08:home', 'SCR-08:inbox', 'SCR-10:queue', 'SCR-08:loops'],
    },
    {
      id: 'plant',
      title_text: 'navigation.menu.group.plant',
      slots: ['SCR-03:coverage', 'SCR-12:*', 'SCR-13:metrics', 'SCR-13:weekly'],
    },
    {
      id: 'record',
      title_text: 'navigation.menu.group.record',
      slots: ['SCR-03:integrity', 'SCR-14:*', 'SCR-16:*'],
    },
  ],
  plant_manager: [
    {
      id: 'summary',
      title_text: 'navigation.menu.group.summary',
      slots: ['SCR-13:weekly', 'SCR-13:metrics', 'SCR-12:*'],
    },
    {
      id: 'tracking',
      title_text: 'navigation.menu.group.tracking',
      slots: ['SCR-08:inbox', 'SCR-08:loops', 'SCR-10:queue', 'SCR-03:coverage'],
    },
    {
      id: 'providers',
      title_text: 'navigation.menu.group.providers',
      slots: ['SCR-02:*', 'SCR-03:integrity', 'SCR-14:*', 'SCR-16:*'],
    },
  ],
  administrator: [
    {
      id: 'people',
      title_text: 'navigation.menu.group.people',
      slots: ['SCR-01:users', 'SCR-01:hierarchy', 'SCR-01:organization', 'SCR-05:gates'],
    },
    {
      id: 'equipment',
      title_text: 'navigation.menu.group.equipment',
      slots: ['SCR-07:*', 'SCR-04:admissions'],
    },
    {
      id: 'rules',
      title_text: 'navigation.menu.group.rules',
      slots: ['SCR-04:catalog', 'SCR-04:vocabulary', 'SCR-05:policy'],
    },
    {
      id: 'record',
      title_text: 'navigation.menu.group.record',
      slots: ['SCR-03:integrity', 'SCR-16:*'],
    },
  ],
};

/** Entradas que la cabecera ya ofrece (cuenta y campana): no se repiten en el menú agrupado. */
const IN_HEADER: ReadonlySet<string> = new Set(['SCR-01:account', 'SCR-15:list']);

const slotOf = (item: NavItem) => `${item.screen_id}:${item.group}`;

function fills(slot: string, item: NavItem): boolean {
  return slot === slotOf(item) || slot === `${item.screen_id}:*`;
}

/** `true` si `item` es el inicio de la sesión. */
function isHomeItem(item: NavItem, home: HomeScreen | null): boolean {
  return home !== null && home.screen_id === item.screen_id && home.group === item.group;
}

/** Nombre de la entrada en el menú: «Inicio» para el inicio de la sesión. */
export function itemLabel(item: NavItem, home: HomeScreen | null): string {
  return isHomeItem(item, home) ? 'navigation.menu.home' : item.title_text;
}

export interface MenuSection {
  readonly id: string;
  /** Encabezado del grupo; `null` en el menú corto, sin grupos. */
  readonly title_text: string | null;
  readonly items: readonly NavItem[];
}

/**
 * Las entradas agrupadas para el rol del inicio, con el inicio primero. Ninguna entrada visible se
 * pierde: la que no está en un grupo del perfil va en «Más», salvo las de la cabecera.
 */
export function menuSections(items: readonly NavItem[], home: HomeScreen | null): MenuSection[] {
  const first = items.find((item) => isHomeItem(item, home));
  const ordered = first === undefined ? items : [first, ...items.filter((item) => item !== first)];
  const profile = home === null ? undefined : MENU_PROFILES[home.role];
  if (profile === undefined) return [{ id: 'main', title_text: null, items: ordered }];
  const used = new Set<NavItem>();
  const sections: MenuSection[] = profile.map((spec, index) => {
    const own = spec.slots.flatMap((slot) =>
      ordered.filter((item) => !used.has(item) && fills(slot, item)),
    );
    const grouped =
      index === 0 && first !== undefined && !own.includes(first) ? [first, ...own] : own;
    for (const item of grouped) used.add(item);
    return { id: spec.id, title_text: spec.title_text, items: grouped };
  });
  const rest = ordered.filter((item) => !used.has(item) && !IN_HEADER.has(slotOf(item)));
  if (rest.length > 0) {
    sections.push({ id: 'more', title_text: 'navigation.menu.group.more', items: rest });
  }
  return sections.filter((section) => section.items.length > 0);
}

/** Pestañas de la barra inferior de la tableta: cuatro destinos como máximo. */
const MAX_TABS = 4;

/**
 * Pestañas de la tableta: el inicio, los primeros destinos del menú y las notificaciones, sin las
 * entradas que piden zona o planta (esas se eligen en el menú). `more` si el menú tiene algo más.
 */
export function tabItems(
  items: readonly NavItem[],
  home: HomeScreen | null,
): { readonly tabs: readonly NavItem[]; readonly more: boolean } {
  const notifications = items.filter((item) => slotOf(item) === 'SCR-15:list');
  const menu = menuSections(items, home).flatMap((section) => section.items);
  const direct = menu.filter((item) => item.select === undefined);
  const candidates = [...direct, ...notifications];
  const tabs =
    candidates.length <= MAX_TABS
      ? candidates
      : [...direct.slice(0, MAX_TABS - notifications.length), ...notifications].slice(0, MAX_TABS);
  return { tabs, more: menu.some((item) => !tabs.includes(item)) };
}

/** Las zonas o plantas del alcance que el selector ofrece, agrupadas por planta. */
function useScopeChoices(open: boolean) {
  const { principal } = useUiSession();
  const scope = principal?.organization.organization_id ?? 'none';
  const concession = principal?.concession_id ?? null;
  return useScreenQuery({
    key: screenKey({ screen: 'shell', operation: 'GET /hierarchy', scope, concession }),
    policy: policyFor('GET /hierarchy'),
    enabled: open,
    read: ({ signal }) => sessionPort.hierarchy.read({}, signal),
  });
}

/** Entrada que necesita zona o planta: abre el selector y «Abrir» lleva a la elegida. */
function SelectEntry({
  item,
}: {
  readonly item: NavItem & { readonly select: 'zone_id' | 'plant_id' };
}) {
  const id = useId();
  const navigate = useNavigate();
  const [open, setOpen] = useState(false);
  const [choice, setChoice] = useState('');
  const hierarchy = useScopeChoices(open);
  const plants = hierarchy.data?.plants ?? [];
  const label = text(item.select === 'zone_id' ? 'role_navigation.zone' : 'role_navigation.plant');
  return (
    <>
      <button
        type="button"
        className={styles.navLink}
        aria-expanded={open}
        aria-controls={`${id}-select`}
        data-screen={item.screen_id}
        data-group={item.group}
        onClick={() => {
          setOpen((value) => !value);
        }}
      >
        {text(item.title_text)}
      </button>
      <form
        noValidate
        id={`${id}-select`}
        className={styles.navSelect}
        aria-label={text(item.title_text)}
        hidden={!open}
        onSubmit={(event) => {
          event.preventDefault();
          if (choice === '') return;
          setOpen(false);
          void navigate(item.path.replace(`:${item.select}`, encodeURIComponent(choice)));
        }}
      >
        <label htmlFor={`${id}-choice`}>{label}</label>
        <select
          id={`${id}-choice`}
          value={choice}
          onChange={(event) => {
            setChoice(event.currentTarget.value);
          }}
        >
          <option value="">
            {text(
              hierarchy.status === 'pending' ? 'screen_state.loading' : 'role_navigation.choose',
            )}
          </option>
          {item.select === 'plant_id'
            ? plants.map((plant) => (
                <option key={plant.plant_id} value={plant.plant_id}>
                  {plant.name}
                </option>
              ))
            : plants.map((plant) => (
                <optgroup key={plant.plant_id} label={plant.name}>
                  {plant.zones.map((zone) => (
                    <option key={zone.zone_id} value={zone.zone_id}>
                      {zone.name}
                    </option>
                  ))}
                </optgroup>
              ))}
        </select>
        {hierarchy.error === null ? null : (
          <p role="alert">{text('role_navigation.unavailable')}</p>
        )}
        <button type="submit" className={styles.button} disabled={choice === ''}>
          {text('role_navigation.open')}
        </button>
      </form>
    </>
  );
}

function Entry({ item, home }: { readonly item: NavItem; readonly home: HomeScreen | null }) {
  if (item.select !== undefined) return <SelectEntry item={{ ...item, select: item.select }} />;
  return (
    <NavLink
      className={() => styles.navLink}
      to={item.path}
      data-screen={item.screen_id}
      data-group={item.group}
    >
      {text(itemLabel(item, home))}
    </NavLink>
  );
}

/** Barra inferior de la tableta: pestañas del rol y «Menú», que despliega el menú completo. */
function TabBar({
  items,
  home,
  menuId,
  open,
  onToggle,
}: {
  readonly items: readonly NavItem[];
  readonly home: HomeScreen | null;
  readonly menuId: string;
  readonly open: boolean;
  readonly onToggle: () => void;
}) {
  const { tabs, more } = tabItems(items, home);
  return (
    <nav className={styles.tabbar} aria-label={text('navigation.menu.tabs')}>
      <ul className={styles.tabList}>
        {tabs.map((item) => (
          <li key={slotOf(item)} className={styles.tabItem}>
            <NavLink
              className={() => styles.tab}
              to={item.path}
              data-screen={item.screen_id}
              data-group={item.group}
            >
              {text(itemLabel(item, home))}
            </NavLink>
          </li>
        ))}
        {more ? (
          <li className={styles.tabItem}>
            <button
              type="button"
              className={styles.tab}
              aria-expanded={open}
              aria-controls={menuId}
              onClick={onToggle}
            >
              {text(open ? 'navigation.menu.close' : 'navigation.menu.open')}
            </button>
          </li>
        ) : null}
      </ul>
    </nav>
  );
}

export function NavMenu({
  navigation,
  screens,
}: {
  readonly navigation: NavigationMap;
  readonly screens: readonly ScreenDescriptor[];
}): ReactNode {
  const items = navItems(navigation, screens);
  const { home } = navigation;
  const sections = menuSections(items, home);
  const tablet = useTabletLayout();
  const id = useId();
  const { pathname } = useLocation();
  // El menú desplegado en la tableta vale para la ruta en que se abrió: navegar lo pliega.
  const [openAt, setOpenAt] = useState<string | null>(null);
  const open = tablet && openAt === pathname;
  useEffect(() => {
    if (!open) return undefined;
    const onKey = (event: KeyboardEvent) => {
      if (event.key === 'Escape') setOpenAt(null);
    };
    document.addEventListener('keydown', onKey);
    return () => {
      document.removeEventListener('keydown', onKey);
    };
  }, [open]);
  const menu = (
    <nav
      id={`${id}-menu`}
      className={tablet ? styles.navPanel : styles.nav}
      aria-label={text('app_shell.nav.label')}
      hidden={tablet && !open}
    >
      {sections.map((section) => (
        <div key={section.id} className={styles.navSection} data-section={section.id}>
          {section.title_text === null ? null : (
            <p className={styles.navHeading} id={`${id}-${section.id}`}>
              {text(section.title_text)}
            </p>
          )}
          <ul
            className={styles.navList}
            aria-labelledby={section.title_text === null ? undefined : `${id}-${section.id}`}
          >
            {section.items.map((item) => (
              <li key={slotOf(item)}>
                <Entry item={item} home={home} />
              </li>
            ))}
          </ul>
        </div>
      ))}
    </nav>
  );
  if (!tablet) return menu;
  return (
    <>
      {menu}
      <TabBar
        items={items}
        home={home}
        menuId={`${id}-menu`}
        open={open}
        onToggle={() => {
          setOpenAt(open ? null : pathname);
        }}
      />
    </>
  );
}
