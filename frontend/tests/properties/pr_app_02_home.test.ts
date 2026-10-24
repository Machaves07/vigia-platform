/**
 * PR-APP-02 (BR-APP-04): el panel de inicio elegido para cualquier conjunto de asignaciones es el
 * del rol más operativo presente en el orden fijo (coordinador SST, administrador, proveedor o
 * instalador, gerente de planta, mando de línea, COPASST), y solo compone secciones cuyo permiso
 * está en el conjunto.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { HOME_ROLE_ORDER, homeScreen } from '@/app/navigation';
import { SCREENS } from '@/app/screens';

import { effectivePermissions, PERMISSION_KEYS, principal, ROLES } from '../generators';

const ORDER = [
  'coordinator_sst',
  'administrator',
  'provider_installer',
  'plant_manager',
  'line_manager',
  'copasst',
];

const meets = (required: readonly string[], granted: readonly string[]): boolean =>
  required.some((key) => granted.includes(key));

function homeOf(role: string) {
  const screen = SCREENS.find((candidate) => candidate.home_for_roles.some((r) => r === role));
  const group = screen?.route_groups.find((candidate) => candidate.id === screen.home_group);
  if (screen === undefined || group === undefined) throw new Error(`${role} sin inicio`);
  return { screen, group };
}

function expectedHome(role: string, granted: readonly string[]) {
  const { screen, group } = homeOf(role);
  return {
    role,
    screen_id: screen.screen_id,
    group: group.id,
    path: group.paths[0],
    sections: screen.sections
      .filter((section) => section.group === group.id)
      .filter((section) => meets(section.required_permissions, granted))
      .map((section) => section.id),
  };
}

const anyRoles = fc.array(
  fc.constantFrom(...ROLES).map((role) => ({ role })),
  { minLength: 1, maxLength: 6 },
);

describe('PR-APP-02 · panel de inicio del rol más operativo', () => {
  it('el orden es el de BR-APP-04 y cada rol del orden tiene un único inicio sin parámetros', () => {
    expect(HOME_ROLE_ORDER).toEqual(ORDER);
    for (const role of ORDER) {
      const homes = SCREENS.filter((screen) => screen.home_for_roles.some((r) => r === role));
      expect(homes, role).toHaveLength(1);
      expect(homeOf(role).group.paths[0], role).not.toContain(':');
    }
  });

  it('con la matriz, es el del primer rol presente en el orden', () => {
    fc.assert(
      fc.property(principal(), (subject) => {
        // `GET /me` solo trae asignaciones que valen en la organización del usuario (BR-NUC-05):
        // un rol de cliente en una organización proveedora no concede nada y no llega a la
        // interfaz. `principal()` las genera; aquí se quitan para tener asignaciones posibles.
        const assignments = subject.assignments.filter(
          (assignment) => effectivePermissions(subject.context, [assignment.role]).length > 0,
        );
        const granted = subject.effectivePermissions;
        const home = homeScreen(assignments, new Set(granted));
        const role = ORDER.find((candidate) => assignments.some((a) => a.role === candidate));
        expect(home).toEqual(role === undefined ? null : expectedHome(role, granted));
      }),
    );
  });

  it('con permisos cualesquiera, solo compone secciones con su clave y salta inicios cerrados', () => {
    fc.assert(
      fc.property(fc.subarray([...PERMISSION_KEYS]), anyRoles, (granted, assignments) => {
        const home = homeScreen(assignments, new Set(granted));
        const open = ORDER.filter((role) => assignments.some((a) => a.role === role)).find((role) =>
          meets(homeOf(role).group.required_permissions, granted),
        );
        expect(home).toEqual(open === undefined ? null : expectedHome(open, granted));
        for (const id of home?.sections ?? []) {
          const section = homeOf(home?.role ?? '').screen.sections.find((s) => s.id === id);
          expect(meets(section?.required_permissions ?? [], granted), id).toBe(true);
        }
      }),
    );
  });

  it('el operador de la plataforma solo no tiene panel de inicio', () => {
    const granted = effectivePermissions('provider_organization', ['platform_operator']);
    expect(homeScreen([{ role: 'platform_operator' }], new Set(granted))).toBeNull();
  });
});
