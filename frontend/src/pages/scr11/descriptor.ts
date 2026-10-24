/**
 * SCR-11 · Causa raíz con asistente (U-04), VIG-314. Ruta con `finding_state_id` (nota de
 * `frontend-components.md` §6).
 */
import type { ScreenDescriptor } from '@/app/descriptor';

export const descriptor: ScreenDescriptor = {
  screen_id: 'SCR-11',
  title_key: 'navigation.scr11',
  route: '/findings/:finding_state_id/root-cause',
  required_permissions: ['root_cause.analyze'],
  route_groups: [
    {
      id: 'root_cause',
      paths: ['/findings/:finding_state_id/root-cause'],
      access: 'permission',
      required_permissions: ['root_cause.analyze'],
      load: () => import('./root_cause'),
    },
  ],
  sections: [{ id: 'analysis', group: 'root_cause', required_permissions: ['root_cause.analyze'] }],
  expected_roles: ['coordinator_sst'],
  backend_unit: 'U-04',
  home_for_roles: [],
  home_group: null,
  device_classes: ['desktop'],
};
