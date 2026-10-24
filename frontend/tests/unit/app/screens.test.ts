/**
 * Tabla de pantallas (LC-APP-03, `frontend-components.md` §3 y §6, BR-APP-02): forma de los
 * dieciséis descriptores, claves reales de la matriz exportada y claves de la conciliación con U-04.
 */
import { describe, expect, it } from 'vitest';

import { SCREENS } from '@/app/screens';
import { texts } from '@/texts';

import { PERMISSION_KEYS, ROLES } from '../../generators';

const ids = Array.from({ length: 16 }, (_, i) => `SCR-${String(i + 1).padStart(2, '0')}`);
const byId = (id: string) => {
  const screen = SCREENS.find((candidate) => candidate.screen_id === id);
  if (screen === undefined) throw new Error(id);
  return screen;
};
const allKeys = (id: string) => {
  const screen = byId(id);
  return new Set([
    ...screen.required_permissions,
    ...screen.route_groups.flatMap((group) => group.required_permissions),
    ...screen.sections.flatMap((section) => section.required_permissions),
  ]);
};

describe('los dieciséis descriptores', () => {
  it('están en orden fijo, una vez cada uno', () => {
    expect(SCREENS.map((screen) => screen.screen_id)).toEqual(ids);
  });

  it('toda clave está en la matriz exportada y ninguna es de la plataforma', () => {
    for (const screen of SCREENS) {
      for (const key of allKeys(screen.screen_id)) {
        expect(PERMISSION_KEYS, `${screen.screen_id}: ${key}`).toContain(key);
        expect(key.startsWith('platform.'), key).toBe(false);
      }
      for (const role of [...screen.expected_roles, ...screen.home_for_roles]) {
        expect(ROLES).toContain(role);
      }
    }
  });

  it('las claves de grupos y secciones están entre las de su pantalla', () => {
    for (const screen of SCREENS) {
      const own = new Set(screen.required_permissions);
      for (const group of screen.route_groups) {
        expect(group.access === 'permission', `${screen.screen_id}.${group.id}`).toBe(
          group.required_permissions.length > 0,
        );
        for (const key of group.required_permissions) expect(own.has(key), key).toBe(true);
      }
      const groups = new Set(screen.route_groups.map((group) => group.id));
      for (const section of screen.sections) {
        expect(groups.has(section.group), `${screen.screen_id}.${section.id}`).toBe(true);
        for (const key of section.required_permissions) expect(own.has(key), key).toBe(true);
      }
    }
  });

  it('cada ruta es de un solo grupo; los grupos tienen carpeta y el título está en el catálogo', () => {
    const paths = SCREENS.flatMap((screen) => screen.route_groups.flatMap((group) => group.paths));
    expect(new Set(paths).size).toBe(paths.length);
    for (const screen of SCREENS) {
      expect(texts[screen.title_key], screen.title_key).toBeDefined();
      expect(paths).toContain(screen.route);
      for (const group of screen.route_groups) {
        expect(group.id).toMatch(/^[a-z_]+(\/[a-z_]+)?$/);
        for (const path of group.paths) expect(path).toMatch(/^\/[a-z0-9\-/:_]*$/);
      }
    }
  });

  it('el inicio es un grupo sin parámetros y solo existe si hay roles', () => {
    for (const screen of SCREENS) {
      expect(screen.home_group === null, screen.screen_id).toBe(screen.home_for_roles.length === 0);
      if (screen.home_group === null) continue;
      const group = screen.route_groups.find((candidate) => candidate.id === screen.home_group);
      expect(group?.paths[0], screen.screen_id).not.toContain(':');
      expect(group?.access).toBe('permission');
    }
  });

  it('tableta solo en SCR-06 y SCR-16; escritorio en todas', () => {
    for (const screen of SCREENS) {
      expect(screen.device_classes).toContain('desktop');
      expect(screen.device_classes.includes('tablet'), screen.screen_id).toBe(
        ['SCR-06', 'SCR-16'].includes(screen.screen_id),
      );
    }
  });

  it('el hallazgo va por finding_state_id en SCR-08, 09 y 11 (nota de §6)', () => {
    const findingPaths = ['SCR-08', 'SCR-09', 'SCR-11'].flatMap((id) =>
      byId(id).route_groups.flatMap((group) =>
        group.paths.filter((p) => p.includes('/findings/:')),
      ),
    );
    expect(findingPaths.length).toBeGreaterThan(0);
    for (const path of findingPaths) expect(path).toMatch(/^\/findings\/:finding_state_id\b/);
    const all = SCREENS.flatMap((s) => s.route_groups.flatMap((g) => g.paths)).join(' ');
    expect(all).not.toContain(':record_id/');
    expect(all).not.toMatch(/\/findings\/:record_id/);
  });
});

describe('claves de la conciliación con U-04 (§6 y diferencia 1 del PR #197)', () => {
  it('loop.manage no existe y audit.read no tiene pantalla', () => {
    for (const screen of SCREENS) {
      expect(allKeys(screen.screen_id).has('loop.manage')).toBe(false);
      expect(allKeys(screen.screen_id).has('audit.read')).toBe(false);
    }
  });

  it('SCR-10: visible con findings.read o review_queue.resolve; resolver solo con la segunda', () => {
    const scr10 = byId('SCR-10');
    expect([...scr10.required_permissions].sort()).toEqual([
      'findings.read',
      'review_queue.resolve',
    ]);
    expect(scr10.sections.find((s) => s.id === 'resolve')?.required_permissions).toEqual([
      'review_queue.resolve',
    ]);
  });

  it('SCR-09, SCR-12 y SCR-13 con las claves de §6', () => {
    expect([...byId('SCR-09').required_permissions].sort()).toEqual([
      'actions.manage',
      'closure_attachments.read',
      'findings.classify',
      'findings.close_and_sign',
      'findings.read',
    ]);
    expect([...byId('SCR-12').required_permissions].sort()).toEqual([
      'commitments.write',
      'findings.classify',
      'review_package.read',
    ]);
    expect([...byId('SCR-13').required_permissions].sort()).toEqual([
      'executive_summary.read',
      'exposure.read',
      'metrics.read',
    ]);
    const sectionKeys = (id: string) =>
      new Set(byId(id).sections.flatMap((section) => section.required_permissions));
    expect(sectionKeys('SCR-09')).toEqual(new Set(byId('SCR-09').required_permissions));
    expect(sectionKeys('SCR-12')).toEqual(new Set(byId('SCR-12').required_permissions));
    expect(sectionKeys('SCR-13')).toEqual(new Set(byId('SCR-13').required_permissions));
  });
});
