/**
 * Textos por ruta (VIG-364): la ruta de una pantalla no se pinta sin sus textos. El `lazy` de cada
 * grupo espera, junto con su porción de código, `loadScreenTexts` con la pantalla y las que cita
 * (`texts_from`); la precarga del inicio también los trae. Un fallo de los textos es un fallo de la
 * ruta, como el de su código.
 */
import type { RouteObject } from 'react-router';
import { afterEach, describe, expect, it, vi } from 'vitest';

import type { ScreenDescriptor } from '@/app/descriptor';
import { createAppRoutes, preloadHomeCode } from '@/app/router';
import * as catalog from '@/texts';

afterEach(() => {
  vi.restoreAllMocks();
});

function Page() {
  return null;
}

const SCREEN: ScreenDescriptor = {
  screen_id: 'SCR-09',
  title_text: 'navigation.scr09',
  route: '/synthetic',
  required_permissions: ['findings.read'],
  route_groups: [
    {
      id: 'main',
      paths: ['/synthetic'],
      access: 'permission',
      required_permissions: ['findings.read'],
      load: () => Promise.resolve({ default: Page }),
    },
  ],
  sections: [],
  expected_roles: ['coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: ['coordinator_sst'],
  home_group: 'main',
  device_classes: ['desktop'],
  texts_from: ['SCR-08'],
};

const PRINCIPAL = {
  effective_permissions: ['findings.read'],
  assignments: [{ role: 'coordinator_sst' }],
};

function lazyOf(path: string) {
  const [root] = createAppRoutes(PRINCIPAL, { screens: [SCREEN] });
  const route = root?.children?.find((child: RouteObject) => child.path === path);
  const lazy = route?.lazy;
  if (typeof lazy !== 'function') throw new Error(`La ruta ${path} no es diferida.`);
  return lazy;
}

describe('textos de cada pantalla con su ruta', () => {
  it('el lazy de la ruta carga los textos de la pantalla y de las que cita', async () => {
    const load = vi.spyOn(catalog, 'loadScreenTexts').mockResolvedValue(undefined);
    const result = await lazyOf('/synthetic')();
    expect(load).toHaveBeenCalledWith(['SCR-09', 'SCR-08']);
    expect(result).toHaveProperty('Component', Page);
  });

  it('si los textos no llegan, la ruta falla como si faltara su código', async () => {
    vi.spyOn(catalog, 'loadScreenTexts').mockRejectedValue(new Error('porción ausente'));
    await expect(lazyOf('/synthetic')()).rejects.toThrow('porción ausente');
  });

  it('la precarga del inicio trae también sus textos', async () => {
    const load = vi.spyOn(catalog, 'loadScreenTexts').mockResolvedValue(undefined);
    await preloadHomeCode(PRINCIPAL, [SCREEN]);
    expect(load).toHaveBeenCalledWith(['SCR-09', 'SCR-08']);
  });
});
