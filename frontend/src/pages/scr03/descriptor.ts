/**
 * SCR-03 · Verificador de cadena y línea de tiempo de cobertura (U-02), VIG-307. Grupos:
 * `integrity` y `coverage`.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-03',
  title_text: 'navigation.scr03',
  route: '/integrity',
  required_permissions: ['integrity.verify', 'coverage.read'],
  route_groups: [
    {
      id: 'integrity',
      paths: ['/integrity'],
      access: 'permission',
      required_permissions: ['integrity.verify'],
      load: () => import('./integrity'),
    },
    {
      id: 'coverage',
      paths: ['/zones/:zone_id/coverage'],
      access: 'permission',
      required_permissions: ['coverage.read'],
      load: () => import('./coverage'),
    },
  ],
  sections: [
    { id: 'integrity', group: 'integrity', required_permissions: ['integrity.verify'] },
    { id: 'coverage', group: 'coverage', required_permissions: ['coverage.read'] },
  ],
  expected_roles: ['plant_manager', 'coordinator_sst'],
  backend_unit: 'U-02',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
