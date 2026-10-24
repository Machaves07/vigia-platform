/** SCR-14 · Exportación del expediente (U-04; U-02), VIG-317. */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-14',
  title_key: 'navigation.scr14',
  route: '/exports',
  required_permissions: ['export.create'],
  route_groups: [
    {
      id: 'exports',
      paths: ['/exports'],
      access: 'permission',
      required_permissions: ['export.create'],
      load: () => import('./exports'),
    },
  ],
  sections: [{ id: 'exports', group: 'exports', required_permissions: ['export.create'] }],
  expected_roles: ['plant_manager', 'coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
