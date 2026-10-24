/**
 * SCR-05 · Gestor de actas, acuerdos y política de incerrables (U-03), VIG-323. Grupos: `gates`
 * (por planta y por zona) y `policy`.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-05',
  title_text: 'navigation.scr05',
  route: '/plants/:plant_id/gates',
  required_permissions: ['catalog.read', 'commissioning.run', 'agreements.sign'],
  route_groups: [
    {
      id: 'gates',
      paths: ['/plants/:plant_id/gates', '/zones/:zone_id/gates'],
      access: 'permission',
      required_permissions: ['catalog.read', 'commissioning.run', 'agreements.sign'],
      load: () => import('./gates'),
    },
    {
      id: 'policy',
      paths: ['/plants/:plant_id/policy'],
      access: 'permission',
      required_permissions: ['catalog.read', 'agreements.sign'],
      load: () => import('./policy'),
    },
  ],
  sections: [
    { id: 'gates', group: 'gates', required_permissions: ['catalog.read'] },
    { id: 'scope_record', group: 'gates', required_permissions: ['commissioning.run'] },
    { id: 'confirm_signature', group: 'gates', required_permissions: ['agreements.sign'] },
    { id: 'policy', group: 'policy', required_permissions: ['catalog.read'] },
  ],
  expected_roles: [
    'provider_installer',
    'plant_manager',
    'coordinator_sst',
    'administrator',
    'copasst',
  ],
  backend_unit: 'U-03',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
