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
 */
import { useId, useState, type ReactNode } from 'react';
import { NavLink, useNavigate } from 'react-router';

import type { GroupMenu, RouteGroup, ScreenDescriptor } from '@/app/descriptor';
import type { NavigationMap } from '@/app/navigation';
import { sessionPort } from '@/ports/shared';
import { policyFor, screenKey, useScreenQuery } from '@/state/query';
import { useUiSession } from '@/state/session';
import { text } from '@/texts';

import styles from './AppShell.module.css';

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

export function NavMenu({
  navigation,
  screens,
}: {
  readonly navigation: NavigationMap;
  readonly screens: readonly ScreenDescriptor[];
}): ReactNode {
  const items = navItems(navigation, screens);
  return (
    <nav className={styles.nav} aria-label={text('app_shell.nav.label')}>
      <ul className={styles.navList}>
        {items.map((item) => (
          <li key={`${item.screen_id}:${item.group}`}>
            {item.select === undefined ? (
              <NavLink
                className={() => styles.navLink}
                to={item.path}
                data-screen={item.screen_id}
                data-group={item.group}
              >
                {text(item.title_text)}
              </NavLink>
            ) : (
              <SelectEntry item={{ ...item, select: item.select }} />
            )}
          </li>
        ))}
      </ul>
    </nav>
  );
}
