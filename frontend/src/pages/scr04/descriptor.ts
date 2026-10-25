/**
 * SCR-04 · Catálogo de estándares, admisión y configuración (U-03; vocabulario U-04). Grupos:
 * `catalog` y `standards` (VIG-308), `parameters`, `admissions` y `vocabulary` (VIG-309).
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-04',
  title_text: 'navigation.scr04',
  route: '/zones/:zone_id/catalog',
  required_permissions: ['catalog.read', 'catalog.manage', 'vocabulary.manage'],
  route_groups: [
    {
      id: 'catalog',
      paths: ['/zones/:zone_id/catalog', '/plants/:plant_id/catalog'],
      access: 'permission',
      required_permissions: ['catalog.read', 'catalog.manage'],
      load: () => import('./catalog'),
    },
    {
      id: 'standards',
      paths: ['/zones/:zone_id/standards'],
      access: 'permission',
      required_permissions: ['catalog.manage'],
      load: () => import('./standards'),
    },
    {
      id: 'parameters',
      paths: ['/zones/:zone_id/catalog/parameters', '/zones/:zone_id/parameters'],
      access: 'permission',
      required_permissions: ['catalog.manage'],
      load: () => import('./parameters'),
    },
    {
      id: 'admissions',
      paths: ['/plants/:plant_id/admissions'],
      access: 'permission',
      required_permissions: ['catalog.read', 'catalog.manage'],
      load: () => import('./admissions'),
    },
    {
      id: 'vocabulary',
      paths: ['/admin/vocabulary'],
      access: 'permission',
      required_permissions: ['vocabulary.manage'],
      load: () => import('./vocabulary'),
    },
  ],
  sections: [
    { id: 'catalog', group: 'catalog', required_permissions: ['catalog.read'] },
    { id: 'standards_manage', group: 'standards', required_permissions: ['catalog.manage'] },
    { id: 'parameters', group: 'parameters', required_permissions: ['catalog.manage'] },
    { id: 'admissions', group: 'admissions', required_permissions: ['catalog.read'] },
    { id: 'admission_test', group: 'admissions', required_permissions: ['catalog.manage'] },
    { id: 'vocabulary', group: 'vocabulary', required_permissions: ['vocabulary.manage'] },
  ],
  expected_roles: ['administrator'],
  backend_unit: 'U-03',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
