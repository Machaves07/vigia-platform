/**
 * PR-APP-01 (BR-APP-01, 03; NFR-APP-23): para todo conjunto de permisos efectivos y todo
 * `ScreenDescriptor`, la pantalla es visible si y solo si sus claves y los permisos se cortan.
 *
 * Oráculo: la matriz exportada por el backend (`u05-fixtures.json`) aplicada a asignaciones
 * generadas (`principal()` de los generadores de dominio). Cada asignación da su conjunto de
 * pantallas visibles por separado y el oráculo es su unión; la interfaz solo recibe
 * `effective_permissions` y debe dar el mismo conjunto. Además, la visibilidad no cambia al cambiar
 * los roles con los mismos permisos, y ninguna sección se ve sin su clave.
 */
import fc from 'fast-check';
import { describe, expect, it } from 'vitest';

import { buildNavigationMap, type NavigationMap } from '@/app/navigation';
import { SCREENS } from '@/app/screens';

import { effectivePermissions, PERMISSION_KEYS, principal, ROLES } from '../generators';

const meets = (required: readonly string[], granted: readonly string[]): boolean =>
  required.some((key) => granted.includes(key));

const visibleScreens = (map: NavigationMap): string[] =>
  map.screens.filter((entry) => entry.visible).map((entry) => entry.screen_id);

const anyPermissions = fc.subarray([...PERMISSION_KEYS]);
const anyRoles = fc.array(
  fc.constantFrom(...ROLES).map((role) => ({ role })),
  { maxLength: 6 },
);

describe('PR-APP-01 · visibilidad derivada solo de effective_permissions', () => {
  it('la matriz aplicada a cada asignación generada da, unida, las mismas pantallas', () => {
    fc.assert(
      fc.property(principal(), (subject) => {
        const oracle = new Set(
          subject.assignments.flatMap((assignment) => {
            const granted = effectivePermissions(subject.context, [assignment.role]);
            return SCREENS.filter((screen) => meets(screen.required_permissions, granted)).map(
              (screen) => screen.screen_id,
            );
          }),
        );
        const map = buildNavigationMap({
          effective_permissions: subject.effectivePermissions,
          assignments: subject.assignments,
        });
        expect(visibleScreens(map)).toEqual(
          SCREENS.map((screen) => screen.screen_id).filter((id) => oracle.has(id)),
        );
      }),
    );
  });

  it('con los mismos permisos, otros roles no cambian ninguna pantalla, grupo ni sección', () => {
    fc.assert(
      fc.property(anyPermissions, anyRoles, anyRoles, (granted, first, second) => {
        const a = buildNavigationMap({ effective_permissions: granted, assignments: first });
        const b = buildNavigationMap({ effective_permissions: granted, assignments: second });
        expect(b.screens).toEqual(a.screens);
      }),
    );
  });

  it('pantallas, grupos y secciones: visibles si y solo si alguna de sus claves está', () => {
    fc.assert(
      fc.property(anyPermissions, anyRoles, (granted, assignments) => {
        const map = buildNavigationMap({ effective_permissions: granted, assignments });
        expect(map.screens.map((entry) => entry.screen_id)).toEqual(
          SCREENS.map((screen) => screen.screen_id),
        );
        SCREENS.forEach((screen, index) => {
          const entry = map.screens[index];
          expect(entry?.visible).toBe(meets(screen.required_permissions, granted));
          expect(entry?.sections).toEqual(
            screen.sections.map((section) => ({
              id: section.id,
              group: section.group,
              visible: meets(section.required_permissions, granted),
            })),
          );
          expect(entry?.groups.map((group) => group.visible)).toEqual(
            screen.route_groups.map(
              (group) =>
                group.access !== 'permission' || meets(group.required_permissions, granted),
            ),
          );
        });
      }),
    );
  });

  it('sin permisos no hay ninguna pantalla ni sección visible, aunque haya roles', () => {
    fc.assert(
      fc.property(anyRoles, (assignments) => {
        const map = buildNavigationMap({ effective_permissions: [], assignments });
        expect(visibleScreens(map)).toEqual([]);
        expect(map.screens.flatMap((entry) => entry.sections).filter((s) => s.visible)).toEqual([]);
      }),
    );
  });

  it('una clave basta: cada clave de cada pantalla y de cada sección la hace visible sola', () => {
    for (const screen of SCREENS) {
      for (const key of screen.required_permissions) {
        const map = buildNavigationMap({ effective_permissions: [key], assignments: [] });
        expect(visibleScreens(map), `${screen.screen_id} con ${key}`).toContain(screen.screen_id);
      }
      for (const section of screen.sections) {
        for (const key of section.required_permissions) {
          const map = buildNavigationMap({ effective_permissions: [key], assignments: [] });
          const entry = map.screens.find((candidate) => candidate.screen_id === screen.screen_id);
          const shown = entry?.sections.find((candidate) => candidate.id === section.id);
          expect(shown?.visible, `${screen.screen_id}.${section.id} con ${key}`).toBe(true);
        }
      }
    }
  });
});
