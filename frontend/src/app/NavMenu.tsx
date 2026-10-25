/**
 * UI-07 `NavMenu` (BR-APP-01 y 03, IF-05): una entrada por pantalla visible de `NavigationMap`,
 * en el orden fijo de `SCREENS`. Una pantalla sin permiso no aparece; alcanzada por URL, la guarda
 * del enrutador pinta «no disponible en su alcance».
 *
 * Cada entrada lleva a la primera ruta de la pantalla que la persona puede abrir: con sesión (no
 * pública), sin parámetros y con su grupo visible. Una pantalla visible sin esa ruta (solo con
 * rutas de detalle) no tiene entrada: no hay adónde llevar.
 */
import type { ReactNode } from 'react';
import { NavLink } from 'react-router';

import type { ScreenDescriptor } from '@/app/descriptor';
import type { NavigationMap } from '@/app/navigation';
import { text } from '@/texts';

import styles from './AppShell.module.css';

interface NavItem {
  readonly screen_id: string;
  readonly title_text: string;
  readonly path: string;
}

/** Las entradas del menú: pantallas visibles con una ruta de entrada, en el orden del mapa. */
export function navItems(
  navigation: NavigationMap,
  screens: readonly ScreenDescriptor[],
): NavItem[] {
  return navigation.screens.flatMap((entry) => {
    if (!entry.visible) return [];
    const descriptor = screens.find((screen) => screen.screen_id === entry.screen_id);
    for (const group of entry.groups) {
      if (!group.visible) continue;
      const access = descriptor?.route_groups.find(
        (candidate) => candidate.id === group.id,
      )?.access;
      if (access === undefined || access === 'public') continue;
      const path = group.paths.find((candidate) => !candidate.includes(':'));
      if (path !== undefined) {
        return [{ screen_id: entry.screen_id, title_text: entry.title_text, path }];
      }
    }
    return [];
  });
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
          <li key={item.screen_id}>
            <NavLink className={() => styles.navLink} to={item.path} data-screen={item.screen_id}>
              {text(item.title_text)}
            </NavLink>
          </li>
        ))}
      </ul>
    </nav>
  );
}
