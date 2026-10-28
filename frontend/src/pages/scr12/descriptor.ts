/**
 * SCR-12 · Paquete de revisión mensual (U-04), VIG-315. Grupos: `package` (ruta canónica) y
 * `entry` (`/zones/monthly`, elección de zona y periodo sin parámetros), que es el inicio del mando
 * de línea. Claves de §6: `review_package.read`, `findings.classify` (muestra) y
 * `commitments.write`.
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-12',
  title_text: 'navigation.scr12',
  route: '/zones/:zone_id/monthly/:period',
  required_permissions: ['review_package.read', 'findings.classify', 'commitments.write'],
  route_groups: [
    {
      id: 'entry',
      paths: ['/zones/monthly'],
      access: 'permission',
      required_permissions: ['review_package.read'],
      menu: { title_text: 'navigation.menu.monthly' },
      load: () => import('./entry'),
    },
    {
      id: 'package',
      paths: ['/zones/:zone_id/monthly/:period'],
      access: 'permission',
      required_permissions: ['review_package.read', 'findings.classify', 'commitments.write'],
      load: () => import('./package'),
    },
  ],
  sections: [
    { id: 'package', group: 'package', required_permissions: ['review_package.read'] },
    { id: 'sample_review', group: 'package', required_permissions: ['findings.classify'] },
    { id: 'commitments', group: 'package', required_permissions: ['commitments.write'] },
  ],
  expected_roles: ['line_manager', 'coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: ['line_manager'],
  home_group: 'entry',
  device_classes: ['desktop'],
};
