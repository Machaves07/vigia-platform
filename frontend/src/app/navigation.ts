/**
 * Construcción de la navegación (IF-05, LC-APP-03): funciones puras sobre `SCREENS`.
 *
 * - Una pantalla, un grupo de rutas o una sección es visible si y solo si alguna de sus claves
 *   está en `effective_permissions` (BR-APP-01). Nunca se miran los roles ni las asignaciones:
 *   la visibilidad es comodidad y la barrera es el servidor (NFR-APP-23, PAT-APP-SEG-06).
 * - El orden es el de `SCREENS` (fijo).
 * - El inicio (BR-APP-04) es el único punto que mira los roles, porque la regla elige «el panel
 *   del rol más operativo»: coordinador SST, administrador, proveedor o instalador, gerente de
 *   planta, mando de línea, COPASST. Solo compone las secciones con permiso.
 */
import type { Role, RouteGroup, ScreenDescriptor, ScreenId } from '@/app/descriptor';
import { SCREENS } from '@/app/screens';

/** Lo que la navegación necesita de `Principal` (`GET /me`). */
export interface NavigationPrincipal {
  readonly effective_permissions: readonly string[];
  readonly assignments: readonly { readonly role: string }[];
}

/** Orden de BR-APP-04 `[objetivo propio]`. `platform_operator` no tiene panel de inicio. */
export const HOME_ROLE_ORDER: readonly Role[] = Object.freeze([
  'coordinator_sst',
  'administrator',
  'provider_installer',
  'plant_manager',
  'line_manager',
  'copasst',
]);

interface GroupEntry {
  readonly id: string;
  readonly paths: readonly string[];
  readonly visible: boolean;
}

interface SectionEntry {
  readonly id: string;
  readonly group: string;
  readonly visible: boolean;
}

interface NavigationEntry {
  readonly screen_id: ScreenId;
  readonly title_text: string;
  readonly route: string;
  readonly visible: boolean;
  readonly groups: readonly GroupEntry[];
  readonly sections: readonly SectionEntry[];
}

export interface HomeScreen {
  readonly role: Role;
  readonly screen_id: ScreenId;
  readonly group: string;
  /** Entrada del grupo de inicio, siempre sin parámetros. */
  readonly path: string;
  /** Secciones del grupo de inicio cuyo permiso está en el conjunto, en su orden. */
  readonly sections: readonly string[];
}

export interface NavigationMap {
  readonly screens: readonly NavigationEntry[];
  readonly home: HomeScreen | null;
}

/** `true` si alguna de `required` está en `permissions` (BR-APP-01). */
function anyGranted(required: readonly string[], permissions: ReadonlySet<string>): boolean {
  return required.some((key) => permissions.has(key));
}

/**
 * `true` si un grupo de rutas se puede abrir: públicos siempre, `authenticated` con cualquier
 * sesión y el resto con alguna de sus claves. `permissions === null` es «sin sesión».
 */
export function canEnterGroup(group: RouteGroup, permissions: ReadonlySet<string> | null): boolean {
  switch (group.access) {
    case 'public':
      return true;
    case 'authenticated':
      return permissions !== null;
    case 'permission':
      return permissions !== null && anyGranted(group.required_permissions, permissions);
  }
}

function entryFor(screen: ScreenDescriptor, permissions: ReadonlySet<string>): NavigationEntry {
  return {
    screen_id: screen.screen_id,
    title_text: screen.title_text,
    route: screen.route,
    visible: anyGranted(screen.required_permissions, permissions),
    groups: screen.route_groups.map((group) => ({
      id: group.id,
      paths: group.paths,
      visible: canEnterGroup(group, permissions),
    })),
    sections: screen.sections.map((section) => ({
      id: section.id,
      group: section.group,
      visible: anyGranted(section.required_permissions, permissions),
    })),
  };
}

/**
 * Panel de inicio (BR-APP-04): el del primer rol de `HOME_ROLE_ORDER` presente en las asignaciones
 * cuyo grupo de inicio admiten los permisos; `null` si ninguno.
 */
export function homeScreen(
  assignments: NavigationPrincipal['assignments'],
  permissions: ReadonlySet<string>,
  screens: readonly ScreenDescriptor[] = SCREENS,
): HomeScreen | null {
  const roles = new Set(assignments.map((assignment) => assignment.role));
  for (const role of HOME_ROLE_ORDER) {
    if (!roles.has(role)) continue;
    for (const screen of screens) {
      if (!screen.home_for_roles.includes(role)) continue;
      const group = screen.route_groups.find((candidate) => candidate.id === screen.home_group);
      if (group === undefined || !canEnterGroup(group, permissions)) continue;
      return {
        role,
        screen_id: screen.screen_id,
        group: group.id,
        path: group.paths[0],
        sections: screen.sections
          .filter((section) => section.group === group.id)
          .filter((section) => anyGranted(section.required_permissions, permissions))
          .map((section) => section.id),
      };
    }
  }
  return null;
}

/** `NavigationMap` de IF-05: las dieciséis pantallas en orden fijo y el panel de inicio. */
export function buildNavigationMap(
  principal: NavigationPrincipal,
  screens: readonly ScreenDescriptor[] = SCREENS,
): NavigationMap {
  const permissions = new Set(principal.effective_permissions);
  return {
    screens: screens.map((screen) => entryFor(screen, permissions)),
    home: homeScreen(principal.assignments, permissions, screens),
  };
}
